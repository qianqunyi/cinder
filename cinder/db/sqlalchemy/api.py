# Copyright (c) 2011 X.commerce, a business unit of eBay Inc.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# Copyright 2014 IBM Corp.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Defines interface for DB access.

Functions in this module are imported into the cinder.db namespace. Call these
functions from cinder.db namespace, not the cinder.db.api namespace.

All functions in this module return objects that implement a dictionary-like
interface. Currently, many of these objects are sqlalchemy objects that
implement a dictionary interface. However, a future goal is to have all of
these objects be simple dictionaries.

**Related Flags**

:connection:  string specifying the sqlalchemy connection to use, like:
    `sqlite:///var/lib/cinder/cinder.sqlite`.
:enable_new_services:  when adding a new service to the database, is it in the
    pool of available hardware (Default: True)
"""

import collections
from collections import abc
import datetime as dt
import functools
import itertools
import re
import sys
import uuid

from oslo_config import cfg
from oslo_db import api as oslo_db_api
from oslo_db import exception as db_exc
from oslo_db import options
from oslo_db.sqlalchemy import enginefacade
from oslo_log import log as logging
from oslo_utils import importutils
from oslo_utils import timeutils
from oslo_utils import uuidutils
osprofiler_sqlalchemy = importutils.try_import('osprofiler.sqlalchemy')
import sqlalchemy as sa
from sqlalchemy import MetaData
from sqlalchemy import or_, and_
from sqlalchemy.orm import joinedload, undefer_group, load_only
from sqlalchemy.orm import RelationshipProperty
from sqlalchemy import sql
from sqlalchemy.sql.expression import bindparam
from sqlalchemy.sql.expression import desc
from sqlalchemy.sql.expression import true
from sqlalchemy.sql import func
from sqlalchemy.sql import sqltypes

from cinder.api import common
from cinder.common import sqlalchemyutils
from cinder import db
from cinder.db.sqlalchemy import models
from cinder import exception
from cinder.i18n import _
from cinder import objects
from cinder.objects import fields
from cinder import utils
from cinder.volume import volume_utils

CONF = cfg.CONF
LOG = logging.getLogger(__name__)

# Map with cases where attach status differs from volume status
ATTACH_STATUS_MAP = {'attached': 'in-use', 'detached': 'available'}

options.set_defaults(CONF, connection='sqlite:///$state_path/cinder.sqlite')

main_context_manager = enginefacade.transaction_context()


def get_engine():
    return main_context_manager.writer.get_engine()


def dispose_engine():
    get_engine().dispose()


_DEFAULT_QUOTA_NAME = 'default'


def get_backend():
    """The backend is this module itself."""

    return sys.modules[__name__]


def is_admin_context(context):
    """Indicates if the request context is an administrator."""
    if not context:
        raise exception.CinderException(
            'Use of empty request context is deprecated'
        )
    return context.is_admin


def is_user_context(context):
    """Indicates if the request context is a normal user."""
    if not context:
        return False
    if context.is_admin:
        return False
    if not context.user_id or not context.project_id:
        return False
    return True


def authorize_project_context(context, project_id):
    """Ensures a request has permission to access the given project."""
    if is_user_context(context):
        if not context.project_id:
            raise exception.NotAuthorized()
        elif context.project_id != project_id:
            raise exception.NotAuthorized()


def authorize_user_context(context, user_id):
    """Ensures a request has permission to access the given user."""
    if is_user_context(context):
        if not context.user_id:
            raise exception.NotAuthorized()
        elif context.user_id != user_id:
            raise exception.NotAuthorized()


def authorize_quota_class_context(context, class_name):
    """Ensures a request has permission to access the given quota class."""
    if is_user_context(context):
        if not context.quota_class:
            raise exception.NotAuthorized()
        elif context.quota_class != class_name:
            raise exception.NotAuthorized()


def require_admin_context(f):
    """Decorator to require admin request context.

    The first argument to the wrapped function must be the context.

    """

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not is_admin_context(args[0]):
            raise exception.AdminRequired()
        return f(*args, **kwargs)

    return wrapper


def require_context(f):
    """Decorator to require *any* user or admin context.

    This does no authorization for user or project access matching, see
    :py:func:`authorize_project_context` and
    :py:func:`authorize_user_context`.

    The first argument to the wrapped function must be the context.

    """

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not is_admin_context(args[0]) and not is_user_context(args[0]):
            raise exception.NotAuthorized()
        return f(*args, **kwargs)

    return wrapper


###################


@require_context
@main_context_manager.reader
def resource_exists(context, model, resource_id):
    conditions = [model.id == resource_id]
    # Match non deleted resources by the id
    if 'no' == context.read_deleted:
        conditions.append(~model.deleted)
    # If the context is not admin we limit it to the context's project
    if is_user_context(context) and hasattr(model, 'project_id'):
        conditions.append(model.project_id == context.project_id)
    query = context.session.query(sql.exists().where(and_(*conditions)))
    return query.scalar()


def require_volume_exists(f):
    """Decorator to require the specified volume to exist.

    Requires the wrapped function to use context and volume_id as
    their first two arguments.
    """

    @functools.wraps(f)
    def wrapper(context, volume_id, *args, **kwargs):
        if not resource_exists(context, models.Volume, volume_id):
            raise exception.VolumeNotFound(volume_id=volume_id)
        return f(context, volume_id, *args, **kwargs)

    return wrapper


def require_snapshot_exists(f):
    """Decorator to require the specified snapshot to exist.

    Requires the wrapped function to use context and snapshot_id as
    their first two arguments.
    """

    @functools.wraps(f)
    def wrapper(context, snapshot_id, *args, **kwargs):
        if not resource_exists(context, models.Snapshot, snapshot_id):
            raise exception.SnapshotNotFound(snapshot_id=snapshot_id)
        return f(context, snapshot_id, *args, **kwargs)

    return wrapper


def require_backup_exists(f):
    """Decorator to require the specified snapshot to exist.

    Requires the wrapped function to use context and backup_id as
    their first two arguments.
    """

    @functools.wraps(f)
    def wrapper(context, backup_id, *args, **kwargs):
        if not resource_exists(context, models.Backup, backup_id):
            raise exception.BackupNotFound(backup_id=backup_id)
        return f(context, backup_id, *args, **kwargs)

    return wrapper


def require_qos_specs_exists(f):
    """Decorator to require the specified QoS speces exist.

    Requires the wrapped function to use context and qos_specs_id as
    their first two arguments.
    """

    @functools.wraps(f)
    def wrapper(context, qos_specs_id, *args, **kwargs):
        if not resource_exists(
            context,
            models.QualityOfServiceSpecs,
            qos_specs_id,
        ):
            raise exception.QoSSpecsNotFound(specs_id=qos_specs_id)
        return f(context, qos_specs_id, *args, **kwargs)

    return wrapper


def handle_db_data_error(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except db_exc.DBDataError:
            msg = _('Error writing field to database')
            LOG.exception(msg)
            raise exception.Invalid(msg)

    return wrapper


def model_query(context, model, *args, **kwargs):
    """Query helper that accounts for context's `read_deleted` field.

    :param context: A request context to query under
    :param model: Model to query. Must be a subclass of ModelBase.
    :param args: Arguments to query. If None - model is used.
    :param read_deleted: if present, overrides context's read_deleted field.
    :param project_only: if present and context is user-type, then restrict
        query to match the context's project_id.
    """
    read_deleted = kwargs.get('read_deleted') or context.read_deleted
    project_only = kwargs.get('project_only')

    query = context.session.query(model, *args)

    if read_deleted == 'no':
        query = query.filter_by(deleted=False)
    elif read_deleted == 'yes':
        pass  # omit the filter to include deleted and active
    elif read_deleted == 'only':
        query = query.filter_by(deleted=True)
    elif read_deleted == 'int_no':
        query = query.filter_by(deleted=0)
    else:
        msg = _("Unrecognized read_deleted value '%s'")
        raise Exception(msg % read_deleted)

    if project_only and is_user_context(context):
        if model is models.VolumeAttachment:
            # NOTE(dulek): In case of VolumeAttachment, we need to join
            # `project_id` through `volume` relationship.
            query = query.filter(
                models.Volume.project_id == context.project_id
            )
        else:
            query = query.filter_by(project_id=context.project_id)

    return query


###################


def get_model_for_versioned_object(versioned_object):
    if isinstance(versioned_object, str):
        model_name = versioned_object
    else:
        model_name = versioned_object.obj_name()
    if model_name == 'BackupImport':
        return models.Backup
    return getattr(models, model_name)


def _get_get_method(model):
    # Exceptions to model to get methods, in general method names are a simple
    # conversion changing ORM name from camel case to snake format and adding
    # _get to the string
    GET_EXCEPTIONS = {
        models.ConsistencyGroup: consistencygroup_get,
        models.VolumeType: _volume_type_get_full,
        models.QualityOfServiceSpecs: qos_specs_get,
        models.GroupType: _group_type_get_full,
        models.CGSnapshot: cgsnapshot_get,
    }

    if model in GET_EXCEPTIONS:
        return GET_EXCEPTIONS[model]

    # General conversion
    # Convert camel cased model name to snake format
    s = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', model.__name__)
    # Get method must be snake formatted model name concatenated with _get
    method_name = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s).lower() + '_get'
    return globals().get(method_name)


_GET_METHODS = {}


@require_context
@main_context_manager.reader
def get_by_id(context, model, id, *args, **kwargs):
    # Add get method to cache dictionary if it's not already there
    if not _GET_METHODS.get(model):
        _GET_METHODS[model] = _get_get_method(model)

    return _GET_METHODS[model](context, id, *args, **kwargs)


def condition_db_filter(model, field, value):
    """Create matching filter.

    If value is an iterable other than a string, any of the values is
    a valid match (OR), so we'll use SQL IN operator.

    If it's not an iterator == operator will be used.
    """
    orm_field = getattr(model, field)
    # For values that must match and are iterables we use IN
    if isinstance(value, abc.Iterable) and not isinstance(value, str):
        # We cannot use in_ when one of the values is None
        if None not in value:
            return orm_field.in_(value)

        return or_(orm_field == v for v in value)

    # For values that must match and are not iterables we use ==
    return orm_field == value


def condition_not_db_filter(model, field, value, auto_none=True):
    """Create non matching filter.

    If value is an iterable other than a string, any of the values is
    a valid match (OR), so we'll use SQL IN operator.

    If it's not an iterator == operator will be used.

    If auto_none is True then we'll consider NULL values as different as well,
    like we do in Python and not like SQL does.
    """
    result = ~condition_db_filter(model, field, value)  # pylint: disable=E1130

    if auto_none and (
        (
            isinstance(value, abc.Iterable)
            and not isinstance(value, str)
            and None not in value
        )
        or (value is not None)
    ):
        orm_field = getattr(model, field)
        result = or_(result, orm_field.is_(None))

    return result


def is_orm_value(obj):
    """Check if object is an ORM field or expression."""
    return isinstance(
        obj,
        (
            sa.orm.attributes.InstrumentedAttribute,
            sa.sql.expression.ColumnElement,
        ),
    )


def _check_is_not_multitable(values, model):
    """Check that we don't try to do multitable updates.

    Since PostgreSQL doesn't support multitable updates we want to always fail
    if we have such a query in our code, even if with MySQL it would work.
    """
    used_models = set()
    for field in values:
        if isinstance(field, sa.orm.attributes.InstrumentedAttribute):
            used_models.add(field.class_)
        elif isinstance(field, str):
            used_models.add(model)
        else:
            raise exception.ProgrammingError(
                reason=(
                    'DB Conditional update - Unknown field type, must be '
                    'string or ORM field.'
                ),
            )
        if len(used_models) > 1:
            raise exception.ProgrammingError(
                reason=(
                    'DB Conditional update - Error in query, multitable '
                    'updates are not supported.'
                ),
            )


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
def _conditional_update(
    context,
    model,
    values,
    expected_values,
    filters=None,
    include_deleted='no',
    project_only=False,
    order=None,
):
    """Compare-and-swap conditional update SQLAlchemy implementation."""
    _check_is_not_multitable(values, model)

    # Provided filters will become part of the where clause
    where_conds = list(filters) if filters else []

    # Build where conditions with operators ==, !=, NOT IN and IN
    for field, condition in expected_values.items():
        if not isinstance(condition, db.Condition):
            condition = db.Condition(condition, field)
        where_conds.append(condition.get_filter(model, field))

    # Create the query with the where clause
    query = model_query(
        context, model, read_deleted=include_deleted, project_only=project_only
    ).filter(*where_conds)

    # NOTE(geguileo): Some DBs' update method are order dependent, and they
    # behave differently depending on the order of the values, example on a
    # volume with 'available' status:
    #    UPDATE volumes SET previous_status=status, status='reyping'
    #        WHERE id='44f284f9-877d-4fce-9eb4-67a052410054';
    # Will result in a volume with 'retyping' status and 'available'
    # previous_status both on SQLite and MariaDB, but
    #    UPDATE volumes SET status='retyping', previous_status=status
    #        WHERE id='44f284f9-877d-4fce-9eb4-67a052410054';
    # Will yield the same result in SQLite but will result in a volume with
    # status and previous_status set to 'retyping' in MariaDB, which is not
    # what we want, so order must be taken into consideration.
    # Order for the update will be:
    #  1- Order specified in argument order
    #  2- Values that refer to other ORM field (simple and using operations,
    #     like size + 10)
    #  3- Values that use Case clause (since they may be using fields as well)
    #  4- All other values
    order = list(order) if order else tuple()
    orm_field_list = []
    case_list = []
    unordered_list = []
    for key, value in values.items():
        if isinstance(value, db.Case):
            value = sa.case(
                *value.whens,
                value=value.value,
                else_=value.else_,
            )

        if key in order:
            # pylint: disable=E1137; ("order" is known to be a list, here)
            order[order.index(key)] = (key, value)
            continue
        # NOTE(geguileo): Check Case first since it's a type of orm value
        if isinstance(value, sql.elements.Case):
            value_list = case_list
        elif is_orm_value(value):
            value_list = orm_field_list
        else:
            value_list = unordered_list
        value_list.append((key, value))

    update_args = {'synchronize_session': False}

    # If we don't have to enforce any kind of order just pass along the values
    # dictionary since it will be a little more efficient.
    if order or orm_field_list or case_list:
        # If we are doing an update with ordered parameters, we need to add
        # remaining values to the list
        values = itertools.chain(
            order, orm_field_list, case_list, unordered_list
        )
        # And we have to tell SQLAlchemy that we want to preserve the order
        update_args['update_args'] = {'preserve_parameter_order': True}

    # Return True if we were able to change any DB entry, False otherwise
    result = query.update(values, **update_args)
    return 0 != result


@require_context
@main_context_manager.writer
def conditional_update(
    context,
    model,
    values,
    expected_values,
    filters=None,
    include_deleted='no',
    project_only=False,
    order=None,
):
    """Compare-and-swap conditional update SQLAlchemy implementation."""
    return _conditional_update(
        context,
        model,
        values,
        expected_values,
        filters=filters,
        include_deleted=include_deleted,
        project_only=project_only,
        order=order,
    )


###################


def _sync_volumes(
    context,
    project_id,
    volume_type_id=None,
    volume_type_name=None,
):
    volumes, _ = _volume_data_get_for_project(
        context,
        project_id,
        volume_type_id=volume_type_id,
    )
    key = 'volumes'
    if volume_type_name:
        key += '_' + volume_type_name
    return {key: volumes}


def _sync_snapshots(
    context,
    project_id,
    volume_type_id=None,
    volume_type_name=None,
):
    snapshots, _ = _snapshot_data_get_for_project(
        context,
        project_id,
        volume_type_id=volume_type_id,
    )
    key = 'snapshots'
    if volume_type_name:
        key += '_' + volume_type_name
    return {key: snapshots}


def _sync_backups(
    context,
    project_id,
    volume_type_id=None,
    volume_type_name=None,
):
    backups, _ = _backup_data_get_for_project(
        context,
        project_id,
        volume_type_id=volume_type_id,
    )
    key = 'backups'
    return {key: backups}


def _sync_gigabytes(
    context,
    project_id,
    volume_type_id=None,
    volume_type_name=None,
):
    _, vol_gigs = _volume_data_get_for_project(
        context,
        project_id,
        volume_type_id=volume_type_id,
    )

    key = 'gigabytes'
    if volume_type_name:
        key += '_' + volume_type_name

    if CONF.no_snapshot_gb_quota:
        return {key: vol_gigs}

    _, snap_gigs = _snapshot_data_get_for_project(
        context,
        project_id,
        volume_type_id=volume_type_id,
    )

    return {key: vol_gigs + snap_gigs}


def _sync_backup_gigabytes(
    context,
    project_id,
    volume_type_id=None,
    volume_type_name=None,
):
    key = 'backup_gigabytes'
    _, backup_gigs = _backup_data_get_for_project(
        context,
        project_id,
        volume_type_id=volume_type_id,
    )
    return {key: backup_gigs}


def _sync_groups(
    context,
    project_id,
    volume_type_id=None,
    volume_type_name=None,
):
    _, groups = _group_data_get_for_project(context, project_id)
    key = 'groups'
    return {key: groups}


QUOTA_SYNC_FUNCTIONS = {
    '_sync_volumes': _sync_volumes,
    '_sync_snapshots': _sync_snapshots,
    '_sync_gigabytes': _sync_gigabytes,
    '_sync_backups': _sync_backups,
    '_sync_backup_gigabytes': _sync_backup_gigabytes,
    '_sync_groups': _sync_groups,
}


###################


def _clean_filters(filters):
    return {k: v for k, v in filters.items() if v is not None}


def _filter_host(field, value, match_level=None):
    """Generate a filter condition for host and cluster fields.

    Levels are:
    - 'pool': Will search for an exact match
    - 'backend': Will search for exact match and value#*
    - 'host'; Will search for exact match, value@* and value#*

    If no level is provided we'll determine it based on the value we want to
    match:
    - 'pool': If '#' is present in value
    - 'backend': If '@' is present in value and '#' is not present
    - 'host': In any other case

    :param field: ORM field.  Ex: objects.Volume.model.host
    :param value: String to compare with
    :param match_level: 'pool', 'backend', or 'host'
    """
    # If we don't set level we'll try to determine it automatically.  LIKE
    # operations are expensive, so we try to reduce them to the minimum.
    if match_level is None:
        if '#' in value:
            match_level = 'pool'
        elif '@' in value:
            match_level = 'backend'
        else:
            match_level = 'host'

    # Mysql is not doing case sensitive filtering, so we force it
    conn_str = CONF.database.connection
    if conn_str.startswith('mysql') and conn_str[5] in ['+', ':']:
        cmp_value = func.binary(value)
        like_op = 'LIKE BINARY'
    else:
        cmp_value = value
        like_op = 'LIKE'

    conditions = [field == cmp_value]
    if match_level != 'pool':
        conditions.append(field.op(like_op)(value + '#%'))
        if match_level == 'host':
            conditions.append(field.op(like_op)(value + '@%'))

    return or_(*conditions)


def _filter_time_comparison(field, time_filter_dict):
    """Generate a filter condition for time comparison operators"""

    conditions = []
    for operator in time_filter_dict:
        filter_time = timeutils.normalize_time(time_filter_dict[operator])
        if operator == 'gt':
            conditions.append(field.op('>')(filter_time))
        elif operator == 'gte':
            conditions.append(field.op('>=')(filter_time))
        if operator == 'eq':
            conditions.append(field.op('=')(filter_time))
        elif operator == 'neq':
            conditions.append(field.op('!=')(filter_time))
        if operator == 'lt':
            conditions.append(field.op('<')(filter_time))
        elif operator == 'lte':
            conditions.append(field.op('<=')(filter_time))
    return or_(*conditions)


def _clustered_bool_field_filter(query, field_name, filter_value):
    # Now that we have clusters, a service is disabled/frozen if the service
    # doesn't belong to a cluster or if it belongs to a cluster and the cluster
    # itself is disabled/frozen.
    if filter_value is not None:
        query_filter = or_(
            and_(
                models.Service.cluster_name.is_(None),
                getattr(models.Service, field_name),
            ),
            and_(
                models.Service.cluster_name.isnot(None),
                sql.exists().where(
                    and_(
                        models.Cluster.name == models.Service.cluster_name,
                        models.Cluster.binary == models.Service.binary,
                        ~models.Cluster.deleted,
                        getattr(models.Cluster, field_name),
                    )
                ),
            ),
        )
        if not filter_value:
            query_filter = ~query_filter  # pylint: disable=E1130
        query = query.filter(query_filter)
    return query


def _service_query(
    context,
    read_deleted='no',
    host=None,
    cluster_name=None,
    is_up=None,
    host_or_cluster=None,
    backend_match_level=None,
    disabled=None,
    frozen=None,
    **filters,
):
    filters = _clean_filters(filters)
    if filters and not is_valid_model_filters(models.Service, filters):
        return None

    query = model_query(context, models.Service, read_deleted=read_deleted)

    # Host and cluster are particular cases of filters, because we must
    # retrieve not only exact matches (single backend configuration), but also
    # match those that have the backend defined (multi backend configuration).
    if host:
        query = query.filter(
            _filter_host(models.Service.host, host, backend_match_level)
        )
    if cluster_name:
        query = query.filter(
            _filter_host(
                models.Service.cluster_name, cluster_name, backend_match_level
            )
        )
    if host_or_cluster:
        query = query.filter(
            or_(
                _filter_host(
                    models.Service.host, host_or_cluster, backend_match_level
                ),
                _filter_host(
                    models.Service.cluster_name,
                    host_or_cluster,
                    backend_match_level,
                ),
            )
        )

    query = _clustered_bool_field_filter(query, 'disabled', disabled)
    query = _clustered_bool_field_filter(query, 'frozen', frozen)

    if filters:
        query = query.filter_by(**filters)

    if is_up is not None:
        date_limit = utils.service_expired_time()
        svc = models.Service
        filter_ = or_(
            and_(svc.created_at.isnot(None), svc.created_at >= date_limit),
            and_(svc.updated_at.isnot(None), svc.updated_at >= date_limit),
        )
        query = query.filter(filter_ == is_up)

    return query


@require_admin_context
@main_context_manager.writer
def service_destroy(context, service_id):
    query = _service_query(context, id=service_id)
    updated_values = models.Service.delete_values()
    if not query.update(updated_values):
        raise exception.ServiceNotFound(service_id=service_id)
    return updated_values


@require_admin_context
@main_context_manager.reader
def service_get(context, service_id=None, backend_match_level=None, **filters):
    """Get a service that matches the criteria.

    A possible filter is is_up=True and it will filter nodes that are down.

    :param service_id: Id of the service.
    :param filters: Filters for the query in the form of key/value.
    :param backend_match_level: 'pool', 'backend', or 'host' for host and
        cluster filters (as defined in _filter_host method)
    :raise ServiceNotFound: If service doesn't exist.
    """
    query = _service_query(
        context,
        backend_match_level=backend_match_level,
        id=service_id,
        **filters,
    )
    service = None if not query else query.first()
    if not service:
        serv_id = service_id or filters.get('topic') or filters.get('binary')
        raise exception.ServiceNotFound(
            service_id=serv_id, host=filters.get('host')
        )
    return service


@require_admin_context
@main_context_manager.reader
def service_get_all(context, backend_match_level=None, **filters):
    """Get all services that match the criteria.

    A possible filter is is_up=True and it will filter nodes that are down.

    :param filters: Filters for the query in the form of key/value.
    :param backend_match_level: 'pool', 'backend', or 'host' for host and
        cluster filters (as defined in _filter_host method)
    """
    query = _service_query(
        context, backend_match_level=backend_match_level, **filters
    )
    return [] if not query else query.all()


@require_admin_context
@main_context_manager.reader
def service_get_by_uuid(context, service_uuid):
    query = model_query(context, models.Service).filter_by(uuid=service_uuid)
    result = query.first()
    if not result:
        raise exception.ServiceNotFound(service_id=service_uuid)

    return result


@require_admin_context
@main_context_manager.writer
def service_create(context, values):
    service_ref = models.Service()
    service_ref.update(values)
    if not CONF.enable_new_services:
        service_ref.disabled = True

    service_ref.save(context.session)
    return service_ref


@require_admin_context
@main_context_manager.writer
def service_update(context, service_id, values, retry=True):
    def _service_update(context, service_id, values):
        query = _service_query(context, id=service_id)

        if 'disabled' in values:
            entity = query.column_descriptions[0]['entity']

            values = values.copy()
            values['modified_at'] = values.get('modified_at',
                                               timeutils.utcnow())
            values['updated_at'] = values.get('updated_at',
                                              entity.updated_at)

        result = query.update(values)
        if not result:
            raise exception.ServiceNotFound(service_id=service_id)

    @oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
    def _service_update_retry(context, service_id, values):
        _service_update(context, service_id, values)

    if retry:
        _service_update_retry(context, service_id, values)
    else:
        _service_update(context, service_id, values)


###################


@require_admin_context
@main_context_manager.reader
def is_backend_frozen(context, host, cluster_name):
    """Check if a storage backend is frozen based on host and cluster_name."""
    if cluster_name:
        model = models.Cluster
        conditions = [model.name == volume_utils.extract_host(cluster_name)]
    else:
        model = models.Service
        conditions = [model.host == volume_utils.extract_host(host)]
    conditions.extend((~model.deleted, model.frozen))
    query = context.session.query(sql.exists().where(and_(*conditions)))
    frozen = query.scalar()
    return frozen


###################


def _cluster_query(
    context,
    is_up=None,
    get_services=False,
    services_summary=False,
    read_deleted='no',
    name_match_level=None,
    name=None,
    **filters,
):
    filters = _clean_filters(filters)
    if filters and not is_valid_model_filters(models.Cluster, filters):
        return None

    query = model_query(context, models.Cluster, read_deleted=read_deleted)

    # Cluster is a special case of filter, because we must match exact match
    # as well as hosts that specify the backend
    if name:
        query = query.filter(
            _filter_host(models.Cluster.name, name, name_match_level)
        )

    if filters:
        query = query.filter_by(**filters)

    if services_summary:
        query = query.options(undefer_group('services_summary'))
        # We bind the expiration time to now (as it changes with each query)
        # and is required by num_down_hosts
        query = query.params(expired=utils.service_expired_time())
    elif 'num_down_hosts' in filters:
        query = query.params(expired=utils.service_expired_time())

    if get_services:
        query = query.options(joinedload(models.Cluster.services))

    if is_up is not None:
        date_limit = utils.service_expired_time()
        filter_ = and_(
            models.Cluster.last_heartbeat.isnot(None),
            models.Cluster.last_heartbeat >= date_limit,
        )
        query = query.filter(filter_ == is_up)

    return query


@require_admin_context
@main_context_manager.reader
def cluster_get(
    context,
    id=None,
    is_up=None,
    get_services=False,
    services_summary=False,
    read_deleted='no',
    name_match_level=None,
    **filters,
):
    """Get a cluster that matches the criteria.

    :param id: Id of the cluster.
    :param is_up: Boolean value to filter based on the cluster's up status.
    :param get_services: If we want to load all services from this cluster.
    :param services_summary: If we want to load num_hosts and
                             num_down_hosts fields.
    :param read_deleted: Filtering based on delete status. Default value is
                         "no".
    :param filters: Field based filters in the form of key/value.
    :param name_match_level: 'pool', 'backend', or 'host' for name filter (as
                             defined in _filter_host method)
    :raise ClusterNotFound: If cluster doesn't exist.
    """
    query = _cluster_query(
        context,
        is_up,
        get_services,
        services_summary,
        read_deleted,
        name_match_level,
        id=id,
        **filters,
    )
    cluster = None if not query else query.first()
    if not cluster:
        cluster_id = id or str(filters)
        raise exception.ClusterNotFound(id=cluster_id)
    return cluster


@require_admin_context
@main_context_manager.reader
def cluster_get_all(
    context,
    is_up=None,
    get_services=False,
    services_summary=False,
    read_deleted='no',
    name_match_level=None,
    **filters,
):
    """Get all clusters that match the criteria.

    :param is_up: Boolean value to filter based on the cluster's up status.
    :param get_services: If we want to load all services from this cluster.
    :param services_summary: If we want to load num_hosts and
        num_down_hosts fields.
    :param read_deleted: Filtering based on delete status. Default value is
        "no".
    :param name_match_level: 'pool', 'backend', or 'host' for name filter (as
        defined in _filter_host method)
    :param filters: Field based filters in the form of key/value.
    """
    query = _cluster_query(
        context,
        is_up,
        get_services,
        services_summary,
        read_deleted,
        name_match_level,
        **filters,
    )
    return [] if not query else query.all()


@require_admin_context
@main_context_manager.writer
def cluster_create(context, values):
    """Create a cluster from the values dictionary."""
    cluster_ref = models.Cluster()
    cluster_ref.update(values)
    # Provided disabled value takes precedence
    if values.get('disabled') is None:
        cluster_ref.disabled = not CONF.enable_new_services

    try:
        cluster_ref.save(context.session)
        # We mark that newly created cluster has no hosts to prevent
        # problems at the OVO level
        cluster_ref.last_heartbeat = None
        return cluster_ref
    # If we had a race condition (another non deleted cluster exists with the
    # same name) raise Duplicate exception.
    except db_exc.DBDuplicateEntry:
        raise exception.ClusterExists(name=values.get('name'))


@require_admin_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def cluster_update(context, cluster_id, values):
    """Set the given properties on an cluster and update it.

    Raises ClusterNotFound if cluster does not exist.
    """
    query = _cluster_query(context, id=cluster_id)
    result = query.update(values)
    if not result:
        raise exception.ClusterNotFound(id=cluster_id)


@require_admin_context
@main_context_manager.writer
def cluster_destroy(context, cluster_id):
    """Destroy the cluster or raise if it does not exist or has hosts."""
    query = _cluster_query(context, id=cluster_id)
    query = query.filter(models.Cluster.num_hosts == 0)
    # If the update doesn't succeed we don't know if it's because the
    # cluster doesn't exist or because it has hosts.
    result = query.update(
        models.Cluster.delete_values(), synchronize_session=False
    )

    if not result:
        # This will fail if the cluster doesn't exist raising the right
        # exception
        cluster_get(context, id=cluster_id)
        # If it doesn't fail, then the problem is that there are hosts
        raise exception.ClusterHasHosts(id=cluster_id)


###################


def _metadata_refs(metadata_dict, meta_class):
    metadata_refs = []
    if metadata_dict:
        for k, v in metadata_dict.items():
            metadata_ref = meta_class()
            metadata_ref['key'] = k
            metadata_ref['value'] = v
            metadata_refs.append(metadata_ref)
    return metadata_refs


def _dict_with_extra_specs_if_authorized(context, inst_type_query):
    """Convert type query result to dict with extra_spec and rate_limit.

    Takes a volume type query returned by sqlalchemy and returns it
    as a dictionary, converting the extra_specs entry from a list
    of dicts.  NOTE the contents of extra-specs are admin readable
    only.  If the context passed in for this request is not admin
    then we will return an empty extra-specs dict rather than
    providing the admin only details.

    Example response with admin context:

    'extra_specs' : [{'key': 'k1', 'value': 'v1', ...}, ...]
    to a single dict:
    'extra_specs' : {'k1': 'v1'}

    """

    inst_type_dict = dict(inst_type_query)

    extra_specs = {
        x['key']: x['value'] for x in inst_type_query['extra_specs']
    }
    inst_type_dict['extra_specs'] = extra_specs

    return inst_type_dict


###################


def _dict_with_group_specs_if_authorized(context, inst_type_query):
    """Convert group type query result to dict with spec and rate_limit.

    Takes a group type query returned by sqlalchemy and returns it
    as a dictionary, converting the extra_specs entry from a list
    of dicts.  NOTE the contents of extra-specs are admin readable
    only.  If the context passed in for this request is not admin
    then we will return an empty extra-specs dict rather than
    providing the admin only details.

    Example response with admin context:

    'group_specs' : [{'key': 'k1', 'value': 'v1', ...}, ...]
    to a single dict:
    'group_specs' : {'k1': 'v1'}

    """

    inst_type_dict = dict(inst_type_query)
    if not is_admin_context(context):
        del inst_type_dict['group_specs']
    else:
        group_specs = {
            x['key']: x['value'] for x in inst_type_query['group_specs']
        }
        inst_type_dict['group_specs'] = group_specs
    return inst_type_dict


###################


def _quota_get(context, project_id, resource):
    result = (
        model_query(context, models.Quota, read_deleted="no")
        .filter_by(project_id=project_id)
        .filter_by(resource=resource)
        .first()
    )

    if not result:
        raise exception.ProjectQuotaNotFound(project_id=project_id)

    return result


@require_context
@main_context_manager.reader
def quota_get(context, project_id, resource):
    return _quota_get(context, project_id, resource)


@require_context
@main_context_manager.reader
def quota_get_all_by_project(context, project_id):
    rows = (
        model_query(context, models.Quota, read_deleted="no")
        .filter_by(project_id=project_id)
        .all()
    )

    result = {'project_id': project_id}
    for row in rows:
        result[row.resource] = row.hard_limit

    return result


@require_context
@main_context_manager.writer
def quota_create(context, project_id, resource, limit):
    quota_ref = models.Quota()
    quota_ref.project_id = project_id
    quota_ref.resource = resource
    quota_ref.hard_limit = limit
    quota_ref.save(context.session)
    return quota_ref


@require_context
@main_context_manager.writer
def quota_update(context, project_id, resource, limit):
    quota_ref = _quota_get(context, project_id, resource)
    quota_ref.hard_limit = limit
    quota_ref.save(context.session)
    return quota_ref


@require_context
@main_context_manager.writer
def quota_update_resource(context, old_res, new_res):
    quotas = (
        model_query(context, models.Quota, read_deleted='no')
        .filter_by(resource=old_res)
        .all()
    )
    for quota in quotas:
        quota.resource = new_res
        quota.save(context.session)
        return quota


@require_admin_context
@main_context_manager.writer
def quota_destroy(context, project_id, resource):
    quota_ref = _quota_get(context, project_id, resource)
    return quota_ref.delete(context.session)


###################


def _quota_class_get(context, class_name, resource):
    result = (
        model_query(context, models.QuotaClass, read_deleted="no")
        .filter_by(class_name=class_name)
        .filter_by(resource=resource)
        .first()
    )

    if not result:
        raise exception.QuotaClassNotFound(class_name=class_name)

    return result


@require_context
@main_context_manager.reader
def quota_class_get(context, class_name, resource):
    return _quota_class_get(context, class_name, resource)


@require_context
@main_context_manager.reader
def quota_class_get_defaults(context):
    rows = (
        model_query(context, models.QuotaClass, read_deleted="no")
        .filter_by(class_name=_DEFAULT_QUOTA_NAME)
        .all()
    )

    result = {'class_name': _DEFAULT_QUOTA_NAME}
    for row in rows:
        result[row.resource] = row.hard_limit

    return result


@require_context
@main_context_manager.reader
def quota_class_get_all_by_name(context, class_name):
    rows = (
        model_query(context, models.QuotaClass, read_deleted="no")
        .filter_by(class_name=class_name)
        .all()
    )

    result = {'class_name': class_name}
    for row in rows:
        result[row.resource] = row.hard_limit

    return result


@require_context
@main_context_manager.reader
def _quota_class_get_all_by_resource(context, resource):
    result = (
        model_query(context, models.QuotaClass, read_deleted="no")
        .filter_by(resource=resource)
        .all()
    )

    return result


@handle_db_data_error
@require_context
@main_context_manager.writer
def quota_class_create(context, class_name, resource, limit):
    quota_class_ref = models.QuotaClass()
    quota_class_ref.class_name = class_name
    quota_class_ref.resource = resource
    quota_class_ref.hard_limit = limit
    quota_class_ref.save(context.session)
    return quota_class_ref


@require_context
@main_context_manager.writer
def quota_class_update(context, class_name, resource, limit):
    quota_class_ref = _quota_class_get(context, class_name, resource)
    quota_class_ref.hard_limit = limit
    quota_class_ref.save(context.session)
    return quota_class_ref


@require_context
@main_context_manager.writer
def quota_class_update_resource(context, old_res, new_res):
    quota_class_list = _quota_class_get_all_by_resource(context, old_res)
    for quota_class in quota_class_list:
        quota_class.resource = new_res
        quota_class.save(context.session)


@require_context
@main_context_manager.writer
def quota_class_destroy(context, class_name, resource):
    quota_class_ref = _quota_class_get(context, class_name, resource)
    return quota_class_ref.delete(context.session)


@require_context
@main_context_manager.writer
def quota_class_destroy_all_by_name(context, class_name):
    quota_classes = (
        model_query(context, models.QuotaClass, read_deleted="no")
        .filter_by(class_name=class_name)
        .all()
    )

    for quota_class_ref in quota_classes:
        quota_class_ref.delete(context.session)


###################


@require_context
@main_context_manager.reader
def quota_usage_get(context, project_id, resource):
    result = (
        model_query(context, models.QuotaUsage, read_deleted="no")
        .filter_by(project_id=project_id)
        .filter_by(resource=resource)
        .first()
    )

    if not result:
        raise exception.QuotaUsageNotFound(project_id=project_id)

    return result


@require_context
@main_context_manager.reader
def quota_usage_get_all_by_project(context, project_id):
    rows = (
        model_query(context, models.QuotaUsage, read_deleted="no")
        .filter_by(project_id=project_id)
        .all()
    )

    result = {'project_id': project_id}
    for row in rows:
        result[row.resource] = dict(in_use=row.in_use, reserved=row.reserved)

    return result


def _quota_usage_create(
    context,
    project_id,
    resource,
    in_use,
    reserved,
    until_refresh,
):
    quota_usage_ref = models.QuotaUsage()
    quota_usage_ref.project_id = project_id
    quota_usage_ref.resource = resource
    quota_usage_ref.in_use = in_use
    quota_usage_ref.reserved = reserved
    quota_usage_ref.until_refresh = until_refresh
    quota_usage_ref.save(context.session)
    return quota_usage_ref


def _reservation_create(
    context,
    uuid,
    usage,
    project_id,
    resource,
    delta,
    expire,
):
    usage_id = usage['id'] if usage else None
    reservation_ref = models.Reservation()
    reservation_ref.uuid = uuid
    reservation_ref.usage_id = usage_id
    reservation_ref.project_id = project_id
    reservation_ref.resource = resource
    reservation_ref.delta = delta
    reservation_ref.expire = expire
    reservation_ref.save(context.session)
    return reservation_ref


# NOTE(johannes): The quota code uses SQL locking to ensure races don't
# cause under or over counting of resources. To avoid deadlocks, this
# code always acquires the lock on quota_usages before acquiring the lock
# on reservations.


def _get_quota_usages(context, project_id, resources=None):
    # Broken out for testability
    query = model_query(
        context, models.QuotaUsage, read_deleted="no"
    ).filter_by(project_id=project_id)
    if resources:
        query = query.filter(models.QuotaUsage.resource.in_(list(resources)))
    rows = query.order_by(models.QuotaUsage.id.asc()).with_for_update().all()

    return {row.resource: row for row in rows}


def _get_quota_usages_by_resource(context, resource):
    rows = (
        model_query(context, models.QuotaUsage, deleted="no")
        .filter_by(resource=resource)
        .order_by(models.QuotaUsage.id.asc())
        .with_for_update()
        .all()
    )
    return rows


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def quota_usage_update_resource(context, old_res, new_res):
    usages = _get_quota_usages_by_resource(context, old_res)
    for usage in usages:
        usage.resource = new_res
        usage.until_refresh = 1


def _get_sync_updates(ctxt, project_id, resources, resource_name):
    """Return usage for a specific resource.

    Resources are volumes, gigabytes, backups, snapshots, and also
    volumes_<type_name> snapshots_<type_name> for each volume type.
    """
    # Grab the sync routine
    sync = QUOTA_SYNC_FUNCTIONS[resources[resource_name].sync]
    # VolumeTypeResource includes the id and name of the resource.
    volume_type_id = getattr(resources[resource_name], 'volume_type_id', None)
    volume_type_name = getattr(
        resources[resource_name], 'volume_type_name', None
    )
    updates = sync(
        ctxt,
        project_id,
        volume_type_id=volume_type_id,
        volume_type_name=volume_type_name,
    )
    return updates


def _is_duplicate(exc):
    """Check if an exception is caused by a unique constraint failure."""
    return isinstance(exc, db_exc.DBDuplicateEntry)


@require_context
@oslo_db_api.wrap_db_retry(
    max_retries=5, retry_on_deadlock=True, exception_checker=_is_duplicate
)
@main_context_manager.writer
def quota_reserve(
    context,
    resources,
    quotas,
    deltas,
    expire,
    until_refresh,
    max_age,
    project_id=None,
):
    elevated = context.elevated()

    if project_id is None:
        project_id = context.project_id

    # Loop until we can lock all the resource rows we'll be modifying
    while True:
        # Get the current usages and lock existing rows
        usages = _get_quota_usages(
            context, project_id, resources=deltas.keys()
        )
        missing = [res for res in deltas if res not in usages]
        # If we have successfully locked all the rows we can continue.
        # SELECT ... FOR UPDATE used in _get_quota usages cannot lock
        # non-existing rows, so there can be races with other requests
        # trying to create those rows.
        if not missing:
            break

        # Create missing rows calculating current values instead of
        # assuming there are no used resources as admins may have been
        # using this mechanism to force quota usage refresh.
        for resource in missing:
            updates = _get_sync_updates(
                elevated,
                project_id,
                resources,
                resource,
            )
            _quota_usage_create(
                elevated,
                project_id,
                resource,
                updates[resource],
                0,
                until_refresh or None,
            )

        # NOTE: When doing the commit there can be a race condition with
        # other service instances or thread  that are also creating the
        # same rows and in that case this will raise either a Deadlock
        # exception (when multiple transactions were creating the same rows
        # and the DB failed to acquire the row lock on the non-first
        # transaction) or a DBDuplicateEntry exception if some other
        # transaction created the row between us doing the
        # _get_quota_usages and here.  In both cases this transaction will
        # be rolled back and the wrap_db_retry decorator will retry.

        # Commit new rows to the DB.
        context.session.commit()

        # Start a new session before trying to lock all the rows again.  By
        # trying to get all the locks in a loop we can protect us against
        # admins directly deleting DB rows.
        context.session.begin()

    # Handle usage refresh
    for resource in deltas.keys():
        # Do we need to refresh the usage?
        refresh = False
        if usages[resource].in_use < 0:
            # If we created the entry right now we want to refresh.
            # Negative in_use count indicates a desync, so try to
            # heal from that...
            refresh = True
        elif usages[resource].until_refresh is not None:
            usages[resource].until_refresh -= 1
            if usages[resource].until_refresh <= 0:
                refresh = True
        elif (
            max_age
            and usages[resource].updated_at is not None
            and (
                (
                    timeutils.utcnow() - usages[resource].updated_at
                ).total_seconds()
                >= max_age
            )
        ):
            refresh = True

        # OK, refresh the usage
        if refresh:
            updates = _get_sync_updates(
                elevated,
                project_id,
                resources,
                resource,
            )
            # Updates will always contain a single resource usage matching
            # the resource variable.
            usages[resource].in_use = updates[resource]
            usages[resource].until_refresh = until_refresh or None

        # There are 3 cases where we want to update "until_refresh" in the
        # DB: when we enabled it, when we disabled it, and when we changed
        # to a value lower than the current remaining value.
        else:
            res_until = usages[resource].until_refresh
            if (res_until is None and until_refresh) or (
                (res_until or 0) > (until_refresh or 0)
            ):
                usages[resource].until_refresh = until_refresh or None

    # Check for deltas that would go negative
    unders = [
        r
        for r, delta in deltas.items()
        if delta < 0 and delta + usages[r].in_use < 0
    ]

    # TODO(mc_nair): Should ignore/zero alloc if using non-nested driver

    # Now, let's check the quotas
    # NOTE(Vek): We're only concerned about positive increments.
    #            If a project has gone over quota, we want them to
    #            be able to reduce their usage without any
    #            problems.
    overs = [
        r
        for r, delta in deltas.items()
        if quotas[r] >= 0
        and delta >= 0
        and quotas[r] < delta + usages[r].total
    ]

    # NOTE(Vek): The quota check needs to be in the transaction,
    #            but the transaction doesn't fail just because
    #            we're over quota, so the OverQuota raise is
    #            outside the transaction.  If we did the raise
    #            here, our usage updates would be discarded, but
    #            they're not invalidated by being over-quota.

    # Create the reservations
    if not overs:
        reservations = []
        for resource, delta in deltas.items():
            usage = usages[resource]
            reservation = _reservation_create(
                elevated,
                str(uuid.uuid4()),
                usage,
                project_id,
                resource,
                delta,
                expire,
            )

            reservations.append(reservation.uuid)

            # Also update the reserved quantity
            # NOTE(Vek): Again, we are only concerned here about
            #            positive increments.  Here, though, we're
            #            worried about the following scenario:
            #
            #            1) User initiates resize down.
            #            2) User allocates a new instance.
            #            3) Resize down fails or is reverted.
            #            4) User is now over quota.
            #
            #            To prevent this, we only update the
            #            reserved value if the delta is positive.
            if delta > 0:
                usages[resource].reserved += delta

    if unders:
        LOG.warning(
            "Reservation would make usage less than 0 for the "
            "following resources, so on commit they will be "
            "limited to prevent going below 0: %s",
            unders,
        )
    if overs:
        usages = {
            k: dict(in_use=v.in_use, reserved=v.reserved)
            for k, v in usages.items()
        }
        raise exception.OverQuota(
            overs=sorted(overs), quotas=quotas, usages=usages
        )

    return reservations


def _quota_reservations(context, reservations):
    """Return the relevant reservations."""

    # Get the listed reservations
    return (
        model_query(context, models.Reservation, read_deleted="no")
        .filter(models.Reservation.uuid.in_(reservations))
        .with_for_update()
        .all()
    )


def _get_reservation_resources(context, reservation_ids):
    """Return the relevant resources by reservations."""

    reservations = (
        model_query(context, models.Reservation, read_deleted="no")
        .options(load_only(models.Reservation.resource))
        .filter(models.Reservation.uuid.in_(reservation_ids))
        .all()
    )
    return {r.resource for r in reservations}


def _dict_with_usage_id(usages):
    return {row.id: row for row in usages.values()}


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def reservation_commit(context, reservations, project_id=None):
    # NOTE: There's a potential race condition window with
    # reservation_expire, since _get_reservation_resources does not lock
    # the rows, but we won't fix it because:
    # - Minuscule chance of happening, since quota expiration is usually
    #   very high
    # - Solution could create a DB lock on rolling upgrades since we need
    #   to reverse the order of locking the rows.
    usages = _get_quota_usages(
        context,
        project_id,
        resources=_get_reservation_resources(context, reservations),
    )
    usages = _dict_with_usage_id(usages)

    for reservation in _quota_reservations(context, reservations):
        usage = usages[reservation.usage_id]
        delta = reservation.delta
        if delta >= 0:
            usage.reserved -= min(delta, usage.reserved)
        # For negative deltas make sure we never go into negative usage
        elif -delta > usage.in_use:
            delta = -usage.in_use

        usage.in_use += delta

        reservation.delete(context.session)


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def reservation_rollback(context, reservations, project_id=None):
    # NOTE: There's a potential race condition window with
    # reservation_expire, since _get_reservation_resources does not lock
    # the rows, but we won't fix it because:
    # - Minuscule chance of happening, since quota expiration is usually
    #   very high
    # - Solution could create a DB lock on rolling upgrades since we need
    #   to reverse the order of locking the rows.
    usages = _get_quota_usages(
        context,
        project_id,
        resources=_get_reservation_resources(context, reservations),
    )
    usages = _dict_with_usage_id(usages)
    for reservation in _quota_reservations(context, reservations):
        usage = usages[reservation.usage_id]
        if reservation.delta >= 0:
            usage.reserved -= min(reservation.delta, usage.reserved)

        reservation.delete(context.session)


@require_context
def quota_destroy_by_project(context, project_id):
    """Destroy all limit quotas associated with a project.

    Leaves usage and reservation quotas intact.
    """
    quota_destroy_all_by_project(context, project_id, only_quotas=True)


# TODO(stephenfin): No one is using this except 'quota_destroy_by_project'
# above, so the only_quotas=False path could be removed.
@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def quota_destroy_all_by_project(context, project_id, only_quotas=False):
    """Destroy all quotas associated with a project.

    This includes limit quotas, usage quotas and reservation quotas.
    Optionally can only remove limit quotas and leave other types as they are.

    :param context: The request context, for access checks.
    :param project_id: The ID of the project being deleted.
    :param only_quotas: Only delete limit quotas, leave other types intact.
    """
    model_query(context, models.Quota).filter_by(project_id=project_id).update(
        models.Quota.delete_values()
    )

    if only_quotas:
        return

    model_query(context, models.QuotaUsage, read_deleted="no").filter_by(
        project_id=project_id
    ).update(models.QuotaUsage.delete_values())

    model_query(context, models.Reservation, read_deleted="no").filter_by(
        project_id=project_id
    ).update(models.Reservation.delete_values())


@require_admin_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def reservation_expire(context):
    current_time = timeutils.utcnow()
    results = (
        model_query(context, models.Reservation, read_deleted="no")
        .filter(models.Reservation.expire < current_time)
        .with_for_update()
        .all()
    )

    if results:
        for reservation in results:
            if reservation.delta >= 0:
                reservation.usage.reserved -= min(
                    reservation.delta,
                    reservation.usage.reserved,
                )
                reservation.usage.save(context.session)

            reservation.delete(context.session)


###################


@require_admin_context
@main_context_manager.writer
def volume_attach(context, values):
    volume_attachment_ref = models.VolumeAttachment()
    if not values.get('id'):
        values['id'] = str(uuid.uuid4())

    volume_attachment_ref.update(values)
    volume_attachment_ref.save(context.session)
    return _attachment_get(context, values['id'])


@require_admin_context
@main_context_manager.writer
def volume_attached(
    context,
    attachment_id,
    instance_uuid,
    host_name,
    mountpoint,
    attach_mode='rw',
    mark_attached=True,
):
    """This method updates a volume attachment entry.

    This function saves the information related to a particular
    attachment for a volume.  It also updates the volume record
    to mark the volume as attached or attaching.

    The mark_attached argument is a boolean, when set to True,
    we mark the volume as 'in-use' and the 'attachment' as
    'attached', if False, we use 'attaching' for both of these
    status settings.

    """
    attach_status = fields.VolumeAttachStatus.ATTACHED
    volume_status = 'in-use'
    if not mark_attached:
        attach_status = fields.VolumeAttachStatus.ATTACHING
        volume_status = 'attaching'

    if instance_uuid and not uuidutils.is_uuid_like(instance_uuid):
        raise exception.InvalidUUID(uuid=instance_uuid)

    volume_attachment_ref = _attachment_get(context, attachment_id)

    updated_values = {
        'mountpoint': mountpoint,
        'attach_status': attach_status,
        'instance_uuid': instance_uuid,
        'attached_host': host_name,
        'attach_time': timeutils.utcnow(),
        'attach_mode': attach_mode,
        'updated_at': volume_attachment_ref.updated_at,
    }
    volume_attachment_ref.update(updated_values)
    volume_attachment_ref.save(context.session)
    del updated_values['updated_at']

    volume_ref = _volume_get(context, volume_attachment_ref['volume_id'])
    volume_ref['status'] = volume_status
    volume_ref['attach_status'] = attach_status
    volume_ref.save(context.session)

    return volume_ref, updated_values


@handle_db_data_error
@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def volume_create(context, values):
    values['volume_metadata'] = _metadata_refs(
        values.get('metadata'),
        models.VolumeMetadata,
    )
    if is_admin_context(context):
        values['volume_admin_metadata'] = _metadata_refs(
            values.get('admin_metadata'),
            models.VolumeAdminMetadata,
        )
    elif values.get('volume_admin_metadata'):
        del values['volume_admin_metadata']

    volume_ref = models.Volume()
    if not values.get('id'):
        values['id'] = str(uuid.uuid4())
    volume_ref.update(values)

    context.session.add(volume_ref)

    return _volume_get(context, values['id'])


def get_booleans_for_table(table_name):
    booleans = set()
    table = getattr(models, table_name.capitalize())
    if hasattr(table, '__table__'):
        columns = table.__table__.columns
        for column in columns:
            if isinstance(column.type, sqltypes.Boolean):
                booleans.add(column.name)

    return booleans


@require_admin_context
@main_context_manager.reader
def volume_data_get_for_host(context, host, count_only=False):
    host_attr = models.Volume.host
    conditions = [host_attr == host, host_attr.op('LIKE')(host + '#%')]
    if count_only:
        result = (
            model_query(
                context, func.count(models.Volume.id), read_deleted="no"
            )
            .filter(or_(*conditions))
            .first()
        )
        return result[0] or 0
    else:
        result = (
            model_query(
                context,
                func.count(models.Volume.id),
                func.sum(models.Volume.size),
                read_deleted="no",
            )
            .filter(or_(*conditions))
            .first()
        )
        # NOTE(vish): convert None to 0
        return (result[0] or 0, result[1] or 0)


@require_admin_context
def _volume_data_get_for_project(
    context,
    project_id,
    volume_type_id=None,
    host=None,
    skip_internal=True,
):
    model = models.Volume
    query = model_query(
        context, func.count(model.id), func.sum(model.size), read_deleted="no"
    ).filter_by(project_id=project_id)

    # By default we skip temporary resources creted for internal usage and
    # migration destination volumes.
    if skip_internal:
        query = query.filter(model.use_quota)

    if host:
        query = query.filter(_filter_host(model.host, host))

    if volume_type_id:
        query = query.filter_by(volume_type_id=volume_type_id)

    result = query.first()

    # NOTE(vish): convert None to 0
    return result[0] or 0, result[1] or 0


@require_admin_context
@main_context_manager.reader
def volume_data_get_for_project(context, project_id, host=None):
    return _volume_data_get_for_project(
        context,
        project_id,
        host=host,
        skip_internal=False,
    )


VOLUME_DEPENDENT_MODELS = frozenset(
    [
        models.VolumeMetadata,
        models.VolumeAdminMetadata,
        models.Snapshot,
        models.Transfer,
        models.VolumeGlanceMetadata,
        models.VolumeAttachment,
    ]
)


@require_admin_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def volume_destroy(context, volume_id):
    now = timeutils.utcnow()
    updated_values = {
        'status': 'deleted',
        'deleted': True,
        'deleted_at': now,
        'migration_status': None,
    }
    query = model_query(context, models.Volume).filter_by(id=volume_id)
    entity = query.column_descriptions[0]['entity']
    updated_values['updated_at'] = entity.updated_at
    query.update(updated_values)

    for model in VOLUME_DEPENDENT_MODELS:
        query = model_query(context, model).filter_by(volume_id=volume_id)
        entity = query.column_descriptions[0]['entity']
        query.update(
            {
                'deleted': True,
                'deleted_at': now,
                'updated_at': entity.updated_at,
            }
        )
    del updated_values['updated_at']

    return updated_values


def _include_in_cluster(context, cluster, model, partial_rename, filters):
    """Generic include in cluster method.

    When we include resources in a cluster we have to be careful to preserve
    the addressing sections that have not been provided.  That's why we allow
    partial_renaming, so we can preserve the backend and pool if we are only
    providing host/cluster level information, and preserve pool information if
    we only provide backend level information.

    For example when we include a host in a cluster we receive calls with
    filters like {'host': 'localhost@lvmdriver-1'} and cluster with something
    like 'mycluster@lvmdriver-1'.  Since in the DB the resources will have the
    host field set to something like 'localhost@lvmdriver-1#lvmdriver-1' we
    want to include original pool in the new cluster_name.  So we want to store
    in cluster_name value 'mycluster@lvmdriver-1#lvmdriver-1'.
    """
    filters = _clean_filters(filters)
    if filters and not is_valid_model_filters(model, filters):
        return None

    query = context.session.query(model)
    if hasattr(model, 'deleted'):
        query = query.filter_by(deleted=False)

    # cluster_name and host are special filter cases
    for field in {'cluster_name', 'host'}.intersection(filters):
        value = filters.pop(field)
        # We do a special backend filter
        query = query.filter(_filter_host(getattr(model, field), value))
        # If we want to do a partial rename and we haven't set the cluster
        # already, the value we want to set is a SQL replace of existing field
        # value.
        if partial_rename and isinstance(cluster, str):
            cluster = func.replace(getattr(model, field), value, cluster)

    query = query.filter_by(**filters)
    result = query.update({'cluster_name': cluster}, synchronize_session=False)
    return result


@require_admin_context
@main_context_manager.writer
def volume_include_in_cluster(
    context, cluster, partial_rename=True, **filters
):
    """Include all volumes matching the filters into a cluster."""
    return _include_in_cluster(
        context, cluster, models.Volume, partial_rename, filters
    )


def _get_statuses_from_attachments(context, volume_id):
    """Get volume status and attach_status based on existing attachments."""
    # NOTE: Current implementation ignores attachments on error attaching,
    # since they will not have been used by any consumer because os-brick's
    # connect_volume has not been called yet. This leads to cases where a
    # volume will be in 'available' state yet have attachments.

    # If we sort status of attachments alphabetically, ignoring errors, the
    # first element will be the attachment status for the volume:
    #     attached > attaching > detaching > reserved
    attach_status = (
        context.session.query(models.VolumeAttachment.attach_status)
        .filter_by(deleted=False)
        .filter_by(volume_id=volume_id)
        .filter(~models.VolumeAttachment.attach_status.startswith('error_'))
        .order_by(models.VolumeAttachment.attach_status.asc())
        .limit(1)
        .scalar()
    )

    # No volume attachment records means the volume is detached.
    attach_status = attach_status or 'detached'

    # Check cases where volume status is different from attach status, and
    # default to the same value if it's not one of those cases.
    status = ATTACH_STATUS_MAP.get(attach_status, attach_status)

    return status, attach_status


@require_admin_context
@main_context_manager.writer
def volume_detached(context, volume_id, attachment_id):
    """Delete an attachment and update the volume accordingly.

    After marking the attachment as detached the method will decide the status
    and attach_status values for the volume based on the current status and the
    remaining attachments and their status.

    Volume status may be changed to: in-use, attaching, detaching, reserved, or
    available.

    Volume attach_status will be changed to one of: attached, attaching,
    detaching, reserved, or detached.
    """

    # NOTE(jdg): This is a funky band-aid for the earlier attempts at
    # multiattach, it's a bummer because these things aren't really being used
    # but at the same time we don't want to break them until we work out the
    # new proposal for multi-attach
    # Only load basic volume info necessary to check various status and use
    # the volume row as a lock with the for_update.
    volume = _volume_get(
        context,
        volume_id,
        joined_load=False,
        for_update=True,
    )

    try:
        attachment = _attachment_get(context, attachment_id)
        attachment_updates = attachment.delete(context.session)
    except exception.VolumeAttachmentNotFound:
        attachment_updates = None

    status, attach_status = _get_statuses_from_attachments(context, volume_id)

    volume_updates = {
        'updated_at': volume.updated_at,
        'attach_status': attach_status,
    }

    # Hide volume status update to available on volume migration or upload,
    # as status is updated later on those flows.
    if (
        attach_status != 'detached'
        or (not volume.migration_status and volume.status != 'uploading')
        or volume.migration_status in ('success', 'error')
    ):
        volume_updates['status'] = status

    volume.update(volume_updates)
    volume.save(context.session)
    del volume_updates['updated_at']

    return volume_updates, attachment_updates


def _process_model_like_filter(model, query, filters):
    """Applies regex expression filtering to a query.

    :param model: model to apply filters to
    :param query: query to apply filters to
    :param filters: dictionary of filters with regex values
    :returns: the updated query.
    """
    if query is None:
        return query

    for key in sorted(filters):
        column_attr = getattr(model, key)
        if 'property' == type(column_attr).__name__:
            continue
        value = filters[key]
        if not (isinstance(value, (str, int))):
            continue
        query = query.filter(column_attr.op('LIKE')(u'%%%s%%' % value))
    return query


def apply_like_filters(model):
    def decorator_filters(process_exact_filters):
        def _decorator(query, filters):
            exact_filters = filters.copy()
            regex_filters = {}
            for key, value in filters.items():
                # NOTE(tommylikehu): For inexact match, the filter keys
                # are in the format of 'key~=value'
                if key.endswith('~'):
                    exact_filters.pop(key)
                    regex_filters[key.rstrip('~')] = value
            query = process_exact_filters(query, exact_filters)
            return _process_model_like_filter(model, query, regex_filters)

        return _decorator

    return decorator_filters


@require_context
def _volume_get_query(context, project_only=False, joined_load=True):
    """Get the query to retrieve the volume.

    :param context: the context used to run the method _volume_get_query
    :param project_only: the boolean used to decide whether to query the
        volume in the current project or all projects
    :param joined_load: the boolean used to decide whether the query loads
        the other models, which join the volume model in the database.
        Currently, the False value for this parameter is specially for the case
        of updating database during volume migration
    :returns: updated query or None
    """
    if not joined_load:
        return model_query(context, models.Volume, project_only=project_only)

    if is_admin_context(context):
        return (
            model_query(
                context,
                models.Volume,
                project_only=project_only,
            )
            .options(joinedload(models.Volume.volume_metadata))
            .options(joinedload(models.Volume.volume_admin_metadata))
            .options(joinedload(models.Volume.volume_type))
            .options(joinedload(models.Volume.volume_attachment))
            .options(joinedload(models.Volume.consistencygroup))
            .options(joinedload(models.Volume.group))
        )
    else:
        return (
            model_query(
                context,
                models.Volume,
                project_only=project_only,
            )
            .options(joinedload(models.Volume.volume_metadata))
            .options(joinedload(models.Volume.volume_type))
            .options(joinedload(models.Volume.volume_attachment))
            .options(joinedload(models.Volume.consistencygroup))
            .options(joinedload(models.Volume.group))
        )


@require_context
def _volume_get(context, volume_id, joined_load=True, for_update=False):
    result = _volume_get_query(
        context,
        project_only=True,
        joined_load=joined_load,
    )
    if joined_load:
        result = result.options(
            joinedload(models.Volume.volume_type).joinedload(
                models.VolumeType.extra_specs
            )
        )
    if for_update:
        result = result.with_for_update()
    result = result.filter_by(id=volume_id).first()

    if not result:
        raise exception.VolumeNotFound(volume_id=volume_id)

    return result


def _attachment_get_all(
    context,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    if filters and not is_valid_model_filters(
        models.VolumeAttachment,
        filters,
        exclude_list=['project_id'],
    ):
        return []

    # Generate the paginate query
    query = _generate_paginate_query(
        context,
        marker,
        limit,
        sort_keys,
        sort_dirs,
        filters,
        offset,
        models.VolumeAttachment,
    )
    if query is None:
        return []
    return query.all()


def _attachment_get(
    context,
    attachment_id,
    read_deleted=False,
    project_only=True,
):
    result = (
        model_query(
            context, models.VolumeAttachment, read_deleted=read_deleted
        )
        .filter_by(id=attachment_id)
        .options(joinedload(models.VolumeAttachment.volume))
        .first()
    )

    if not result:
        raise exception.VolumeAttachmentNotFound(
            filter='attachment_id = %s' % attachment_id,
        )
    return result


def _attachment_get_query(context, project_only=False):
    return model_query(
        context,
        models.VolumeAttachment,
        project_only=project_only,
    ).options(joinedload(models.VolumeAttachment.volume))


@apply_like_filters(model=models.VolumeAttachment)
def _process_attachment_filters(query, filters):
    if filters:
        project_id = filters.pop('project_id', None)
        # Ensure that filters' keys exist on the model
        if not is_valid_model_filters(models.VolumeAttachment, filters):
            return
        if project_id:
            volume = models.Volume
            query = query.filter(
                volume.id == models.VolumeAttachment.volume_id,
                volume.project_id == project_id,
            )

        query = query.filter_by(**filters)
    return query


@require_admin_context
@main_context_manager.reader
def volume_attachment_get_all(
    context,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    """Retrieve all Attachment records with filter and pagination options."""
    return _attachment_get_all(
        context, filters, marker, limit, offset, sort_keys, sort_dirs
    )


@require_context
@main_context_manager.reader
def volume_attachment_get_all_by_volume_id(context, volume_id):
    result = (
        model_query(context, models.VolumeAttachment)
        .filter_by(volume_id=volume_id)
        .filter(
            models.VolumeAttachment.attach_status
            != fields.VolumeAttachStatus.DETACHED
        )
        .options(joinedload(models.VolumeAttachment.volume))
        .all()
    )
    return result


# FIXME(jdg): Not using filters
@require_context
@main_context_manager.reader
def volume_attachment_get_all_by_host(context, host, filters=None):
    result = (
        model_query(context, models.VolumeAttachment)
        .filter_by(attached_host=host)
        .filter(
            models.VolumeAttachment.attach_status
            != fields.VolumeAttachStatus.DETACHED
        )
        .options(joinedload(models.VolumeAttachment.volume))
        .all()
    )
    return result


@require_context
@main_context_manager.reader
def volume_attachment_get(context, attachment_id):
    """Fetch the specified attachment record."""
    return _attachment_get(context, attachment_id)


# FIXME(jdg): Not using filters
@require_context
@main_context_manager.reader
def volume_attachment_get_all_by_instance_uuid(
    context,
    instance_uuid,
    filters=None,
):
    """Fetch all attachment records associated with the specified instance."""
    result = (
        model_query(context, models.VolumeAttachment)
        .filter_by(instance_uuid=instance_uuid)
        .filter(
            models.VolumeAttachment.attach_status
            != fields.VolumeAttachStatus.DETACHED
        )
        .options(joinedload(models.VolumeAttachment.volume))
        .all()
    )
    return result


@require_context
@main_context_manager.reader
def volume_attachment_get_all_by_project(
    context,
    project_id,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    """Retrieve all Attachment records for specific project."""
    authorize_project_context(context, project_id)
    if not filters:
        filters = {}
    else:
        filters = filters.copy()

    filters['project_id'] = project_id

    return _attachment_get_all(
        context,
        filters,
        marker,
        limit,
        offset,
        sort_keys,
        sort_dirs,
    )


@require_admin_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def attachment_destroy(context, attachment_id):
    """Destroy the specified attachment record."""
    utcnow = timeutils.utcnow()
    query = model_query(context, models.VolumeAttachment).filter_by(
        id=attachment_id
    )
    entity = query.column_descriptions[0]['entity']
    updated_values = {
        'attach_status': fields.VolumeAttachStatus.DELETED,
        'deleted': True,
        'deleted_at': utcnow,
        'updated_at': entity.updated_at,
    }
    query.update(updated_values)

    query = model_query(context, models.AttachmentSpecs).filter_by(
        attachment_id=attachment_id
    )
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': utcnow,
            'updated_at': entity.updated_at,
        }
    )
    del updated_values['updated_at']
    return updated_values


@require_context
@main_context_manager.writer
def attachment_specs_exist(context):
    query = model_query(context, models.AttachmentSpecs, read_deleted='no')
    return bool(query.first())


def _attachment_specs_query(context, attachment_id):
    return model_query(
        context, models.AttachmentSpecs, read_deleted="no"
    ).filter_by(attachment_id=attachment_id)


@require_context
@main_context_manager.reader
def attachment_specs_get(context, attachment_id):
    """DEPRECATED: Fetch the attachment_specs for the specified attachment."""
    rows = _attachment_specs_query(context, attachment_id).all()

    result = {row['key']: row['value'] for row in rows}
    return result


@require_context
@main_context_manager.writer
def attachment_specs_delete(context, attachment_id, key):
    """DEPRECATED: Delete attachment_specs for the specified attachment."""
    _attachment_specs_get_item(context, attachment_id, key)
    query = _attachment_specs_query(context, attachment_id).filter_by(key=key)
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': timeutils.utcnow(),
            'updated_at': entity.updated_at,
        }
    )


@require_context
def _attachment_specs_get_item(context, attachment_id, key):
    result = (
        _attachment_specs_query(context, attachment_id)
        .filter_by(key=key)
        .first()
    )

    if not result:
        raise exception.AttachmentSpecsNotFound(
            specs_key=key,
            attachment_id=attachment_id,
        )

    return result


@handle_db_data_error
@require_context
@main_context_manager.writer
def attachment_specs_update_or_create(context, attachment_id, specs):
    """DEPRECATED: Update attachment_specs for the specified attachment."""
    spec_ref = None
    for key, value in specs.items():
        try:
            spec_ref = _attachment_specs_get_item(context, attachment_id, key)
        except exception.AttachmentSpecsNotFound:
            spec_ref = models.AttachmentSpecs()
        spec_ref.update(
            {
                "key": key,
                "value": value,
                "attachment_id": attachment_id,
                "deleted": False,
            }
        )
        spec_ref.save(context.session)

    return specs


@require_context
@main_context_manager.reader
def volume_get(context, volume_id):
    return _volume_get(context, volume_id)


@require_admin_context
@main_context_manager.reader
def volume_get_all(
    context,
    marker=None,
    limit=None,
    sort_keys=None,
    sort_dirs=None,
    filters=None,
    offset=None,
):
    """Retrieves all volumes.

    If no sort parameters are specified then the returned volumes are sorted
    first by the 'created_at' key and then by the 'id' key in descending
    order.

    :param context: context to query under
    :param marker: the last item of the previous page, used to determine the
        next page of results to return
    :param limit: maximum number of items to return
    :param sort_keys: list of attributes by which results should be sorted,
        paired with corresponding item in sort_dirs
    :param sort_dirs: list of directions in which results should be sorted,
        paired with corresponding item in sort_keys
    :param filters: dictionary of filters; values that are in lists, tuples,
        or sets cause an 'IN' operation, while exact matching is used for other
        values, see _process_volume_filters function for more information
    :returns: list of matching volumes
    """
    # Generate the query
    query = _generate_paginate_query(
        context,
        marker,
        limit,
        sort_keys,
        sort_dirs,
        filters,
        offset,
        models.Volume,
    )
    # No volumes would match, return empty list
    if query is None:
        return []
    return query.all()


@require_context
@main_context_manager.reader
def get_volume_summary(context, project_only, filters=None):
    """Retrieves all volumes summary.

    :param context: context to query under
    :param project_only: limit summary to project volumes
    :param filters: dictionary of filters; values that are in lists, tuples,
        or sets cause an 'IN' operation, while exact matching is used for other
        values, see _process_volume_filters function for more information
    :returns: volume summary
    """
    if not (project_only or is_admin_context(context)):
        raise exception.AdminRequired()
    query = model_query(
        context,
        func.count(models.Volume.id),
        func.sum(models.Volume.size),
        read_deleted="no",
    )
    if project_only:
        query = query.filter_by(project_id=context.project_id)

    if filters:
        query = _process_volume_filters(query, filters)

    if query is None:
        return []

    result = query.first()

    query_metadata = model_query(
        context,
        models.VolumeMetadata.key,
        models.VolumeMetadata.value,
        read_deleted="no",
    )
    if project_only:
        query_metadata = query_metadata.join(
            models.Volume, models.Volume.id == models.VolumeMetadata.volume_id
        ).filter_by(project_id=context.project_id)
    result_metadata = query_metadata.distinct().all()

    result_metadata_list = collections.defaultdict(list)
    for key, value in result_metadata:
        result_metadata_list[key].append(value)

    return (result[0] or 0, result[1] or 0, result_metadata_list)


@require_admin_context
@main_context_manager.reader
def volume_get_all_by_host(context, host, filters=None):
    """Retrieves all volumes hosted on a host.

    :param context: context to query under
    :param host: host for all volumes being retrieved
    :param filters: dictionary of filters; values that are in lists, tuples,
        or sets cause an 'IN' operation, while exact matching is used for other
        values, see _process_volume_filters function for more information
    :returns: list of matching volumes
    """
    # As a side effect of the introduction of pool-aware scheduler,
    # newly created volumes will have pool information appended to
    # 'host' field of a volume record. So a volume record in DB can
    # now be either form below:
    #     Host
    #     Host#Pool
    if host and isinstance(host, str):
        host_attr = getattr(models.Volume, 'host')
        conditions = [host_attr == host, host_attr.op('LIKE')(host + '#%')]
        query = _volume_get_query(context).filter(or_(*conditions))
        if filters:
            query = _process_volume_filters(query, filters)
            # No volumes would match, return empty list
            if query is None:
                return []
        return query.all()
    elif not host:
        return []


@require_context
@main_context_manager.reader
def volume_get_all_by_group(context, group_id, filters=None):
    """Retrieves all volumes associated with the group_id.

    :param context: context to query under
    :param group_id: consistency group ID for all volumes being retrieved
    :param filters: dictionary of filters; values that are in lists, tuples,
        or sets cause an 'IN' operation, while exact matching is used for other
        values, see _process_volume_filters function for more information
    :returns: list of matching volumes
    """
    query = _volume_get_query(context).filter_by(consistencygroup_id=group_id)
    if filters:
        query = _process_volume_filters(query, filters)
        # No volumes would match, return empty list
        if query is None:
            return []
    return query.all()


@require_admin_context
@main_context_manager.writer
def volume_update_all_by_service(context):
    """Ensure volumes have the correct service_uuid value for their host.

    In some deployment tools, when performing an upgrade, all service records
    are recreated including c-vol service which gets a new record in the
    services table, though its host name is constant. Later we then delete the
    old service record.
    As a consequence, the volumes have the right host name but the service
    UUID needs to be updated to the ID of the new service record.

    :param context: context to query under
    """
    # Get all cinder-volume services
    services = service_get_all(context, binary='cinder-volume')
    for service in services:
        query = model_query(context, models.Volume)
        query = query.filter(
            _filter_host(
                models.Volume.host, service.host),
            models.Volume.service_uuid != service.uuid)
        query.update(
            {"service_uuid": service.uuid}, synchronize_session=False)


@require_context
@main_context_manager.reader
def volume_get_all_by_generic_group(context, group_id, filters=None):
    """Retrieves all volumes associated with the group_id.

    :param context: context to query under
    :param group_id: group ID for all volumes being retrieved
    :param filters: dictionary of filters; values that are in lists, tuples,
        or sets cause an 'IN' operation, while exact matching is used for other
        values, see _process_volume_filters function for more information
    :returns: list of matching volumes
    """
    query = _volume_get_query(context).filter_by(group_id=group_id)
    if filters:
        query = _process_volume_filters(query, filters)
        # No volumes would match, return empty list
        if query is None:
            return []
    return query.all()


@require_context
@main_context_manager.reader
def volume_get_all_by_project(
    context,
    project_id,
    marker,
    limit,
    sort_keys=None,
    sort_dirs=None,
    filters=None,
    offset=None,
):
    """Retrieves all volumes in a project.

    If no sort parameters are specified then the returned volumes are sorted
    first by the 'created_at' key and then by the 'id' key in descending
    order.

    :param context: context to query under
    :param project_id: project for all volumes being retrieved
    :param marker: the last item of the previous page, used to determine the
        next page of results to return
    :param limit: maximum number of items to return
    :param sort_keys: list of attributes by which results should be sorted,
        paired with corresponding item in sort_dirs
    :param sort_dirs: list of directions in which results should be sorted,
        paired with corresponding item in sort_keys
    :param filters: dictionary of filters; values that are in lists, tuples,
        or sets cause an 'IN' operation, while exact matching is used for other
        values, see _process_volume_filters function for more information
    :returns: list of matching volumes
    """
    authorize_project_context(context, project_id)
    # Add in the project filter without modifying the given filters
    filters = filters.copy() if filters else {}
    filters['project_id'] = project_id
    # Generate the query
    query = _generate_paginate_query(
        context,
        marker,
        limit,
        sort_keys,
        sort_dirs,
        filters,
        offset,
        models.Volume,
    )
    # No volumes would match, return empty list
    if query is None:
        return []
    return query.all()


def _generate_paginate_query(
    context,
    marker,
    limit,
    sort_keys,
    sort_dirs,
    filters,
    offset=None,
    paginate_type=models.Volume,
):
    """Generate the query to include the filters and the paginate options.

    Returns a query with sorting / pagination criteria added or None
    if the given filters will not yield any results.

    :param context: context to query under
    :param marker: the last item of the previous page; we returns the next
        results after this value.
    :param limit: maximum number of items to return
    :param sort_keys: list of attributes by which results should be sorted,
        paired with corresponding item in sort_dirs
    :param sort_dirs: list of directions in which results should be sorted,
        paired with corresponding item in sort_keys
    :param filters: dictionary of filters; values that are in lists, tuples,
        or sets cause an 'IN' operation, while exact matching is used for other
        values, see _process_volume_filters function for more information
    :param offset: number of items to skip
    :param paginate_type: type of pagination to generate
    :returns: updated query or None
    """
    get_query, process_filters, get = PAGINATION_HELPERS[paginate_type]

    sort_keys, sort_dirs = process_sort_params(
        sort_keys,
        sort_dirs,
        default_dir='desc',
    )
    query = get_query(context)

    if filters:
        query = process_filters(query, filters)
        if query is None:
            return None

    marker_object = None
    if marker is not None:
        marker_object = get(context, marker)

    return sqlalchemyutils.paginate_query(
        query,
        paginate_type,
        limit,
        sort_keys,
        marker=marker_object,
        sort_dirs=sort_dirs,
        offset=offset,
    )


@main_context_manager.reader
def calculate_resource_count(context, resource_type, filters):
    """Calculate total count with filters applied"""
    if resource_type not in CALCULATE_COUNT_HELPERS.keys():
        msg = _("Model %s doesn't support counting resource.")
        raise exception.InvalidInput(reason=msg % resource_type)

    get_query, process_filters = CALCULATE_COUNT_HELPERS[resource_type]
    query = get_query(context, joined_load=False)
    if filters:
        query = process_filters(query, filters)
        if query is None:
            return 0
    return query.with_entities(func.count()).scalar()


@apply_like_filters(model=models.Volume)
def _process_volume_filters(query, filters):
    """Common filter processing for Volume queries.

    Filter values that are in lists, tuples, or sets cause an 'IN' operator
    to be used, while exact matching ('==' operator) is used for other values.

    A filter key/value of 'no_migration_targets'=True causes volumes with
    either a NULL 'migration_status' or a 'migration_status' that does not
    start with 'target:' to be retrieved.

    A 'metadata' filter key must correspond to a dictionary value of metadata
    key-value pairs.

    :param query: Model query to use
    :param filters: dictionary of filters
    :returns: updated query or None
    """
    filters = filters.copy()

    # 'no_migration_targets' is unique, must be either NULL or
    # not start with 'target:'
    if filters.get('no_migration_targets', False):
        filters.pop('no_migration_targets')
        try:
            column_attr = getattr(models.Volume, 'migration_status')
            conditions = [
                column_attr == None,  # noqa
                column_attr.op('NOT LIKE')('target:%'),
            ]
            query = query.filter(or_(*conditions))
        except AttributeError:
            LOG.debug("'migration_status' column could not be found.")
            return None

    host = filters.pop('host', None)
    if host:
        query = query.filter(_filter_host(models.Volume.host, host))

    cluster_name = filters.pop('cluster_name', None)
    if cluster_name:
        query = query.filter(
            _filter_host(models.Volume.cluster_name, cluster_name),
        )

    for time_comparison_filter in ['created_at', 'updated_at']:
        if filters.get(time_comparison_filter, None):
            time_filter_dict = filters.pop(time_comparison_filter)
            try:
                query = query.filter(
                    _filter_time_comparison(
                        getattr(models.Volume, time_comparison_filter),
                        time_filter_dict,
                    ),
                )
            except AttributeError:
                LOG.debug(
                    "%s column could not be found.",
                    time_comparison_filter,
                )
                return None

    # Apply exact match filters for everything else, ensure that the
    # filter value exists on the model
    for key in filters.keys():
        # metadata/glance_metadata is unique, must be a dict
        if key in ('metadata', 'glance_metadata'):
            if not isinstance(filters[key], dict):
                LOG.debug("'%s' filter value is not valid.", key)
                return None
            continue
        try:
            column_attr = getattr(models.Volume, key)
            # Do not allow relationship properties since those require
            # schema specific knowledge
            prop = getattr(column_attr, 'property')
            if isinstance(prop, RelationshipProperty):
                LOG.debug(
                    "'%s' filter key is not valid, it maps to a relationship.",
                    key,
                )
                return None
        except AttributeError:
            LOG.debug("'%s' filter key is not valid.", key)
            return None

    # Holds the simple exact matches
    filter_dict = {}

    # Iterate over all filters, special case the filter if necessary
    for key, value in filters.items():
        if key == 'metadata':
            # model.VolumeMetadata defines the backref to Volumes as
            # 'volume_metadata' or 'volume_admin_metadata', use those as
            # column attribute keys
            col_attr = getattr(models.Volume, 'volume_metadata')
            col_ad_attr = getattr(models.Volume, 'volume_admin_metadata')
            for k, v in value.items():
                query = query.filter(
                    or_(
                        col_attr.any(key=k, value=v),
                        col_ad_attr.any(key=k, value=v),
                    )
                )
        elif key == 'glance_metadata':
            # use models.Volume.volume_glance_metadata as column attribute key.
            col_gl_attr = models.Volume.volume_glance_metadata
            for k, v in value.items():
                query = query.filter(col_gl_attr.any(key=k, value=v))
        elif isinstance(value, (list, tuple, set, frozenset)):
            # Looking for values in a list; apply to query directly
            column_attr = getattr(models.Volume, key)
            query = query.filter(column_attr.in_(value))
        else:
            # OK, simple exact match; save for later
            filter_dict[key] = value

    # Apply simple exact matches
    if filter_dict:
        query = query.filter_by(**filter_dict)
    return query


def process_sort_params(
    sort_keys,
    sort_dirs,
    default_keys=None,
    default_dir='asc',
):
    """Process the sort parameters to include default keys.

    Creates a list of sort keys and a list of sort directions. Adds the default
    keys to the end of the list if they are not already included.

    When adding the default keys to the sort keys list, the associated
    direction is:
    1) The first element in the 'sort_dirs' list (if specified), else
    2) 'default_dir' value (Note that 'asc' is the default value since this is
    the default in sqlalchemy.utils.paginate_query)

    :param sort_keys: List of sort keys to include in the processed list
    :param sort_dirs: List of sort directions to include in the processed list
    :param default_keys: List of sort keys that need to be included in the
        processed list, they are added at the end of the list if not already
        specified.
    :param default_dir: Sort direction associated with each of the default
        keys that are not supplied, used when they are added to the processed
        list
    :returns: list of sort keys, list of sort directions
    :raise exception.InvalidInput: If more sort directions than sort keys
        are specified or if an invalid sort direction is specified
    """
    if default_keys is None:
        default_keys = ['created_at', 'id']

    # Determine direction to use for when adding default keys
    if sort_dirs and len(sort_dirs):
        default_dir_value = sort_dirs[0]
    else:
        default_dir_value = default_dir

    # Create list of keys (do not modify the input list)
    if sort_keys:
        result_keys = list(sort_keys)
    else:
        result_keys = []

    # If a list of directions is not provided, use the default sort direction
    # for all provided keys.
    if sort_dirs:
        result_dirs = []
        # Verify sort direction
        for sort_dir in sort_dirs:
            if sort_dir not in ('asc', 'desc'):
                msg = _("Unknown sort direction, must be 'desc' or 'asc'.")
                raise exception.InvalidInput(reason=msg)
            result_dirs.append(sort_dir)
    else:
        result_dirs = [default_dir_value for _sort_key in result_keys]

    # Ensure that the key and direction length match
    while len(result_dirs) < len(result_keys):
        result_dirs.append(default_dir_value)
    # Unless more direction are specified, which is an error
    if len(result_dirs) > len(result_keys):
        msg = _("Sort direction array size exceeds sort key array size.")
        raise exception.InvalidInput(reason=msg)

    # Ensure defaults are included
    for key in default_keys:
        if key not in result_keys:
            result_keys.append(key)
            result_dirs.append(default_dir_value)

    return result_keys, result_dirs


@handle_db_data_error
@require_context
@main_context_manager.writer
def volume_update(context, volume_id, values):
    metadata = values.get('metadata')
    if metadata is not None:
        _volume_user_metadata_update(
            context,
            volume_id,
            values.pop('metadata'),
            delete=True,
        )

    admin_metadata = values.get('admin_metadata')
    if is_admin_context(context) and admin_metadata is not None:
        _volume_admin_metadata_update(
            context,
            volume_id,
            values.pop('admin_metadata'),
            delete=True,
        )

    query = _volume_get_query(context, joined_load=False)
    result = query.filter_by(id=volume_id).update(values)
    if not result:
        raise exception.VolumeNotFound(volume_id=volume_id)


@handle_db_data_error
@require_context
@main_context_manager.writer
def volumes_update(context, values_list):
    volume_refs = []
    for values in values_list:
        volume_id = values['id']
        values.pop('id')
        metadata = values.get('metadata')
        if metadata is not None:
            _volume_user_metadata_update(
                context,
                volume_id,
                values.pop('metadata'),
                delete=True,
            )

        admin_metadata = values.get('admin_metadata')
        if is_admin_context(context) and admin_metadata is not None:
            _volume_admin_metadata_update(
                context,
                volume_id,
                values.pop('admin_metadata'),
                delete=True,
            )

        volume_ref = _volume_get(context, volume_id)
        volume_ref.update(values)
        volume_refs.append(volume_ref)

    return volume_refs


@require_context
@main_context_manager.writer
def volume_attachment_update(context, attachment_id, values):
    query = model_query(context, models.VolumeAttachment)
    result = query.filter_by(id=attachment_id).update(values)
    if not result:
        raise exception.VolumeAttachmentNotFound(
            filter='attachment_id = ' + attachment_id
        )


@main_context_manager.writer
def volume_update_status_based_on_attachment(context, volume_id):
    """Update volume status based on attachment.

    Get volume and check if 'volume_attachment' parameter is present in volume.
    If 'volume_attachment' is None then set volume status to 'available'
    else set volume status to 'in-use'.

    :param context: context to query under
    :param volume_id: id of volume to be updated
    :returns: updated volume
    """
    volume_ref = _volume_get(context, volume_id)
    # We need to get and update volume using same session because
    # there is possibility that instance is deleted between the 'get'
    # and 'update' volume call.
    if not volume_ref['volume_attachment']:
        volume_ref.update({'status': 'available'})
    else:
        volume_ref.update({'status': 'in-use'})

    return volume_ref


def volume_has_snapshots_filter():
    return sql.exists().where(
        and_(
            models.Volume.id == models.Snapshot.volume_id,
            ~models.Snapshot.deleted,
        )
    )


def volume_has_undeletable_snapshots_filter():
    deletable_statuses = ['available', 'error']
    return sql.exists().where(
        and_(
            models.Volume.id == models.Snapshot.volume_id,
            ~models.Snapshot.deleted,
            or_(
                models.Snapshot.cgsnapshot_id != None,  # noqa: != None
                models.Snapshot.status.notin_(deletable_statuses),
            ),
            or_(
                models.Snapshot.group_snapshot_id != None,  # noqa: != None
                models.Snapshot.status.notin_(deletable_statuses),
            ),
        )
    )


def volume_has_snapshots_in_a_cgsnapshot_filter():
    return sql.exists().where(
        and_(
            models.Volume.id == models.Snapshot.volume_id,
            models.Snapshot.cgsnapshot_id.isnot(None),
        )
    )


def volume_has_attachments_filter():
    return sql.exists().where(
        and_(
            models.Volume.id == models.VolumeAttachment.volume_id,
            models.VolumeAttachment.attach_status
            != fields.VolumeAttachStatus.DETACHED,
            ~models.VolumeAttachment.deleted,
        )
    )


def volume_qos_allows_retype(new_vol_type):
    """Filter to check that qos allows retyping the volume to new_vol_type.

    Returned sqlalchemy filter will evaluate to True when volume's status is
    available or when it's 'in-use' but the qos in new_vol_type is the same as
    the qos of the volume or when it doesn't exist a consumer spec key that
    specifies anything other than the back-end in any of the 2 volume_types.
    """
    # Query to get the qos of the volume type new_vol_type
    q = (
        sql.select(models.VolumeType.qos_specs_id)
        .where(
            and_(
                ~models.VolumeType.deleted,
                models.VolumeType.id == new_vol_type,
            )
        )
        .scalar_subquery()
    )
    # Construct the filter to check qos when volume is 'in-use'
    return or_(
        # If volume is available
        models.Volume.status == 'available',
        # Or both volume types have the same qos specs
        sql.exists().where(
            and_(
                ~models.VolumeType.deleted,
                models.VolumeType.id == models.Volume.volume_type_id,
                models.VolumeType.qos_specs_id == q,
            )
        ),
        # Or they are different specs but they are handled by the backend or
        # it is not specified.  The way SQL evaluatels value != 'back-end'
        # makes it result in False not only for 'back-end' values but for
        # NULL as well, and with the double negation we ensure that we only
        # allow QoS with 'consumer' values of 'back-end' and NULL.
        and_(
            ~sql.exists().where(
                and_(
                    ~models.VolumeType.deleted,
                    models.VolumeType.id == models.Volume.volume_type_id,
                    (
                        models.VolumeType.qos_specs_id
                        == models.QualityOfServiceSpecs.specs_id
                    ),
                    models.QualityOfServiceSpecs.key == 'consumer',
                    models.QualityOfServiceSpecs.value != 'back-end',
                )
            ),
            ~sql.exists().where(
                and_(
                    ~models.VolumeType.deleted,
                    models.VolumeType.id == new_vol_type,
                    (
                        models.VolumeType.qos_specs_id
                        == models.QualityOfServiceSpecs.specs_id
                    ),
                    models.QualityOfServiceSpecs.key == 'consumer',
                    models.QualityOfServiceSpecs.value != 'back-end',
                )
            ),
        ),
    )


def volume_has_other_project_snp_filter():
    return sql.exists().where(
        and_(
            models.Volume.id == models.Snapshot.volume_id,
            models.Volume.project_id != models.Snapshot.project_id,
        )
    )


####################


def _volume_x_metadata_get_query(context, volume_id, model):
    return model_query(context, model, read_deleted="no").filter_by(
        volume_id=volume_id
    )


def _volume_x_metadata_get(context, volume_id, model):
    rows = _volume_x_metadata_get_query(context, volume_id, model).all()
    result = {}
    for row in rows:
        result[row['key']] = row['value']

    return result


def _volume_x_metadata_get_item(context, volume_id, key, model, notfound_exec):
    result = (
        _volume_x_metadata_get_query(context, volume_id, model)
        .filter_by(key=key)
        .first()
    )

    if not result:
        if model is models.VolumeGlanceMetadata:
            raise notfound_exec(id=volume_id)
        else:
            raise notfound_exec(metadata_key=key, volume_id=volume_id)
    return result


def _volume_x_metadata_update(
    context, volume_id, metadata, delete, model, add=True, update=True
):
    metadata = metadata.copy()

    # Set existing metadata to deleted if delete argument is True.  This is
    # committed immediately to the DB
    if delete:
        expected_values = {'volume_id': volume_id}
        # We don't want to delete keys we are going to update
        if metadata:
            expected_values['key'] = db.Not(metadata.keys())
        _conditional_update(
            context,
            model,
            {'deleted': True, 'deleted_at': timeutils.utcnow()},
            expected_values,
        )

    # Get existing metadata
    db_meta = _volume_x_metadata_get_query(context, volume_id, model).all()
    save = []
    skip = []

    # We only want to send changed metadata.
    for row in db_meta:
        if row.key in metadata:
            value = metadata.pop(row.key)
            if row.value != value and update:
                # ORM objects will not be saved until we do the bulk save
                row.value = value
                save.append(row)
                continue
        skip.append(row)

    # We also want to save non-existent metadata
    if add:
        save.extend(
            model(key=key, value=value, volume_id=volume_id)
            for key, value in metadata.items()
        )
    # Do a bulk save
    if save:
        context.session.bulk_save_objects(save, update_changed_only=True)

    # Construct result dictionary with current metadata
    save.extend(skip)
    result = {row['key']: row['value'] for row in save}
    return result


def _volume_user_metadata_get_query(context, volume_id):
    return _volume_x_metadata_get_query(
        context, volume_id, models.VolumeMetadata
    )


def _volume_image_metadata_get_query(context, volume_id):
    return _volume_x_metadata_get_query(
        context, volume_id, models.VolumeGlanceMetadata
    )


@require_context
def _volume_user_metadata_get(context, volume_id):
    return _volume_x_metadata_get(context, volume_id, models.VolumeMetadata)


@require_context
def _volume_user_metadata_get_item(context, volume_id, key):
    return _volume_x_metadata_get_item(
        context,
        volume_id,
        key,
        models.VolumeMetadata,
        exception.VolumeMetadataNotFound,
    )


@require_context
@require_volume_exists
def _volume_user_metadata_update(context, volume_id, metadata, delete):
    return _volume_x_metadata_update(
        context, volume_id, metadata, delete, models.VolumeMetadata
    )


@require_context
@require_volume_exists
def _volume_image_metadata_update(context, volume_id, metadata, delete):
    return _volume_x_metadata_update(
        context, volume_id, metadata, delete, models.VolumeGlanceMetadata
    )


@require_context
def _volume_glance_metadata_key_to_id(context, volume_id, key):
    db_data = volume_glance_metadata_get(context, volume_id)
    metadata = {
        meta_entry.key: meta_entry.id
        for meta_entry in db_data
        if meta_entry.key == key
    }
    metadata_id = metadata[key]
    return metadata_id


@require_context
@require_volume_exists
@main_context_manager.reader
def volume_metadata_get(context, volume_id):
    return _volume_user_metadata_get(context, volume_id)


@require_context
@require_volume_exists
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def volume_metadata_delete(
    context,
    volume_id,
    key,
    meta_type=common.METADATA_TYPES.user,
):
    if meta_type == common.METADATA_TYPES.user:
        query = _volume_user_metadata_get_query(context, volume_id).filter_by(
            key=key
        )
        entity = query.column_descriptions[0]['entity']
        query.update(
            {
                'deleted': True,
                'deleted_at': timeutils.utcnow(),
                'updated_at': entity.updated_at,
            }
        )
    elif meta_type == common.METADATA_TYPES.image:
        metadata_id = _volume_glance_metadata_key_to_id(
            context, volume_id, key
        )
        query = _volume_image_metadata_get_query(context, volume_id).filter_by(
            id=metadata_id
        )
        entity = query.column_descriptions[0]['entity']
        query.update(
            {
                'deleted': True,
                'deleted_at': timeutils.utcnow(),
                'updated_at': entity.updated_at,
            }
        )
    else:
        raise exception.InvalidMetadataType(
            metadata_type=meta_type, id=volume_id
        )


@require_context
@handle_db_data_error
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def volume_metadata_update(
    context,
    volume_id,
    metadata,
    delete,
    meta_type=common.METADATA_TYPES.user,
):
    if meta_type == common.METADATA_TYPES.user:
        return _volume_user_metadata_update(
            context, volume_id, metadata, delete
        )
    elif meta_type == common.METADATA_TYPES.image:
        return _volume_image_metadata_update(
            context, volume_id, metadata, delete
        )
    else:
        raise exception.InvalidMetadataType(
            metadata_type=meta_type, id=volume_id
        )


###################


def _volume_admin_metadata_get_query(context, volume_id):
    return _volume_x_metadata_get_query(
        context, volume_id, models.VolumeAdminMetadata
    )


@require_admin_context
@require_volume_exists
def _volume_admin_metadata_get(context, volume_id):
    return _volume_x_metadata_get(
        context, volume_id, models.VolumeAdminMetadata
    )


@require_admin_context
@require_volume_exists
def _volume_admin_metadata_update(
    context, volume_id, metadata, delete, add=True, update=True
):
    return _volume_x_metadata_update(
        context,
        volume_id,
        metadata,
        delete,
        models.VolumeAdminMetadata,
        add=add,
        update=update,
    )


@require_admin_context
@main_context_manager.reader
def volume_admin_metadata_get(context, volume_id):
    return _volume_admin_metadata_get(context, volume_id)


@require_admin_context
@require_volume_exists
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def volume_admin_metadata_delete(context, volume_id, key):
    query = _volume_admin_metadata_get_query(context, volume_id).filter_by(
        key=key
    )
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': timeutils.utcnow(),
            'updated_at': entity.updated_at,
        }
    )


@require_admin_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def volume_admin_metadata_update(
    context, volume_id, metadata, delete, add=True, update=True
):
    return _volume_admin_metadata_update(
        context, volume_id, metadata, delete, add=add, update=update
    )


###################


@require_context
@handle_db_data_error
@main_context_manager.writer
def snapshot_create(context, values):
    values['snapshot_metadata'] = _metadata_refs(
        values.get('metadata'), models.SnapshotMetadata
    )
    if not values.get('id'):
        values['id'] = str(uuid.uuid4())

    snapshot_ref = models.Snapshot()
    snapshot_ref.update(values)
    context.session.add(snapshot_ref)

    return _snapshot_get(context, values['id'])


@require_admin_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def snapshot_destroy(context, snapshot_id):
    utcnow = timeutils.utcnow()
    query = model_query(context, models.Snapshot).filter_by(id=snapshot_id)
    entity = query.column_descriptions[0]['entity']
    updated_values = {
        'status': 'deleted',
        'deleted': True,
        'deleted_at': utcnow,
        'updated_at': entity.updated_at,
    }
    query.update(updated_values)
    query = model_query(context, models.SnapshotMetadata).filter_by(
        snapshot_id=snapshot_id
    )
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': utcnow,
            'updated_at': entity.updated_at,
        }
    )
    del updated_values['updated_at']
    return updated_values


@require_context
def _snapshot_get(context, snapshot_id):
    result = (
        model_query(context, models.Snapshot, project_only=True)
        .options(joinedload(models.Snapshot.volume))
        .options(joinedload(models.Snapshot.snapshot_metadata))
        .filter_by(id=snapshot_id)
        .first()
    )

    if not result:
        raise exception.SnapshotNotFound(snapshot_id=snapshot_id)

    return result


@require_context
@main_context_manager.reader
def snapshot_get(context, snapshot_id):
    return _snapshot_get(context, snapshot_id)


@require_admin_context
@main_context_manager.reader
def snapshot_get_all(
    context,
    filters=None,
    marker=None,
    limit=None,
    sort_keys=None,
    sort_dirs=None,
    offset=None,
):
    """Retrieves all snapshots.

    If no sorting parameters are specified then returned snapshots are sorted
    first by the 'created_at' key and then by the 'id' key in descending
    order.

    :param context: context to query under
    :param filters: dictionary of filters; will do exact matching on values.
        Special keys host and cluster_name refer to the volume.
    :param marker: the last item of the previous page, used to determine the
        next page of results to return
    :param limit: maximum number of items to return
    :param sort_keys: list of attributes by which results should be sorted,
        paired with corresponding item in sort_dirs
    :param sort_dirs: list of directions in which results should be sorted,
        paired with corresponding item in sort_keys
    :returns: list of matching snapshots
    """
    if filters and not is_valid_model_filters(
        models.Snapshot,
        filters,
        exclude_list=('host', 'cluster_name', 'availability_zone'),
    ):
        return []

    query = _generate_paginate_query(
        context,
        marker,
        limit,
        sort_keys,
        sort_dirs,
        filters,
        offset,
        models.Snapshot,
    )

    # No snapshots would match, return empty list
    if not query:
        return []
    return query.all()


def _snaps_get_query(
    context,
    project_only=False,
    joined_load=True,
):
    query = model_query(context, models.Snapshot, project_only=project_only)
    if joined_load:
        query = query.options(joinedload(models.Snapshot.snapshot_metadata))
    return query


@apply_like_filters(model=models.Snapshot)
def _process_snaps_filters(query, filters):
    if filters:
        filters = filters.copy()

        exclude_list = ('host', 'cluster_name', 'availability_zone')

        # Ensure that filters' keys exist on the model or is metadata
        for key in filters.keys():
            # Ensure if filtering based on metadata filter is queried
            # then the filters value is a dictionary
            if key == 'metadata':
                if not isinstance(filters[key], dict):
                    LOG.debug("Metadata filter value is not valid dictionary")
                    return None
                continue

            if key in exclude_list:
                continue

            # for keys in filter other than metadata and exclude_list
            # ensure that the keys are in Snapshot modelt
            try:
                column_attr = getattr(models.Snapshot, key)
                prop = getattr(column_attr, 'property')
                if isinstance(prop, RelationshipProperty):
                    LOG.debug(
                        "'%s' key is not valid, it maps to a relationship.",
                        key,
                    )
                    return None
            except AttributeError:
                LOG.debug("'%s' filter key is not valid.", key)
                return None

        # filter handling for host and cluster name
        host = filters.pop('host', None)
        cluster = filters.pop('cluster_name', None)
        az = filters.pop('availability_zone', None)
        if host or cluster or az:
            query = query.join(models.Snapshot.volume)
        vol_field = models.Volume
        if host:
            query = query.filter(_filter_host(vol_field.host, host))
        if cluster:
            query = query.filter(_filter_host(vol_field.cluster_name, cluster))
        if az:
            query = query.filter_by(availability_zone=az)

        filters_dict = {}
        LOG.debug("Building query based on filter")
        for key, value in filters.items():
            if key == 'metadata':
                col_attr = getattr(models.Snapshot, 'snapshot_metadata')
                for k, v in value.items():
                    query = query.filter(col_attr.any(key=k, value=v))
            else:
                filters_dict[key] = value

        # Apply exact matches
        if filters_dict:
            query = query.filter_by(**filters_dict)

    return query


@require_context
@main_context_manager.reader
def snapshot_get_all_for_volume(context, volume_id):
    return (
        model_query(
            context, models.Snapshot, read_deleted='no', project_only=True
        )
        .filter_by(volume_id=volume_id)
        .options(joinedload(models.Snapshot.snapshot_metadata))
        .all()
    )


@require_context
@main_context_manager.reader
def snapshot_get_latest_for_volume(context, volume_id):
    result = (
        model_query(
            context, models.Snapshot, read_deleted='no', project_only=True
        )
        .filter_by(volume_id=volume_id)
        .options(joinedload(models.Snapshot.snapshot_metadata))
        .order_by(desc(models.Snapshot.created_at))
        .first()
    )
    if not result:
        raise exception.VolumeSnapshotNotFound(volume_id=volume_id)
    return result


@require_context
@main_context_manager.reader
def snapshot_get_all_by_host(context, host, filters=None):
    if filters and not is_valid_model_filters(models.Snapshot, filters):
        return []

    query = model_query(
        context, models.Snapshot, read_deleted='no', project_only=True
    )
    if filters:
        query = query.filter_by(**filters)

    # As a side effect of the introduction of pool-aware scheduler,
    # newly created volumes will have pool information appended to
    # 'host' field of a volume record. So a volume record in DB can
    # now be either form below:
    #     Host
    #     Host#Pool
    if host and isinstance(host, str):
        host_attr = getattr(models.Volume, 'host')
        conditions = [host_attr == host, host_attr.op('LIKE')(host + '#%')]
        query = (
            query.join(models.Snapshot.volume)
            .filter(or_(*conditions))
            .options(joinedload(models.Snapshot.snapshot_metadata))
        )
        return query.all()
    elif not host:
        return []


@require_context
@main_context_manager.reader
def snapshot_get_all_for_cgsnapshot(context, cgsnapshot_id):
    return (
        model_query(
            context, models.Snapshot, read_deleted='no', project_only=True
        )
        .filter_by(cgsnapshot_id=cgsnapshot_id)
        .options(joinedload(models.Snapshot.volume))
        .options(joinedload(models.Snapshot.snapshot_metadata))
        .all()
    )


@require_context
@main_context_manager.reader
def snapshot_get_all_for_group_snapshot(context, group_snapshot_id):
    return (
        model_query(
            context, models.Snapshot, read_deleted='no', project_only=True
        )
        .filter_by(group_snapshot_id=group_snapshot_id)
        .options(joinedload(models.Snapshot.volume))
        .options(joinedload(models.Snapshot.snapshot_metadata))
        .all()
    )


@require_context
@main_context_manager.reader
def snapshot_get_all_by_project(
    context,
    project_id,
    filters=None,
    marker=None,
    limit=None,
    sort_keys=None,
    sort_dirs=None,
    offset=None,
):
    """Retrieves all snapshots in a project.

    If no sorting parameters are specified then returned snapshots are sorted
    first by the 'created_at' key and then by the 'id' key in descending
    order.

    :param context: context to query under
    :param project_id: project for all snapshots being retrieved
    :param filters: dictionary of filters; will do exact matching on values
    :param marker: the last item of the previous page, used to determine the
        next page of results to return
    :param limit: maximum number of items to return
    :param sort_keys: list of attributes by which results should be sorted,
        paired with corresponding item in sort_dirs
    :param sort_dirs: list of directions in which results should be sorted,
        paired with corresponding item in sort_keys
    :returns: list of matching snapshots
    """
    if filters and not is_valid_model_filters(
        models.Snapshot,
        filters,
        exclude_list=('host', 'cluster_name', 'availability_zone'),
    ):
        return []

    authorize_project_context(context, project_id)

    # Add project_id to filters
    filters = filters.copy() if filters else {}
    filters['project_id'] = project_id

    query = _generate_paginate_query(
        context,
        marker,
        limit,
        sort_keys,
        sort_dirs,
        filters,
        offset,
        models.Snapshot,
    )

    # No snapshots would match, return empty list
    if not query:
        return []

    query = query.options(joinedload(models.Snapshot.snapshot_metadata))
    return query.all()


@require_context
def _snapshot_data_get_for_project(
    context,
    project_id,
    volume_type_id=None,
    host=None,
    skip_internal=True,
):
    authorize_project_context(context, project_id)
    query = model_query(
        context,
        func.count(models.Snapshot.id),
        func.sum(models.Snapshot.volume_size),
        read_deleted="no",
    )
    if skip_internal:
        query = query.filter(models.Snapshot.use_quota)

    if volume_type_id or host:
        query = query.join(models.Snapshot.volume)
        if volume_type_id:
            query = query.filter(
                models.Volume.volume_type_id == volume_type_id
            )
        if host:
            query = query.filter(_filter_host(models.Volume.host, host))
    result = query.filter(models.Snapshot.project_id == project_id).first()

    # NOTE(vish): convert None to 0
    return (result[0] or 0, result[1] or 0)


@require_context
@main_context_manager.reader
def snapshot_data_get_for_project(
    context, project_id, volume_type_id=None, host=None
):
    # This method doesn't support filtering temporary resources (use_quota
    # field) and defaults to returning all snapshots because all callers (quota
    # sync methods and os-host API extension) require all the snapshots.
    return _snapshot_data_get_for_project(
        context, project_id, volume_type_id, host=host, skip_internal=False
    )


@require_context
@main_context_manager.reader
def snapshot_get_all_active_by_window(
    context, begin, end=None, project_id=None
):
    """Return snapshots that were active during window."""

    query = model_query(context, models.Snapshot, read_deleted="yes")
    query = query.filter(
        or_(
            models.Snapshot.deleted_at == None,  # noqa
            models.Snapshot.deleted_at > begin,
        )
    )
    query = query.options(joinedload(models.Snapshot.volume))
    query = query.options(joinedload(models.Snapshot.snapshot_metadata))
    if end:
        query = query.filter(models.Snapshot.created_at < end)
    if project_id:
        query = query.filter_by(project_id=project_id)

    return query.all()


@handle_db_data_error
@require_context
@main_context_manager.writer
def snapshot_update(context, snapshot_id, values):
    query = model_query(context, models.Snapshot, project_only=True)
    result = query.filter_by(id=snapshot_id).update(values)
    if not result:
        raise exception.SnapshotNotFound(snapshot_id=snapshot_id)


@require_context
@main_context_manager.reader
def get_snapshot_summary(context, project_only, filters=None):
    """Retrieves all snapshots summary.

    :param context: context to query under
    :param project_only: limit summary to snapshots
    :param filters: dictionary of filters; values that are in lists, tuples,
                    or sets cause an 'IN' operation, while exact matching
                    is used for other values, see _process_snaps_filters
                    function for more information
    :returns: snapshots summary
    """

    if not (project_only or is_admin_context(context)):
        raise exception.AdminRequired()

    query = model_query(
        context,
        func.count(models.Snapshot.id),
        func.sum(models.Snapshot.volume_size),
        read_deleted="no",
    )

    if project_only:
        query = query.filter_by(project_id=context.project_id)

    if filters:
        query = _process_snaps_filters(query, filters)

    if query is None:
        return []

    result = query.first()

    return result[0] or 0, result[1] or 0


####################


def _snapshot_metadata_get_query(context, snapshot_id):
    return model_query(
        context, models.SnapshotMetadata, read_deleted="no"
    ).filter_by(snapshot_id=snapshot_id)


@require_context
def _snapshot_metadata_get(context, snapshot_id):
    rows = _snapshot_metadata_get_query(context, snapshot_id).all()
    result = {}
    for row in rows:
        result[row['key']] = row['value']

    return result


@require_context
@require_snapshot_exists
@main_context_manager.reader
def snapshot_metadata_get(context, snapshot_id):
    return _snapshot_metadata_get(context, snapshot_id)


@require_context
@require_snapshot_exists
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def snapshot_metadata_delete(context, snapshot_id, key):
    query = _snapshot_metadata_get_query(context, snapshot_id).filter_by(
        key=key
    )
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': timeutils.utcnow(),
            'updated_at': entity.updated_at,
        }
    )


@require_context
def _snapshot_metadata_get_item(context, snapshot_id, key):
    result = (
        _snapshot_metadata_get_query(context, snapshot_id)
        .filter_by(key=key)
        .first()
    )

    if not result:
        raise exception.SnapshotMetadataNotFound(
            metadata_key=key, snapshot_id=snapshot_id
        )
    return result


@require_context
@require_snapshot_exists
@handle_db_data_error
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def snapshot_metadata_update(context, snapshot_id, metadata, delete):
    # Set existing metadata to deleted if delete argument is True
    if delete:
        original_metadata = _snapshot_metadata_get(context, snapshot_id)
        for meta_key, meta_value in original_metadata.items():
            if meta_key not in metadata:
                meta_ref = _snapshot_metadata_get_item(
                    context,
                    snapshot_id,
                    meta_key,
                )
                meta_ref.update(
                    {
                        'deleted': True,
                        'deleted_at': timeutils.utcnow(),
                    }
                )
                meta_ref.save(context.session)

    meta_ref = None

    # Now update all existing items with new values, or create new meta
    # objects
    for meta_key, meta_value in metadata.items():

        # update the value whether it exists or not
        item = {"value": meta_value}

        try:
            meta_ref = _snapshot_metadata_get_item(
                context, snapshot_id, meta_key
            )
        except exception.SnapshotMetadataNotFound:
            meta_ref = models.SnapshotMetadata()
            item.update({"key": meta_key, "snapshot_id": snapshot_id})

        meta_ref.update(item)
        meta_ref.save(context.session)

    return _snapshot_metadata_get(context, snapshot_id)


###################


@handle_db_data_error
@require_admin_context
@main_context_manager.writer
def volume_type_create(context, values, projects=None):
    """Create a new volume type.

    In order to pass in extra specs, the values dict should contain a
    'extra_specs' key/value pair:
    {'extra_specs' : {'k1': 'v1', 'k2': 'v2', ...}}
    """
    if not values.get('id'):
        values['id'] = str(uuid.uuid4())

    projects = projects or []
    orm_projects = []

    try:
        _volume_type_get_by_name(context, values['name'])
        raise exception.VolumeTypeExists(id=values['name'])
    except exception.VolumeTypeNotFoundByName:
        pass

    try:
        _volume_type_get(context, values['id'])
        raise exception.VolumeTypeExists(id=values['id'])
    except exception.VolumeTypeNotFound:
        pass

    try:
        values['extra_specs'] = _metadata_refs(
            values.get('extra_specs'),
            models.VolumeTypeExtraSpecs,
        )
        volume_type_ref = models.VolumeType()
        volume_type_ref.update(values)
        context.session.add(volume_type_ref)
    except Exception as e:
        raise db_exc.DBError(e)

    for project in set(projects):
        access_ref = models.VolumeTypeProjects()
        access_ref.update(
            {
                "volume_type_id": volume_type_ref.id,
                "project_id": project,
            }
        )
        access_ref.save(context.session)
        orm_projects.append(access_ref)

    volume_type_ref.projects = orm_projects

    return volume_type_ref


@handle_db_data_error
@require_admin_context
@main_context_manager.writer
def group_type_create(context, values, projects=None):
    """Create a new group type.

    In order to pass in group specs, the values dict should contain a
    'group_specs' key/value pair:
    {'group_specs' : {'k1': 'v1', 'k2': 'v2', ...}}
    """
    if not values.get('id'):
        values['id'] = str(uuid.uuid4())

    projects = projects or []

    try:
        _group_type_get_by_name(context, values['name'])
        raise exception.GroupTypeExists(id=values['name'])
    except exception.GroupTypeNotFoundByName:
        pass

    try:
        _group_type_get(context, values['id'])
        raise exception.GroupTypeExists(id=values['id'])
    except exception.GroupTypeNotFound:
        pass

    try:
        values['group_specs'] = _metadata_refs(
            values.get('group_specs'), models.GroupTypeSpecs
        )
        group_type_ref = models.GroupType()
        group_type_ref.update(values)
        context.session.add(group_type_ref)
    except Exception as e:
        raise db_exc.DBError(e)

    for project in set(projects):
        access_ref = models.GroupTypeProjects()
        access_ref.update(
            {
                "group_type_id": group_type_ref.id,
                "project_id": project,
            }
        )
        access_ref.save(context.session)

    return group_type_ref


def _volume_type_get_query(context, read_deleted='no', expected_fields=None):
    expected_fields = expected_fields or []
    query = model_query(
        context, models.VolumeType, read_deleted=read_deleted
    ).options(joinedload(models.VolumeType.extra_specs))

    for expected in expected_fields:
        query = query.options(joinedload(getattr(models.VolumeType, expected)))

    if not context.is_admin:
        the_filter = [models.VolumeType.is_public == true()]
        projects_attr = getattr(models.VolumeType, 'projects')
        the_filter.extend([projects_attr.any(project_id=context.project_id)])
        query = query.filter(or_(*the_filter))

    return query


def _group_type_get_query(context, read_deleted='no', expected_fields=None):
    expected_fields = expected_fields or []
    query = model_query(
        context, models.GroupType, read_deleted=read_deleted
    ).options(joinedload(models.GroupType.group_specs))

    if 'projects' in expected_fields:
        query = query.options(joinedload(models.GroupType.projects))

    if not context.is_admin:
        the_filter = [models.GroupType.is_public == true()]
        projects_attr = models.GroupType.projects
        the_filter.extend([projects_attr.any(project_id=context.project_id)])
        query = query.filter(or_(*the_filter))

    return query


def _process_volume_types_filters(query, filters):
    context = filters.pop('context', None)
    if 'is_public' in filters and filters['is_public'] is not None:
        the_filter = [models.VolumeType.is_public == filters['is_public']]
        if filters['is_public'] and context.project_id is not None:
            projects_attr = getattr(models.VolumeType, 'projects')
            the_filter.extend(
                [projects_attr.any(project_id=context.project_id, deleted=0)]
            )
        if len(the_filter) > 1:
            query = query.filter(or_(*the_filter))
        else:
            query = query.filter(the_filter[0])
    if 'is_public' in filters:
        del filters['is_public']
    if filters:
        # Ensure that filters' keys exist on the model
        if not is_valid_model_filters(models.VolumeType, filters):
            return
        if filters.get('extra_specs') is not None:
            the_filter = []
            searchdict = filters.pop('extra_specs')
            extra_specs = getattr(models.VolumeType, 'extra_specs')
            for k, v in searchdict.items():
                # NOTE(tommylikehu): We will use 'LIKE' operator for
                # 'availability_zones' extra spec as it always store the
                # AZ list info within the format: "az1, az2,...."
                if k == 'RESKEY:availability_zones':
                    the_filter.extend(
                        [
                            extra_specs.any(
                                models.VolumeTypeExtraSpecs.value.like(
                                    u'%%%s%%' % v
                                ),
                                key=k,
                                deleted=False,
                            )
                        ]
                    )
                else:
                    the_filter.extend(
                        [extra_specs.any(key=k, value=v, deleted=False)]
                    )
            if len(the_filter) > 1:
                query = query.filter(and_(*the_filter))
            else:
                query = query.filter(the_filter[0])
        query = query.filter_by(**filters)
    return query


def _process_group_types_filters(query, filters):
    context = filters.pop('context', None)
    if 'is_public' in filters and filters['is_public'] is not None:
        the_filter = [models.GroupType.is_public == filters['is_public']]
        if filters['is_public'] and context.project_id is not None:
            projects_attr = getattr(models.GroupType, 'projects')
            the_filter.extend(
                [
                    projects_attr.any(
                        project_id=context.project_id, deleted=False
                    )
                ]
            )
        if len(the_filter) > 1:
            query = query.filter(or_(*the_filter))
        else:
            query = query.filter(the_filter[0])
    if 'is_public' in filters:
        del filters['is_public']
    if filters:
        # Ensure that filters' keys exist on the model
        if not is_valid_model_filters(models.GroupType, filters):
            return
        if filters.get('group_specs') is not None:
            the_filter = []
            searchdict = filters.pop('group_specs')
            group_specs = getattr(models.GroupType, 'group_specs')
            for k, v in searchdict.items():
                the_filter.extend(
                    [group_specs.any(key=k, value=v, deleted=False)]
                )
            if len(the_filter) > 1:
                query = query.filter(and_(*the_filter))
            else:
                query = query.filter(the_filter[0])
        query = query.filter_by(**filters)
    return query


@handle_db_data_error
@require_admin_context
def _type_update(context, type_id, values, is_group):
    if is_group:
        model = models.GroupType
        exists_exc = exception.GroupTypeExists
    else:
        model = models.VolumeType
        exists_exc = exception.VolumeTypeExists

    # No description change
    if values['description'] is None:
        del values['description']

    # No is_public change
    if values['is_public'] is None:
        del values['is_public']

    # No name change
    if values['name'] is None:
        del values['name']
    else:
        # Group type name is unique. If change to a name that belongs to
        # a different group_type, it should be prevented.
        conditions = and_(
            model.name == values['name'], model.id != type_id, ~model.deleted
        )
        query = context.session.query(sql.exists().where(conditions))
        if query.scalar():
            raise exists_exc(id=values['name'])

    query = model_query(context, model, project_only=True)
    result = query.filter_by(id=type_id).update(values)
    if not result:
        if is_group:
            raise exception.GroupTypeNotFound(group_type_id=type_id)
        else:
            raise exception.VolumeTypeNotFound(volume_type_id=type_id)


@main_context_manager.writer
def volume_type_update(context, volume_type_id, values):
    _type_update(context, volume_type_id, values, is_group=False)


@main_context_manager.writer
def group_type_update(context, group_type_id, values):
    _type_update(context, group_type_id, values, is_group=True)


@require_context
@main_context_manager.reader
def volume_type_get_all(
    context,
    inactive=False,
    filters=None,
    marker=None,
    limit=None,
    sort_keys=None,
    sort_dirs=None,
    offset=None,
    list_result=False,
):
    """Returns a dict describing all volume_types with name as key.

    If no sort parameters are specified then the returned volume types are
    sorted first by the 'created_at' key and then by the 'id' key in descending
    order.

    :param context: context to query under
    :param marker: the last item of the previous page, used to determine the
                   next page of results to return
    :param limit: maximum number of items to return
    :param sort_keys: list of attributes by which results should be sorted,
                      paired with corresponding item in sort_dirs
    :param sort_dirs: list of directions in which results should be sorted,
                      paired with corresponding item in sort_keys
    :param filters: dictionary of filters; values that are in lists, tuples,
                    or sets cause an 'IN' operation, while exact matching
                    is used for other values, see _process_volume_type_filters
                    function for more information
    :param list_result: For compatibility, if list_result = True, return a list
                        instead of dict.
    :returns: list/dict of matching volume types
    """
    # Add context for _process_volume_types_filters
    filters = filters or {}
    filters['context'] = context
    # Generate the query
    query = _generate_paginate_query(
        context,
        marker,
        limit,
        sort_keys,
        sort_dirs,
        filters,
        offset,
        models.VolumeType,
    )
    # No volume types would match, return empty dict or list
    if query is None:
        if list_result:
            return []
        return {}

    rows = query.all()

    if list_result:
        result = [
            _dict_with_extra_specs_if_authorized(context, row) for row in rows
        ]
        return result

    result = {
        row['name']: _dict_with_extra_specs_if_authorized(context, row)
        for row in rows
    }
    return result


@require_context
@main_context_manager.reader
def group_type_get_all(
    context,
    inactive=False,
    filters=None,
    marker=None,
    limit=None,
    sort_keys=None,
    sort_dirs=None,
    offset=None,
    list_result=False,
):
    """Returns a dict describing all group_types with name as key.

    If no sort parameters are specified then the returned group types are
    sorted first by the 'created_at' key and then by the 'id' key in descending
    order.

    :param context: context to query under
    :param marker: the last item of the previous page, used to determine the
        next page of results to return
    :param limit: maximum number of items to return
    :param sort_keys: list of attributes by which results should be sorted,
        paired with corresponding item in sort_dirs
    :param sort_dirs: list of directions in which results should be sorted,
        paired with corresponding item in sort_keys
    :param filters: dictionary of filters; values that are in lists, tuples,
        or sets cause an 'IN' operation, while exact matching is used for other
        values, see _process_volume_type_filters function for more information
    :param list_result: For compatibility, if list_result = True, return a list
        instead of dict.
    :returns: list/dict of matching group types
    """
    # Add context for _process_group_types_filters
    filters = filters or {}
    filters['context'] = context
    # Generate the query
    query = _generate_paginate_query(
        context,
        marker,
        limit,
        sort_keys,
        sort_dirs,
        filters,
        offset,
        models.GroupType,
    )
    # No group types would match, return empty dict or list
    if query is None:
        if list_result:
            return []
        return {}

    rows = query.all()
    if list_result:
        result = [
            _dict_with_group_specs_if_authorized(context, row) for row in rows
        ]
        return result
    result = {
        row['name']: _dict_with_group_specs_if_authorized(context, row)
        for row in rows
    }
    return result


def _volume_type_get_id_from_volume_type_query(context, id):
    return model_query(
        context,
        models.VolumeType.id,
        read_deleted="no",
        base_model=models.VolumeType,
    ).filter_by(id=id)


def _group_type_get_id_from_group_type_query(context, id):
    return model_query(
        context,
        models.GroupType.id,
        read_deleted="no",
        base_model=models.GroupType,
    ).filter_by(id=id)


def _volume_type_get_id_from_volume_type(context, id):
    result = _volume_type_get_id_from_volume_type_query(context, id).first()
    if not result:
        raise exception.VolumeTypeNotFound(volume_type_id=id)
    return result[0]


def _group_type_get_id_from_group_type(context, id):
    result = _group_type_get_id_from_group_type_query(context, id).first()
    if not result:
        raise exception.GroupTypeNotFound(group_type_id=id)
    return result[0]


def _volume_type_get_db_object(
    context, id, inactive=False, expected_fields=None
):
    read_deleted = "yes" if inactive else "no"
    result = (
        _volume_type_get_query(context, read_deleted, expected_fields)
        .filter_by(id=id)
        .first()
    )
    return result


def _group_type_get_db_object(
    context,
    id,
    inactive=False,
    expected_fields=None,
):
    read_deleted = "yes" if inactive else "no"
    result = (
        _group_type_get_query(context, read_deleted, expected_fields)
        .filter_by(id=id)
        .first()
    )
    return result


@require_context
def _volume_type_get(context, id, inactive=False, expected_fields=None):
    expected_fields = expected_fields or []
    result = _volume_type_get_db_object(
        context,
        id,
        inactive,
        expected_fields,
    )
    if not result:
        raise exception.VolumeTypeNotFound(volume_type_id=id)

    vtype = _dict_with_extra_specs_if_authorized(context, result)

    if 'projects' in expected_fields:
        vtype['projects'] = [p['project_id'] for p in result['projects']]

    if 'qos_specs' in expected_fields:
        vtype['qos_specs'] = result.qos_specs

    return vtype


@require_context
def _group_type_get(context, id, inactive=False, expected_fields=None):
    expected_fields = expected_fields or []
    result = _group_type_get_db_object(context, id, inactive, expected_fields)
    if not result:
        raise exception.GroupTypeNotFound(group_type_id=id)

    gtype = _dict_with_group_specs_if_authorized(context, result)

    if 'projects' in expected_fields:
        gtype['projects'] = [p['project_id'] for p in result['projects']]

    return gtype


@require_context
@main_context_manager.reader
def volume_type_get(context, id, inactive=False, expected_fields=None):
    """Get volume type by id.

    :param context: context to query under
    :param id: Volume type id to get.
    :param inactive: Consider inactive volume types when searching
    :param expected_fields: Return those additional fields.
        Supported fields are: projects.
    :returns: volume type
    """

    return _volume_type_get(
        context, id, inactive=inactive, expected_fields=expected_fields
    )


@require_context
@main_context_manager.reader
def group_type_get(context, id, inactive=False, expected_fields=None):
    """Return a dict describing specific group_type."""

    return _group_type_get(
        context, id, inactive=inactive, expected_fields=expected_fields
    )


def _volume_type_get_full(context, id):
    """Return dict for a specific volume_type with extra_specs and projects."""
    return _volume_type_get(
        context,
        id,
        inactive=False,
        expected_fields=('extra_specs', 'projects'),
    )


def _group_type_get_full(context, id):
    """Return dict for a specific group_type with group_specs and projects."""
    return _group_type_get(
        context,
        id,
        inactive=False,
        expected_fields=('group_specs', 'projects'),
    )


@require_context
def _volume_type_ref_get(context, id, inactive=False):
    read_deleted = "yes" if inactive else "no"
    result = (
        model_query(context, models.VolumeType, read_deleted=read_deleted)
        .options(joinedload(models.VolumeType.extra_specs))
        .filter_by(id=id)
        .first()
    )

    if not result:
        raise exception.VolumeTypeNotFound(volume_type_id=id)

    return result


@require_context
def _group_type_ref_get(context, id, inactive=False):
    read_deleted = "yes" if inactive else "no"
    result = (
        model_query(context, models.GroupType, read_deleted=read_deleted)
        .options(joinedload(models.GroupType.group_specs))
        .filter_by(id=id)
        .first()
    )

    if not result:
        raise exception.GroupTypeNotFound(group_type_id=id)

    return result


@require_context
def _volume_type_get_by_name(context, name):
    result = (
        model_query(context, models.VolumeType)
        .options(joinedload(models.VolumeType.extra_specs))
        .filter_by(name=name)
        .first()
    )

    if not result:
        raise exception.VolumeTypeNotFoundByName(volume_type_name=name)

    return _dict_with_extra_specs_if_authorized(context, result)


@require_context
def _group_type_get_by_name(context, name):
    result = (
        model_query(context, models.GroupType)
        .options(joinedload(models.GroupType.group_specs))
        .filter_by(name=name)
        .first()
    )

    if not result:
        raise exception.GroupTypeNotFoundByName(group_type_name=name)

    return _dict_with_group_specs_if_authorized(context, result)


@require_context
@main_context_manager.reader
def volume_type_get_by_name(context, name):
    """Return a dict describing specific volume_type."""

    return _volume_type_get_by_name(context, name)


@require_context
@main_context_manager.reader
def group_type_get_by_name(context, name):
    """Return a dict describing specific group_type."""

    return _group_type_get_by_name(context, name)


@require_context
@main_context_manager.reader
def volume_types_get_by_name_or_id(context, volume_type_list):
    """Return a dict describing specific volume_type."""
    req_volume_types = []
    for vol_t in volume_type_list:
        if not uuidutils.is_uuid_like(vol_t):
            vol_type = _volume_type_get_by_name(context, vol_t)
        else:
            try:
                vol_type = _volume_type_get(context, vol_t)
            except exception.VolumeTypeNotFound:
                # check again if we get this volume type by uuid-like name
                try:
                    vol_type = _volume_type_get_by_name(context, vol_t)
                except exception.VolumeTypeNotFoundByName:
                    raise exception.VolumeTypeNotFound(volume_type_id=vol_t)
        req_volume_types.append(vol_type)
    return req_volume_types


@require_context
@main_context_manager.reader
def group_types_get_by_name_or_id(context, group_type_list):
    """Return a dict describing specific group_type."""
    req_group_types = []
    for grp_t in group_type_list:
        if not uuidutils.is_uuid_like(grp_t):
            grp_type = _group_type_get_by_name(context, grp_t)
        else:
            grp_type = _group_type_get(context, grp_t)
        req_group_types.append(grp_type)
    return req_group_types


@require_admin_context
@require_qos_specs_exists
@main_context_manager.reader
def volume_type_qos_associations_get(context, qos_specs_id, inactive=False):
    read_deleted = "yes" if inactive else "no"
    vts = (
        model_query(context, models.VolumeType, read_deleted=read_deleted)
        .options(joinedload(models.VolumeType.extra_specs))
        .options(joinedload(models.VolumeType.projects))
        .filter_by(qos_specs_id=qos_specs_id)
        .all()
    )
    return vts


@require_admin_context
@main_context_manager.writer
def volume_type_qos_associate(context, type_id, qos_specs_id):
    _volume_type_get(context, type_id)

    context.session.query(models.VolumeType).filter_by(id=type_id).update(
        {'qos_specs_id': qos_specs_id, 'updated_at': timeutils.utcnow()}
    )


@require_admin_context
@main_context_manager.writer
def volume_type_qos_disassociate(context, qos_specs_id, type_id):
    """Disassociate volume type from qos specs."""
    _volume_type_get(context, type_id)

    context.session.query(models.VolumeType).filter_by(id=type_id).filter_by(
        qos_specs_id=qos_specs_id
    ).update({'qos_specs_id': None, 'updated_at': timeutils.utcnow()})


@require_admin_context
@main_context_manager.writer
def volume_type_qos_disassociate_all(context, qos_specs_id):
    """Disassociate all volume types associated with specified qos specs."""
    context.session.query(models.VolumeType).filter_by(
        qos_specs_id=qos_specs_id
    ).update({'qos_specs_id': None, 'updated_at': timeutils.utcnow()})


@require_admin_context
@main_context_manager.reader
def volume_type_qos_specs_get(context, type_id):
    """Return all qos specs for given volume type.

    result looks like:
        {
         'qos_specs':
                     {
                        'id': 'qos-specs-id',
                        'name': 'qos_specs_name',
                        'consumer': 'Consumer',
                        'specs': {
                            'key1': 'value1',
                            'key2': 'value2',
                            'key3': 'value3'
                        }
                     }
        }

    """
    _volume_type_get(context, type_id, context.session)

    row = (
        context.session.query(models.VolumeType)
        .options(joinedload(models.VolumeType.qos_specs))
        .filter_by(id=type_id)
        .first()
    )

    # row.qos_specs is a list of QualityOfServiceSpecs ref
    specs = _dict_with_qos_specs(row.qos_specs)

    if not specs:
        # turn empty list to None
        specs = None
    else:
        specs = specs[0]

    return {'qos_specs': specs}


@require_admin_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def volume_type_destroy(context, type_id):
    utcnow = timeutils.utcnow()

    vol_types = volume_type_get_all(context)
    if len(vol_types) <= 1:
        raise exception.VolumeTypeDeletionError(volume_type_id=type_id)
    _volume_type_get(context, type_id)
    results = (
        model_query(context, models.Volume)
        .filter_by(volume_type_id=type_id)
        .all()
    )

    group_count = (
        model_query(
            context,
            models.GroupVolumeTypeMapping,
            read_deleted="no",
        )
        .filter_by(volume_type_id=type_id)
        .count()
    )
    cg_count = (
        model_query(
            context,
            models.ConsistencyGroup,
        )
        .filter(models.ConsistencyGroup.volume_type_id.contains(type_id))
        .count()
    )
    if results or group_count or cg_count:
        LOG.error('VolumeType %s deletion failed, VolumeType in use.', type_id)
        raise exception.VolumeTypeInUse(volume_type_id=type_id)

    query = model_query(context, models.VolumeType).filter_by(id=type_id)
    entity = query.column_descriptions[0]['entity']
    updated_values = {
        'deleted': True,
        'deleted_at': utcnow,
        'updated_at': entity.updated_at,
    }
    query.update(updated_values)

    query = model_query(
        context,
        models.VolumeTypeExtraSpecs,
    ).filter_by(volume_type_id=type_id)
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': utcnow,
            'updated_at': entity.updated_at,
        }
    )

    query = model_query(context, models.Encryption).filter_by(
        volume_type_id=type_id
    )
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': utcnow,
            'updated_at': entity.updated_at,
        }
    )

    model_query(
        context, models.VolumeTypeProjects, read_deleted="int_no"
    ).filter_by(volume_type_id=type_id).soft_delete(synchronize_session=False)
    del updated_values['updated_at']
    return updated_values


@require_admin_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def group_type_destroy(context, type_id):
    _group_type_get(context, type_id)
    results = (
        model_query(context, models.Group)
        .filter_by(group_type_id=type_id)
        .all()
    )
    if results:
        LOG.error('GroupType %s deletion failed, GroupType in use.', type_id)
        raise exception.GroupTypeInUse(group_type_id=type_id)

    query = model_query(context, models.GroupType).filter_by(id=type_id)
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': timeutils.utcnow(),
            'updated_at': entity.updated_at,
        }
    )

    query = model_query(context, models.GroupTypeSpecs).filter_by(
        group_type_id=type_id
    )
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': timeutils.utcnow(),
            'updated_at': entity.updated_at,
        }
    )


@require_context
@main_context_manager.reader
def volume_get_all_active_by_window(context, begin, end=None, project_id=None):
    """Return volumes that were active during window."""
    query = model_query(context, models.Volume, read_deleted="yes")
    query = query.filter(
        or_(
            models.Volume.deleted_at == None,  # noqa
            models.Volume.deleted_at > begin,
        )
    )
    if end:
        query = query.filter(models.Volume.created_at < end)
    if project_id:
        query = query.filter_by(project_id=project_id)

    query = (
        query.options(joinedload(models.Volume.volume_metadata))
        .options(joinedload(models.Volume.volume_type))
        .options(joinedload(models.Volume.volume_attachment))
        .options(joinedload(models.Volume.consistencygroup))
        .options(joinedload(models.Volume.group))
    )

    if is_admin_context(context):
        query = query.options(joinedload(models.Volume.volume_admin_metadata))

    return query.all()


def _volume_type_access_query(context):
    return model_query(
        context, models.VolumeTypeProjects, read_deleted="int_no"
    )


def _group_type_access_query(context):
    return model_query(context, models.GroupTypeProjects, read_deleted="no")


@require_admin_context
@main_context_manager.reader
def volume_type_access_get_all(context, type_id):
    volume_type_id = _volume_type_get_id_from_volume_type(context, type_id)
    return (
        _volume_type_access_query(context)
        .filter_by(volume_type_id=volume_type_id)
        .all()
    )


@require_admin_context
@main_context_manager.reader
def group_type_access_get_all(context, type_id):
    group_type_id = _group_type_get_id_from_group_type(context, type_id)
    return (
        _group_type_access_query(context)
        .filter_by(group_type_id=group_type_id)
        .all()
    )


def _group_volume_type_mapping_query(context):
    return model_query(
        context, models.GroupVolumeTypeMapping, read_deleted="no"
    )


@require_admin_context
@main_context_manager.reader
def volume_type_get_all_by_group(context, group_id):
    # Generic volume group
    mappings = (
        _group_volume_type_mapping_query(context)
        .filter_by(group_id=group_id)
        .all()
    )
    volume_type_ids = [mapping.volume_type_id for mapping in mappings]
    query = (
        model_query(context, models.VolumeType, read_deleted='no')
        .filter(models.VolumeType.id.in_(volume_type_ids))
        .options(joinedload(models.VolumeType.extra_specs))
        .options(joinedload(models.VolumeType.projects))
        .all()
    )
    return query


def _group_volume_type_mapping_get_all_by_group_volume_type(
    context, group_id, volume_type_id
):
    mappings = (
        _group_volume_type_mapping_query(context)
        .filter_by(group_id=group_id)
        .filter_by(volume_type_id=volume_type_id)
        .all()
    )
    return mappings


@require_admin_context
@main_context_manager.writer
def volume_type_access_add(context, type_id, project_id):
    """Add given tenant to the volume type access list."""
    volume_type_id = _volume_type_get_id_from_volume_type(context, type_id)

    access_ref = models.VolumeTypeProjects()
    access_ref.update(
        {"volume_type_id": volume_type_id, "project_id": project_id}
    )

    try:
        access_ref.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.VolumeTypeAccessExists(
            volume_type_id=type_id, project_id=project_id
        )
    return access_ref


@require_admin_context
@main_context_manager.writer
def group_type_access_add(context, type_id, project_id):
    """Add given tenant to the group type access list."""
    group_type_id = _group_type_get_id_from_group_type(context, type_id)

    access_ref = models.GroupTypeProjects()
    access_ref.update(
        {"group_type_id": group_type_id, "project_id": project_id}
    )

    try:
        access_ref.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.GroupTypeAccessExists(
            group_type_id=type_id, project_id=project_id
        )
    return access_ref


@require_admin_context
@main_context_manager.writer
def volume_type_access_remove(context, type_id, project_id):
    """Remove given tenant from the volume type access list."""
    volume_type_id = _volume_type_get_id_from_volume_type(context, type_id)

    count = (
        _volume_type_access_query(context)
        .filter_by(volume_type_id=volume_type_id)
        .filter_by(project_id=project_id)
        .soft_delete(synchronize_session=False)
    )
    if count == 0:
        raise exception.VolumeTypeAccessNotFound(
            volume_type_id=type_id, project_id=project_id
        )


def _project_default_volume_type_get(context, project_id=None):
    """Get default volume type(s) for a project(s)

    If a project id is passed, it returns default type for that particular
    project else returns default volume types for all projects
    """
    if project_id:
        return (
            model_query(context, models.DefaultVolumeTypes)
            .filter_by(project_id=project_id)
            .first()
        )
    return model_query(context, models.DefaultVolumeTypes).all()


@main_context_manager.reader
def project_default_volume_type_get(context, project_id=None):
    """Get default volume type(s) for a project(s)

    If a project id is passed, it returns default type for that particular
    project else returns default volume types for all projects
    """
    return _project_default_volume_type_get(context, project_id)


@main_context_manager.writer
def project_default_volume_type_set(context, volume_type_id, project_id):
    """Set default volume type for a project"""

    update_default = _project_default_volume_type_get(context, project_id)
    if update_default:
        LOG.info("Updating default type for project %s", project_id)
        update_default.volume_type_id = volume_type_id
        return update_default

    access_ref = models.DefaultVolumeTypes(
        volume_type_id=volume_type_id, project_id=project_id
    )
    access_ref.save(context.session)
    return access_ref


@main_context_manager.reader
def get_all_projects_with_default_type(context, volume_type_id):
    """Get all projects with volume_type_id as their default type"""
    return (
        model_query(context, models.DefaultVolumeTypes)
        .filter_by(volume_type_id=volume_type_id)
        .all()
    )


@main_context_manager.writer
def project_default_volume_type_unset(context, project_id):
    """Unset default volume type for a project (hard delete)"""

    model_query(context, models.DefaultVolumeTypes).filter_by(
        project_id=project_id
    ).delete()


@require_admin_context
@main_context_manager.writer
def group_type_access_remove(context, type_id, project_id):
    """Remove given tenant from the group type access list."""
    group_type_id = _group_type_get_id_from_group_type(context, type_id)

    count = (
        _group_type_access_query(context)
        .filter_by(group_type_id=group_type_id)
        .filter_by(project_id=project_id)
        .soft_delete(synchronize_session=False)
    )
    if count == 0:
        raise exception.GroupTypeAccessNotFound(
            group_type_id=type_id, project_id=project_id
        )


####################


def _volume_type_extra_specs_query(context, volume_type_id):
    return model_query(
        context,
        models.VolumeTypeExtraSpecs,
        read_deleted="no",
    ).filter_by(volume_type_id=volume_type_id)


@require_context
@main_context_manager.reader
def volume_type_extra_specs_get(context, volume_type_id):
    rows = _volume_type_extra_specs_query(context, volume_type_id).all()

    result = {}
    for row in rows:
        result[row['key']] = row['value']

    return result


@require_context
@main_context_manager.writer
def volume_type_extra_specs_delete(context, volume_type_id, key):
    _volume_type_extra_specs_get_item(context, volume_type_id, key)
    query = _volume_type_extra_specs_query(
        context,
        volume_type_id,
    ).filter_by(key=key)
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': timeutils.utcnow(),
            'updated_at': entity.updated_at,
        },
    )


@require_context
def _volume_type_extra_specs_get_item(context, volume_type_id, key):
    result = (
        _volume_type_extra_specs_query(context, volume_type_id)
        .filter_by(key=key)
        .first()
    )

    if not result:
        raise exception.VolumeTypeExtraSpecsNotFound(
            extra_specs_key=key,
            volume_type_id=volume_type_id,
        )

    return result


@handle_db_data_error
@require_context
@main_context_manager.writer
def volume_type_extra_specs_update_or_create(
    context,
    volume_type_id,
    extra_specs,
):
    spec_ref = None
    for key, value in extra_specs.items():
        try:
            spec_ref = _volume_type_extra_specs_get_item(
                context,
                volume_type_id,
                key,
            )
        except exception.VolumeTypeExtraSpecsNotFound:
            spec_ref = models.VolumeTypeExtraSpecs()

        spec_ref.update(
            {
                "key": key,
                "value": value,
                "volume_type_id": volume_type_id,
                "deleted": False,
            },
        )
        spec_ref.save(context.session)

    return extra_specs


####################


def _group_type_specs_query(context, group_type_id):
    return model_query(
        context,
        models.GroupTypeSpecs,
        read_deleted="no",
    ).filter_by(group_type_id=group_type_id)


@require_context
@main_context_manager.reader
def group_type_specs_get(context, group_type_id):
    rows = _group_type_specs_query(context, group_type_id).all()

    result = {}
    for row in rows:
        result[row['key']] = row['value']

    return result


@require_context
@main_context_manager.writer
def group_type_specs_delete(context, group_type_id, key):
    _group_type_specs_get_item(context, group_type_id, key)
    query = _group_type_specs_query(context, group_type_id).filter_by(key=key)
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': timeutils.utcnow(),
            'updated_at': entity.updated_at,
        },
    )


@require_context
def _group_type_specs_get_item(context, group_type_id, key):
    result = (
        _group_type_specs_query(context, group_type_id)
        .filter_by(key=key)
        .first()
    )

    if not result:
        raise exception.GroupTypeSpecsNotFound(
            group_specs_key=key,
            group_type_id=group_type_id,
        )

    return result


@handle_db_data_error
@require_context
@main_context_manager.writer
def group_type_specs_update_or_create(context, group_type_id, group_specs):
    spec_ref = None
    for key, value in group_specs.items():
        try:
            spec_ref = _group_type_specs_get_item(context, group_type_id, key)
        except exception.GroupTypeSpecsNotFound:
            spec_ref = models.GroupTypeSpecs()
        spec_ref.update(
            {
                "key": key,
                "value": value,
                "group_type_id": group_type_id,
                "deleted": False,
            },
        )
        spec_ref.save(context.session)

    return group_specs


####################


@require_admin_context
@main_context_manager.writer
def qos_specs_create(context, values):
    """Create a new QoS specs.

    :param values dictionary that contains specifications for QoS

    Expected format of the input parameter:

    .. code-block:: json

        {
            'name': 'Name',
            'consumer': 'front-end',
            'specs':
            {
                'total_iops_sec': 1000,
                'total_bytes_sec': 1024000
            }
        }

    """
    specs_id = str(uuid.uuid4())
    try:
        _qos_specs_get_all_by_name(context, values['name'])
        raise exception.QoSSpecsExists(specs_id=values['name'])
    except exception.QoSSpecsNotFound:
        pass
    try:
        # Insert a root entry for QoS specs
        specs_root = models.QualityOfServiceSpecs()
        root = {'id': specs_id}
        # 'QoS_Specs_Name' is an internal reserved key to store
        # the name of QoS specs
        root['key'] = 'QoS_Specs_Name'
        root['value'] = values['name']
        LOG.debug("DB qos_specs_create(): root %s", root)
        specs_root.update(root)
        specs_root.save(context.session)

        # Save 'consumer' value directly as it will not be in
        # values['specs'] and so we avoid modifying/copying passed in dict
        consumer = {
            'key': 'consumer',
            'value': values['consumer'],
            'specs_id': specs_id,
            'id': str(uuid.uuid4()),
        }
        cons_entry = models.QualityOfServiceSpecs()
        cons_entry.update(consumer)
        cons_entry.save(context.session)

        # Insert all specification entries for QoS specs
        for k, v in values.get('specs', {}).items():
            item = {'key': k, 'value': v, 'specs_id': specs_id}
            item['id'] = str(uuid.uuid4())
            spec_entry = models.QualityOfServiceSpecs()
            spec_entry.update(item)
            spec_entry.save(context.session)
    except db_exc.DBDataError:
        msg = _('Error writing field to database')
        LOG.exception(msg)
        raise exception.Invalid(msg)
    except Exception as e:
        raise db_exc.DBError(e)

    return {'id': specs_root.id, 'name': specs_root.value}


@require_admin_context
def _qos_specs_get_all_by_name(context, name, inactive=False):
    read_deleted = 'yes' if inactive else 'no'
    results = (
        model_query(
            context,
            models.QualityOfServiceSpecs,
            read_deleted=read_deleted,
        )
        .filter_by(key='QoS_Specs_Name')
        .filter_by(value=name)
        .options(joinedload(models.QualityOfServiceSpecs.specs))
        .all()
    )

    if not results:
        raise exception.QoSSpecsNotFound(specs_id=name)

    return results


@require_admin_context
def _qos_specs_get_all_ref(context, qos_specs_id, inactive=False):
    read_deleted = 'yes' if inactive else 'no'
    result = (
        model_query(
            context,
            models.QualityOfServiceSpecs,
            read_deleted=read_deleted,
        )
        .filter_by(id=qos_specs_id)
        .options(joinedload(models.QualityOfServiceSpecs.specs))
        .all()
    )

    if not result:
        raise exception.QoSSpecsNotFound(specs_id=qos_specs_id)

    return result


def _dict_with_children_specs(specs):
    """Convert specs list to a dict."""
    result = {}
    update_time = None
    for spec in specs:
        # Skip deleted keys
        if not spec['deleted']:
            # Add update time to specs list, in order to get the keyword
            # 'updated_at' in specs info when printing logs.
            if not update_time and spec['updated_at']:
                update_time = spec['updated_at']
            elif update_time and spec['updated_at']:
                if (update_time - spec['updated_at']).total_seconds() < 0:
                    update_time = spec['updated_at']
            result.update({spec['key']: spec['value']})
    if update_time:
        result.update({'updated_at': update_time})
    return result


def _dict_with_qos_specs(rows):
    """Convert qos specs query results to list.

    Qos specs query results are a list of quality_of_service_specs refs,
    some are root entry of a qos specs (key == 'QoS_Specs_Name') and the
    rest are children entry, a.k.a detailed specs for a qos specs. This
    function converts query results to a dict using spec name as key.
    """
    result = []
    for row in rows:
        if row['key'] == 'QoS_Specs_Name':
            # Add create time for member, in order to get the keyword
            # 'created_at' in the specs info when printing logs.
            member = {
                'name': row['value'],
                'id': row['id'],
                'created_at': row['created_at'],
            }
            if row.specs:
                spec_dict = _dict_with_children_specs(row.specs)
                member['consumer'] = spec_dict.pop('consumer')
                if spec_dict.get('updated_at'):
                    member['updated_at'] = spec_dict.pop('updated_at')
                member.update({'specs': spec_dict})
            result.append(member)
    return result


@require_admin_context
@main_context_manager.reader
def qos_specs_get(context, qos_specs_id, inactive=False):
    rows = _qos_specs_get_all_ref(context, qos_specs_id, inactive)

    return _dict_with_qos_specs(rows)[0]


@require_admin_context
@main_context_manager.reader
def qos_specs_get_all(
    context,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    """Returns a list of all qos_specs.

    Results is like:
        [{
            'id': SPECS-UUID,
            'name': 'qos_spec-1',
            'consumer': 'back-end',
            'specs': {
                'key1': 'value1',
                'key2': 'value2',
                ...
            }
         },
         {
            'id': SPECS-UUID,
            'name': 'qos_spec-2',
            'consumer': 'front-end',
            'specs': {
                'key1': 'value1',
                'key2': 'value2',
                ...
            }
         },
        ]
    """
    # Generate the query
    query = _generate_paginate_query(
        context,
        marker,
        limit,
        sort_keys,
        sort_dirs,
        filters,
        offset,
        models.QualityOfServiceSpecs,
    )
    # No Qos specs would match, return empty list
    if query is None:
        return []
    rows = query.all()
    return _dict_with_qos_specs(rows)


@require_admin_context
def _qos_specs_get_query(context):
    rows = (
        model_query(
            context,
            models.QualityOfServiceSpecs,
            read_deleted='no',
        )
        .options(joinedload(models.QualityOfServiceSpecs.specs))
        .filter_by(key='QoS_Specs_Name')
    )
    return rows


def _process_qos_specs_filters(query, filters):
    if filters:
        # Ensure that filters' keys exist on the model
        if not is_valid_model_filters(models.QualityOfServiceSpecs, filters):
            return
        query = query.filter_by(**filters)
    return query


@require_admin_context
def _qos_specs_get(context, qos_spec_id):
    result = (
        model_query(context, models.QualityOfServiceSpecs, read_deleted='no')
        .filter_by(id=qos_spec_id)
        .filter_by(key='QoS_Specs_Name')
        .first()
    )

    if not result:
        raise exception.QoSSpecsNotFound(specs_id=qos_spec_id)

    return result


@require_admin_context
@main_context_manager.reader
def qos_specs_get_by_name(context, name, inactive=False):
    rows = _qos_specs_get_all_by_name(context, name, inactive)

    return _dict_with_qos_specs(rows)[0]


@require_admin_context
@main_context_manager.reader
def qos_specs_associations_get(context, qos_specs_id):
    """Return all entities associated with specified qos specs.

    For now, the only entity that is possible to associate with
    a qos specs is volume type, so this is just a wrapper of
    volume_type_qos_associations_get(). But it's possible to
    extend qos specs association to other entities, such as volumes,
    sometime in future.
    """
    return volume_type_qos_associations_get(context, qos_specs_id)


@require_admin_context
@main_context_manager.writer
def qos_specs_associate(context, qos_specs_id, type_id):
    """Associate volume type from specified qos specs."""
    return volume_type_qos_associate(context, type_id, qos_specs_id)


@require_admin_context
@main_context_manager.writer
def qos_specs_disassociate(context, qos_specs_id, type_id):
    """Disassociate volume type from specified qos specs."""
    return volume_type_qos_disassociate(context, qos_specs_id, type_id)


@require_admin_context
@main_context_manager.writer
def qos_specs_disassociate_all(context, qos_specs_id):
    """Disassociate all entities associated with specified qos specs.

    For now, the only entity that is possible to associate with
    a qos specs is volume type, so this is just a wrapper of
    volume_type_qos_disassociate_all(). But it's possible to
    extend qos specs association to other entities, such as volumes,
    sometime in future.
    """
    return volume_type_qos_disassociate_all(context, qos_specs_id)


@require_admin_context
@main_context_manager.writer
def qos_specs_item_delete(context, qos_specs_id, key):
    query = (
        context.session.query(models.QualityOfServiceSpecs)
        .filter(models.QualityOfServiceSpecs.key == key)
        .filter(models.QualityOfServiceSpecs.specs_id == qos_specs_id)
    )
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': timeutils.utcnow(),
            'updated_at': entity.updated_at,
        }
    )


@require_admin_context
@main_context_manager.writer
def qos_specs_delete(context, qos_specs_id):
    _qos_specs_get_all_ref(context, qos_specs_id)
    query = context.session.query(models.QualityOfServiceSpecs).filter(
        or_(
            models.QualityOfServiceSpecs.id == qos_specs_id,
            models.QualityOfServiceSpecs.specs_id == qos_specs_id,
        )
    )
    entity = query.column_descriptions[0]['entity']
    updated_values = {
        'deleted': True,
        'deleted_at': timeutils.utcnow(),
        'updated_at': entity.updated_at,
    }
    query.update(updated_values)
    del updated_values['updated_at']
    return updated_values


@require_admin_context
def _qos_specs_get_item(context, qos_specs_id, key):
    result = (
        model_query(context, models.QualityOfServiceSpecs)
        .filter(models.QualityOfServiceSpecs.key == key)
        .filter(models.QualityOfServiceSpecs.specs_id == qos_specs_id)
        .first()
    )

    if not result:
        raise exception.QoSSpecsKeyNotFound(
            specs_key=key, specs_id=qos_specs_id
        )

    return result


@handle_db_data_error
@require_admin_context
@require_qos_specs_exists
@main_context_manager.writer
def qos_specs_update(context, qos_specs_id, values):
    """Make updates to an existing qos specs.

    Perform add, update or delete key/values to a qos specs.
    """
    specs = values.get('specs', {})

    if 'consumer' in values:
        # Massage consumer to the right place for DB and copy specs
        # before updating so we don't modify dict for caller
        specs = specs.copy()
        specs['consumer'] = values['consumer']
    spec_ref = None
    for key in specs.keys():
        try:
            spec_ref = _qos_specs_get_item(context, qos_specs_id, key)
        except exception.QoSSpecsKeyNotFound:
            spec_ref = models.QualityOfServiceSpecs()
        id = None
        if spec_ref.get('id', None):
            id = spec_ref['id']
        else:
            id = str(uuid.uuid4())
        value = {
            'id': id,
            'key': key,
            'value': specs[key],
            'specs_id': qos_specs_id,
            'deleted': False,
        }
        LOG.debug('qos_specs_update() value: %s', value)
        spec_ref.update(value)
        spec_ref.save(context.session)

    return specs


####################


@require_context
def _volume_type_encryption_get(context, volume_type_id):
    return (
        model_query(
            context,
            models.Encryption,
            read_deleted="no",
        )
        .filter_by(volume_type_id=volume_type_id)
        .first()
    )


@require_context
@main_context_manager.reader
def volume_type_encryption_get(context, volume_type_id):
    return _volume_type_encryption_get(context, volume_type_id)


@require_admin_context
@main_context_manager.writer
def volume_type_encryption_delete(context, volume_type_id):
    encryption = _volume_type_encryption_get(context, volume_type_id)
    if not encryption:
        raise exception.VolumeTypeEncryptionNotFound(type_id=volume_type_id)
    encryption.update(
        {
            'deleted': True,
            'deleted_at': timeutils.utcnow(),
            'updated_at': encryption.updated_at,
        },
    )


@handle_db_data_error
@require_admin_context
@main_context_manager.writer
def volume_type_encryption_create(context, volume_type_id, values):
    encryption = models.Encryption()

    if 'volume_type_id' not in values:
        values['volume_type_id'] = volume_type_id

    if 'encryption_id' not in values:
        values['encryption_id'] = str(uuid.uuid4())

    encryption.update(values)
    context.session.add(encryption)

    return encryption


@handle_db_data_error
@require_admin_context
@main_context_manager.writer
def volume_type_encryption_update(context, volume_type_id, values):
    query = model_query(context, models.Encryption)
    result = query.filter_by(volume_type_id=volume_type_id).update(values)
    if not result:
        raise exception.VolumeTypeEncryptionNotFound(type_id=volume_type_id)


@main_context_manager.reader
def volume_type_encryption_volume_get(context, volume_type_id):
    volume_list = (
        _volume_get_query(context, project_only=False)
        .filter_by(volume_type_id=volume_type_id)
        .all()
    )
    return volume_list


@require_context
@main_context_manager.reader
def volume_encryption_metadata_get(context, volume_id):
    """Return the encryption metadata for a given volume."""

    volume_ref = _volume_get(context, volume_id)
    encryption_ref = _volume_type_encryption_get(
        context,
        volume_ref['volume_type_id'],
    )

    values = {
        'encryption_key_id': volume_ref['encryption_key_id'],
    }

    if encryption_ref:
        for key in ['control_location', 'cipher', 'key_size', 'provider']:
            values[key] = encryption_ref[key]

    return values


####################


@require_context
def _volume_glance_metadata_get_all(context):
    query = model_query(context, models.VolumeGlanceMetadata)
    if is_user_context(context):
        query = query.filter(
            models.Volume.id == models.VolumeGlanceMetadata.volume_id,
            models.Volume.project_id == context.project_id,
        )
    return query.all()


@require_context
@main_context_manager.reader
def volume_glance_metadata_get_all(context):
    """Return the Glance metadata for all volumes."""
    return _volume_glance_metadata_get_all(context)


@require_context
@main_context_manager.reader
def volume_glance_metadata_list_get(context, volume_id_list):
    """Return the glance metadata for a volume list."""
    query = model_query(context, models.VolumeGlanceMetadata)
    query = query.filter(
        models.VolumeGlanceMetadata.volume_id.in_(volume_id_list)
    )
    return query.all()


@require_context
@require_volume_exists
def _volume_glance_metadata_get(context, volume_id):
    rows = (
        model_query(context, models.VolumeGlanceMetadata)
        .filter_by(volume_id=volume_id)
        .filter_by(deleted=False)
        .all()
    )

    if not rows:
        raise exception.GlanceMetadataNotFound(id=volume_id)

    return rows


@require_context
@main_context_manager.reader
def volume_glance_metadata_get(context, volume_id):
    """Return the Glance metadata for the specified volume."""
    return _volume_glance_metadata_get(context, volume_id)


@require_context
@require_snapshot_exists
def _volume_snapshot_glance_metadata_get(context, snapshot_id):
    rows = (
        model_query(context, models.VolumeGlanceMetadata)
        .filter_by(snapshot_id=snapshot_id)
        .filter_by(deleted=False)
        .all()
    )

    if not rows:
        raise exception.GlanceMetadataNotFound(id=snapshot_id)

    return rows


@require_context
@main_context_manager.reader
def volume_snapshot_glance_metadata_get(context, snapshot_id):
    """Return the Glance metadata for the specified snapshot."""
    return _volume_snapshot_glance_metadata_get(context, snapshot_id)


@require_context
@require_volume_exists
@main_context_manager.writer
def volume_glance_metadata_create(context, volume_id, key, value):
    """Update the Glance metadata for a volume by adding a new key:value pair.

    This API does not support changing the value of a key once it has been
    created.
    """
    rows = (
        context.session.query(models.VolumeGlanceMetadata)
        .filter_by(volume_id=volume_id)
        .filter_by(key=key)
        .filter_by(deleted=False)
        .all()
    )

    if len(rows) > 0:
        vol_glance_metadata = rows[0]
        if vol_glance_metadata.value == str(value):
            return
        raise exception.GlanceMetadataExists(key=key, volume_id=volume_id)

    vol_glance_metadata = models.VolumeGlanceMetadata()
    vol_glance_metadata.volume_id = volume_id
    vol_glance_metadata.key = key
    vol_glance_metadata.value = str(value)
    context.session.add(vol_glance_metadata)

    return


@require_context
@require_volume_exists
@main_context_manager.writer
def volume_glance_metadata_bulk_create(context, volume_id, metadata):
    """Update the Glance metadata for a volume by adding new key:value pairs.

    This API does not support changing the value of a key once it has been
    created.
    """
    for key, value in metadata.items():
        rows = (
            context.session.query(models.VolumeGlanceMetadata)
            .filter_by(volume_id=volume_id)
            .filter_by(key=key)
            .filter_by(deleted=False)
            .all()
        )

        if len(rows) > 0:
            vol_glance_metadata = rows[0]
            if vol_glance_metadata.value == str(value):
                continue
            raise exception.GlanceMetadataExists(key=key, volume_id=volume_id)

        vol_glance_metadata = models.VolumeGlanceMetadata()
        vol_glance_metadata.volume_id = volume_id
        vol_glance_metadata.key = key
        vol_glance_metadata.value = str(value)
        context.session.add(vol_glance_metadata)


@require_context
@require_snapshot_exists
@main_context_manager.writer
def volume_glance_metadata_copy_to_snapshot(context, snapshot_id, volume_id):
    """Update the Glance metadata for a snapshot.

    This copies all of the key:value pairs from the originating volume, to
    ensure that a volume created from the snapshot will retain the
    original metadata.
    """
    metadata = _volume_glance_metadata_get(context, volume_id)
    for meta in metadata:
        vol_glance_metadata = models.VolumeGlanceMetadata()
        vol_glance_metadata.snapshot_id = snapshot_id
        vol_glance_metadata.key = meta['key']
        vol_glance_metadata.value = meta['value']
        vol_glance_metadata.save(context.session)


@require_context
@main_context_manager.writer
def volume_glance_metadata_copy_from_volume_to_volume(
    context,
    src_volume_id,
    volume_id,
):
    """Update the Glance metadata for a volume.

    This copies all of the key:value pairs from the originating volume,
    to ensure that a volume created from the volume (clone) will
    retain the original metadata.
    """

    metadata = _volume_glance_metadata_get(context, src_volume_id)
    for meta in metadata:
        vol_glance_metadata = models.VolumeGlanceMetadata()
        vol_glance_metadata.volume_id = volume_id
        vol_glance_metadata.key = meta['key']
        vol_glance_metadata.value = meta['value']
        vol_glance_metadata.save(context.session)


@require_context
@require_volume_exists
@main_context_manager.writer
def volume_glance_metadata_copy_to_volume(context, volume_id, snapshot_id):
    """Update Glance metadata from a volume.

    Update the Glance metadata from a volume (created from a snapshot) by
    copying all of the key:value pairs from the originating snapshot.

    This is so that the Glance metadata from the original volume is retained.
    """
    metadata = _volume_snapshot_glance_metadata_get(context, snapshot_id)
    for meta in metadata:
        vol_glance_metadata = models.VolumeGlanceMetadata()
        vol_glance_metadata.volume_id = volume_id
        vol_glance_metadata.key = meta['key']
        vol_glance_metadata.value = meta['value']
        vol_glance_metadata.save(context.session)


@require_context
@main_context_manager.writer
def volume_glance_metadata_delete_by_volume(context, volume_id):
    query = model_query(
        context,
        models.VolumeGlanceMetadata,
        read_deleted='no',
    ).filter_by(volume_id=volume_id)
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': timeutils.utcnow(),
            'updated_at': entity.updated_at,
        },
    )


@require_context
@main_context_manager.writer
def volume_glance_metadata_delete_by_snapshot(context, snapshot_id):
    query = model_query(
        context,
        models.VolumeGlanceMetadata,
        read_deleted='no',
    ).filter_by(snapshot_id=snapshot_id)
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': timeutils.utcnow(),
            'updated_at': entity.updated_at,
        },
    )


###############################


@require_admin_context
def _backup_data_get_for_project(context, project_id, volume_type_id=None):
    query = model_query(
        context,
        func.count(models.Backup.id),
        func.sum(models.Backup.size),
        read_deleted="no",
    ).filter_by(project_id=project_id)

    if volume_type_id:
        query = query.filter_by(volume_type_id=volume_type_id)

    result = query.first()

    # NOTE(vish): convert None to 0
    return result[0] or 0, result[1] or 0


@require_context
@main_context_manager.reader
def backup_get(context, backup_id, read_deleted=None, project_only=True):
    return _backup_get(
        context,
        backup_id,
        read_deleted=read_deleted,
        project_only=project_only,
    )


def _backup_get(
    context,
    backup_id,
    read_deleted=None,
    project_only=True,
):
    result = (
        model_query(
            context,
            models.Backup,
            project_only=project_only,
            read_deleted=read_deleted,
        )
        .options(joinedload(models.Backup.backup_metadata))
        .filter_by(id=backup_id)
        .first()
    )

    if not result:
        raise exception.BackupNotFound(backup_id=backup_id)

    return result


def _backup_get_all(
    context,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    if filters and not is_valid_model_filters(models.Backup, filters):
        return []

    # Generate the paginate query
    query = _generate_paginate_query(
        context,
        marker,
        limit,
        sort_keys,
        sort_dirs,
        filters,
        offset,
        models.Backup,
    )
    if query is None:
        return []
    return query.all()


def _backups_get_query(context, project_only=False, joined_load=True):
    query = model_query(context, models.Backup, project_only=project_only)
    if joined_load:
        query = query.options(joinedload(models.Backup.backup_metadata))
    return query


@apply_like_filters(model=models.Backup)
def _process_backups_filters(query, filters):
    if filters:
        # Ensure that filters' keys exist on the model
        if not is_valid_model_filters(models.Backup, filters):
            return
        filters_dict = {}
        for key, value in filters.items():
            if key == 'metadata':
                col_attr = getattr(models.Backup, 'backup_metadata')
                for k, v in value.items():
                    query = query.filter(col_attr.any(key=k, value=v))
            elif isinstance(value, (list, tuple, set, frozenset)):
                orm_field = getattr(models.Backup, key)
                query = query.filter(or_(orm_field == v for v in value))
            else:
                filters_dict[key] = value

        # Apply exact matches
        if filters_dict:
            query = query.filter_by(**filters_dict)
    return query


@require_admin_context
@main_context_manager.reader
def backup_get_all(
    context,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    return _backup_get_all(
        context, filters, marker, limit, offset, sort_keys, sort_dirs
    )


@require_admin_context
@main_context_manager.reader
def backup_get_all_by_host(context, host):
    return (
        model_query(context, models.Backup)
        .options(joinedload(models.Backup.backup_metadata))
        .filter_by(host=host)
        .all()
    )


@require_context
@main_context_manager.reader
def backup_get_all_by_project(
    context,
    project_id,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):

    authorize_project_context(context, project_id)
    if not filters:
        filters = {}
    else:
        filters = filters.copy()

    filters['project_id'] = project_id

    return _backup_get_all(
        context, filters, marker, limit, offset, sort_keys, sort_dirs
    )


@require_context
@main_context_manager.reader
def backup_get_all_by_volume(context, volume_id, vol_project_id, filters=None):

    authorize_project_context(context, vol_project_id)
    if not filters:
        filters = {}
    else:
        filters = filters.copy()

    filters['volume_id'] = volume_id

    return _backup_get_all(context, filters)


@require_context
@main_context_manager.reader
def backup_get_all_active_by_window(context, begin, end=None, project_id=None):
    """Return backups that were active during window."""

    query = model_query(context, models.Backup, read_deleted="yes").options(
        joinedload(models.Backup.backup_metadata)
    )
    query = query.filter(
        or_(
            models.Backup.deleted_at == None,  # noqa
            models.Backup.deleted_at > begin,
        )
    )
    if end:
        query = query.filter(models.Backup.created_at < end)
    if project_id:
        query = query.filter_by(project_id=project_id)

    return query.all()


@handle_db_data_error
@require_context
@main_context_manager.writer
def backup_create(context, values):
    values['backup_metadata'] = _metadata_refs(
        values.get('metadata'), models.BackupMetadata
    )
    if not values.get('id'):
        values['id'] = str(uuid.uuid4())

    backup_ref = models.Backup()
    backup_ref.update(values)
    context.session.add(backup_ref)

    return _backup_get(context, values['id'])


@handle_db_data_error
@require_context
@main_context_manager.writer
def backup_update(context, backup_id, values):
    if 'fail_reason' in values:
        values = values.copy()
        values['fail_reason'] = (values['fail_reason'] or '')[:255]
    query = model_query(context, models.Backup, read_deleted="yes")
    result = query.filter_by(id=backup_id).update(values)
    if not result:
        raise exception.BackupNotFound(backup_id=backup_id)


@require_admin_context
@main_context_manager.writer
def backup_destroy(context, backup_id):
    utcnow = timeutils.utcnow()
    updated_values = {
        'status': fields.BackupStatus.DELETED,
        'deleted': True,
        'deleted_at': utcnow,
    }
    query = model_query(context, models.Backup).filter_by(id=backup_id)
    entity = query.column_descriptions[0]['entity']
    updated_values['updated_at'] = entity.updated_at
    query.update(updated_values)

    query = model_query(
        context,
        models.BackupMetadata,
    ).filter_by(backup_id=backup_id)
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': utcnow,
            'updated_at': entity.updated_at,
        }
    )
    del updated_values['updated_at']
    return updated_values


def _backup_metadata_get_query(context, backup_id):
    return model_query(
        context, models.BackupMetadata, read_deleted="no"
    ).filter_by(backup_id=backup_id)


@require_context
def _backup_metadata_get(context, backup_id):
    rows = _backup_metadata_get_query(context, backup_id).all()
    result = {}
    for row in rows:
        result[row['key']] = row['value']

    return result


@require_context
@require_backup_exists
@main_context_manager.reader
def backup_metadata_get(context, backup_id):
    return _backup_metadata_get(context, backup_id)


@require_context
def _backup_metadata_get_item(context, backup_id, key):
    result = (
        _backup_metadata_get_query(context, backup_id)
        .filter_by(key=key)
        .first()
    )

    if not result:
        raise exception.BackupMetadataNotFound(
            metadata_key=key, backup_id=backup_id
        )
    return result


@require_context
@require_backup_exists
@handle_db_data_error
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def backup_metadata_update(context, backup_id, metadata, delete):
    # Set existing metadata to deleted if delete argument is True
    if delete:
        original_metadata = _backup_metadata_get(context, backup_id)
        for meta_key, meta_value in original_metadata.items():
            if meta_key not in metadata:
                meta_ref = _backup_metadata_get_item(
                    context, backup_id, meta_key
                )
                meta_ref.update(
                    {'deleted': True, 'deleted_at': timeutils.utcnow()}
                )
                meta_ref.save(context.session)

    meta_ref = None

    # Now update all existing items with new values, or create new meta
    # objects
    for meta_key, meta_value in metadata.items():

        # update the value whether it exists or not
        item = {"value": meta_value}

        try:
            meta_ref = _backup_metadata_get_item(context, backup_id, meta_key)
        except exception.BackupMetadataNotFound:
            meta_ref = models.BackupMetadata()
            item.update({"key": meta_key, "backup_id": backup_id})

        meta_ref.update(item)
        meta_ref.save(context.session)

    return backup_metadata_get(context, backup_id)


###############################


@require_context
def _transfer_get(context, transfer_id):
    query = model_query(
        context,
        models.Transfer,
    ).filter_by(id=transfer_id)

    if not is_admin_context(context):
        volume = models.Volume
        query = query.filter(
            models.Transfer.volume_id == volume.id,
            volume.project_id == context.project_id,
        )

    result = query.first()

    if not result:
        raise exception.TransferNotFound(transfer_id=transfer_id)

    return result


@require_context
@main_context_manager.reader
def transfer_get(context, transfer_id):
    return _transfer_get(context, transfer_id)


def _process_transfer_filters(query, filters):
    if filters:
        project_id = filters.pop('project_id', None)
        # Ensure that filters' keys exist on the model
        if not is_valid_model_filters(models.Transfer, filters):
            return
        if project_id:
            volume = models.Volume
            query = query.filter(
                volume.id == models.Transfer.volume_id,
                volume.project_id == project_id,
            )

        query = query.filter_by(**filters)
    return query


def _translate_transfers(transfers):
    fields = (
        'id',
        'volume_id',
        'display_name',
        'created_at',
        'deleted',
        'no_snapshots',
        'source_project_id',
        'destination_project_id',
        'accepted',
    )
    return [{k: transfer[k] for k in fields} for transfer in transfers]


def _transfer_get_all(
    context,
    marker=None,
    limit=None,
    sort_keys=None,
    sort_dirs=None,
    filters=None,
    offset=None,
):
    # Generate the query
    query = _generate_paginate_query(
        context,
        marker,
        limit,
        sort_keys,
        sort_dirs,
        filters,
        offset,
        models.Transfer,
    )
    if query is None:
        return []
    return _translate_transfers(query.all())


@require_admin_context
@main_context_manager.reader
def transfer_get_all(
    context,
    marker=None,
    limit=None,
    sort_keys=None,
    sort_dirs=None,
    filters=None,
    offset=None,
):
    return _transfer_get_all(
        context,
        marker=marker,
        limit=limit,
        sort_keys=sort_keys,
        sort_dirs=sort_dirs,
        filters=filters,
        offset=offset,
    )


def _transfer_get_query(context, project_only=False):
    return model_query(context, models.Transfer, project_only=project_only)


@require_context
@main_context_manager.reader
def transfer_get_all_by_project(
    context,
    project_id,
    marker=None,
    limit=None,
    sort_keys=None,
    sort_dirs=None,
    filters=None,
    offset=None,
):
    authorize_project_context(context, project_id)
    filters = filters.copy() if filters else {}
    filters['project_id'] = project_id
    return _transfer_get_all(
        context,
        marker=marker,
        limit=limit,
        sort_keys=sort_keys,
        sort_dirs=sort_dirs,
        filters=filters,
        offset=offset,
    )


@require_context
@handle_db_data_error
@main_context_manager.writer
def transfer_create(context, values):
    if not values.get('id'):
        values['id'] = str(uuid.uuid4())
    transfer_id = values['id']
    volume_id = values['volume_id']
    expected = {'id': volume_id, 'status': 'available'}
    update = {'status': 'awaiting-transfer'}
    if not _conditional_update(context, models.Volume, update, expected):
        msg = _(
            'Transfer %(transfer_id)s: Volume id %(volume_id)s '
            'expected in available state.'
        ) % {'transfer_id': transfer_id, 'volume_id': volume_id}
        LOG.error(msg)
        raise exception.InvalidVolume(reason=msg)

    transfer = models.Transfer()
    transfer.update(values)
    context.session.add(transfer)
    return transfer


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def transfer_destroy(context, transfer_id):
    utcnow = timeutils.utcnow()
    volume_id = _transfer_get(context, transfer_id)['volume_id']
    expected = {'id': volume_id, 'status': 'awaiting-transfer'}
    update = {'status': 'available'}
    if not _conditional_update(context, models.Volume, update, expected):
        # If the volume state is not 'awaiting-transfer' don't change it,
        # but we can still mark the transfer record as deleted.
        msg = _(
            'Transfer %(transfer_id)s: Volume expected in '
            'awaiting-transfer state.'
        ) % {'transfer_id': transfer_id}
        LOG.error(msg)

    query = model_query(context, models.Transfer).filter_by(id=transfer_id)

    entity = query.column_descriptions[0]['entity']
    updated_values = {
        'deleted': True,
        'deleted_at': utcnow,
        'updated_at': entity.updated_at,
    }

    query.update(updated_values)
    del updated_values['updated_at']
    return updated_values


def _roll_back_transferred_volume_and_snapshots(
    context, volume_id, old_user_id, old_project_id, transffered_snapshots
):
    expected = {'id': volume_id, 'status': 'available'}
    update = {
        'status': 'awaiting-transfer',
        'user_id': old_user_id,
        'project_id': old_project_id,
        'updated_at': timeutils.utcnow(),
    }
    if not _conditional_update(context, models.Volume, update, expected):
        LOG.warning(
            'Volume: %(volume_id)s is not in the expected available '
            'status. Rolling it back.',
            {'volume_id': volume_id},
        )
        return

    for snapshot_id in transffered_snapshots:
        LOG.info(
            'Beginning to roll back transferred snapshots: %s', snapshot_id
        )
        expected = {'id': snapshot_id, 'status': 'available'}
        update = {
            'user_id': old_user_id,
            'project_id': old_project_id,
            'updated_at': timeutils.utcnow(),
        }
        if not _conditional_update(context, models.Snapshot, update, expected):
            LOG.warning(
                'Snapshot: %(snapshot_id)s is not in the expected '
                'available state. Rolling it back.',
                {'snapshot_id': snapshot_id},
            )
            return


@require_context
@main_context_manager.writer
def transfer_accept(
    context, transfer_id, user_id, project_id, no_snapshots=False
):
    volume_id = _transfer_get(context, transfer_id)['volume_id']
    expected = {'id': volume_id, 'status': 'awaiting-transfer'}
    update = {
        'status': 'available',
        'user_id': user_id,
        'project_id': project_id,
        'updated_at': timeutils.utcnow(),
    }
    if not _conditional_update(context, models.Volume, update, expected):
        msg = _(
            'Transfer %(transfer_id)s: Volume id %(volume_id)s '
            'expected in awaiting-transfer state.'
        ) % {'transfer_id': transfer_id, 'volume_id': volume_id}
        LOG.error(msg)
        raise exception.InvalidVolume(reason=msg)

    # Update snapshots for transfer snapshots with volume.
    if not no_snapshots:
        snapshots = snapshot_get_all_for_volume(context, volume_id)
        transferred_snapshots = []
        for snapshot in snapshots:
            LOG.info('Begin to transfer snapshot: %s', snapshot['id'])
            old_user_id = snapshot['user_id']
            old_project_id = snapshot['project_id']
            expected = {'id': snapshot['id'], 'status': 'available'}
            update = {
                'user_id': user_id,
                'project_id': project_id,
                'updated_at': timeutils.utcnow(),
            }
            if not _conditional_update(
                context, models.Snapshot, update, expected
            ):
                msg = _(
                    'Transfer %(transfer_id)s: Snapshot '
                    '%(snapshot_id)s is not in the expected '
                    'available state.'
                ) % {'transfer_id': transfer_id, 'snapshot_id': snapshot['id']}
                LOG.error(msg)
                _roll_back_transferred_volume_and_snapshots(
                    context,
                    volume_id,
                    old_user_id,
                    old_project_id,
                    transferred_snapshots,
                )
                raise exception.InvalidSnapshot(reason=msg)
            transferred_snapshots.append(snapshot['id'])

    query = context.session.query(models.Transfer).filter_by(id=transfer_id)
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': timeutils.utcnow(),
            'updated_at': entity.updated_at,
            'destination_project_id': project_id,
            'accepted': True,
        }
    )


###############################


@require_admin_context
def _consistencygroup_data_get_for_project(context, project_id):
    query = model_query(
        context,
        func.count(models.ConsistencyGroup.id),
        read_deleted="no",
    ).filter_by(project_id=project_id)

    result = query.first()

    return (0, result[0] or 0)


@require_context
def _consistencygroup_get(context, consistencygroup_id):
    result = (
        model_query(
            context,
            models.ConsistencyGroup,
            project_only=True,
        )
        .filter_by(id=consistencygroup_id)
        .first()
    )

    if not result:
        raise exception.ConsistencyGroupNotFound(
            consistencygroup_id=consistencygroup_id
        )

    return result


@require_context
@main_context_manager.reader
def consistencygroup_get(context, consistencygroup_id):
    return _consistencygroup_get(context, consistencygroup_id)


def _consistencygroups_get_query(context, project_only=False):
    return model_query(
        context,
        models.ConsistencyGroup,
        project_only=project_only,
    )


def _process_consistencygroups_filters(query, filters):
    if filters:
        # Ensure that filters' keys exist on the model
        if not is_valid_model_filters(models.ConsistencyGroup, filters):
            return
        query = query.filter_by(**filters)
    return query


def _consistencygroup_get_all(
    context,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    if filters and not is_valid_model_filters(
        models.ConsistencyGroup, filters
    ):
        return []

    # Generate the paginate query
    query = _generate_paginate_query(
        context,
        marker,
        limit,
        sort_keys,
        sort_dirs,
        filters,
        offset,
        models.ConsistencyGroup,
    )
    if query is None:
        return []
    return query.all()


@require_admin_context
@main_context_manager.reader
def consistencygroup_get_all(
    context,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    """Retrieves all consistency groups.

    If no sort parameters are specified then the returned cgs are sorted
    first by the 'created_at' key and then by the 'id' key in descending
    order.

    :param context: context to query under
    :param marker: the last item of the previous page, used to determine the
                   next page of results to return
    :param limit: maximum number of items to return
    :param sort_keys: list of attributes by which results should be sorted,
                      paired with corresponding item in sort_dirs
    :param sort_dirs: list of directions in which results should be sorted,
                      paired with corresponding item in sort_keys
    :param filters: Filters for the query in the form of key/value.
    :returns: list of matching consistency groups
    """
    return _consistencygroup_get_all(
        context, filters, marker, limit, offset, sort_keys, sort_dirs
    )


@require_context
@main_context_manager.reader
def consistencygroup_get_all_by_project(
    context,
    project_id,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    """Retrieves all consistency groups in a project.

    If no sort parameters are specified then the returned cgs are sorted
    first by the 'created_at' key and then by the 'id' key in descending
    order.

    :param context: context to query under
    :param marker: the last item of the previous page, used to determine the
                   next page of results to return
    :param limit: maximum number of items to return
    :param sort_keys: list of attributes by which results should be sorted,
                      paired with corresponding item in sort_dirs
    :param sort_dirs: list of directions in which results should be sorted,
                      paired with corresponding item in sort_keys
    :param filters: Filters for the query in the form of key/value.
    :returns: list of matching consistency groups
    """
    authorize_project_context(context, project_id)
    if not filters:
        filters = {}
    else:
        filters = filters.copy()

    filters['project_id'] = project_id
    return _consistencygroup_get_all(
        context, filters, marker, limit, offset, sort_keys, sort_dirs
    )


@handle_db_data_error
@require_context
@main_context_manager.writer
def consistencygroup_create(context, values, cg_snap_id=None, cg_id=None):
    cg_model = models.ConsistencyGroup

    values = values.copy()
    if not values.get('id'):
        values['id'] = str(uuid.uuid4())

    if cg_snap_id:
        conditions = [
            cg_model.id == models.CGSnapshot.consistencygroup_id,
            models.CGSnapshot.id == cg_snap_id,
        ]
    elif cg_id:
        conditions = [cg_model.id == cg_id]
    else:
        conditions = None

    if conditions:
        # We don't want duplicated field values
        names = ['volume_type_id', 'availability_zone', 'host', 'cluster_name']
        for name in names:
            values.pop(name, None)

        fields = [getattr(cg_model, name) for name in names]
        fields.extend(bindparam(k, v) for k, v in values.items())
        sel = context.session.query(*fields).filter(*conditions)
        names.extend(values.keys())
        insert_stmt = cg_model.__table__.insert().from_select(names, sel)
        result = context.session.execute(insert_stmt)
        # If we couldn't insert the row because of the conditions raise
        # the right exception
        if not result.rowcount:
            if cg_id:
                raise exception.ConsistencyGroupNotFound(
                    consistencygroup_id=cg_id
                )
            raise exception.CgSnapshotNotFound(cgsnapshot_id=cg_snap_id)
    else:
        consistencygroup = cg_model()
        consistencygroup.update(values)
        context.session.add(consistencygroup)

    return _consistencygroup_get(context, values['id'])


@handle_db_data_error
@require_context
@main_context_manager.writer
def consistencygroup_update(context, consistencygroup_id, values):
    query = model_query(context, models.ConsistencyGroup, project_only=True)
    result = query.filter_by(id=consistencygroup_id).update(values)
    if not result:
        raise exception.ConsistencyGroupNotFound(
            consistencygroup_id=consistencygroup_id
        )


@require_admin_context
@main_context_manager.writer
def consistencygroup_destroy(context, consistencygroup_id):
    utcnow = timeutils.utcnow()
    query = model_query(
        context,
        models.ConsistencyGroup,
    ).filter_by(id=consistencygroup_id)
    entity = query.column_descriptions[0]['entity']
    updated_values = {
        'status': fields.ConsistencyGroupStatus.DELETED,
        'deleted': True,
        'deleted_at': utcnow,
        'updated_at': entity.updated_at,
    }
    query.update(updated_values)

    del updated_values['updated_at']
    return updated_values


def cg_has_cgsnapshot_filter():
    """Return a filter that checks if a CG has CG Snapshots."""
    return sql.exists().where(
        and_(
            models.CGSnapshot.consistencygroup_id
            == models.ConsistencyGroup.id,
            ~models.CGSnapshot.deleted,
        )
    )


def cg_has_volumes_filter(attached_or_with_snapshots=False):
    """Return a filter to check if a CG has volumes.

    When attached_or_with_snapshots parameter is given a True value only
    attached volumes or those with snapshots will be considered.
    """
    query = sql.exists().where(
        and_(
            models.Volume.consistencygroup_id == models.ConsistencyGroup.id,
            ~models.Volume.deleted,
        )
    )

    if attached_or_with_snapshots:
        query = query.where(
            or_(
                models.Volume.attach_status == 'attached',
                sql.exists().where(
                    and_(
                        models.Volume.id == models.Snapshot.volume_id,
                        ~models.Snapshot.deleted,
                    )
                ),
            )
        )
    return query


def cg_creating_from_src(cg_id=None, cgsnapshot_id=None):
    """Return a filter to check if a CG is being used as creation source.

    Returned filter is meant to be used in the Conditional Update mechanism and
    checks if provided CG ID or CG Snapshot ID is currently being used to
    create another CG.

    This filter will not include CGs that have used the ID but have already
    finished their creation (status is no longer creating).

    Filter uses a subquery that allows it to be used on updates to the
    consistencygroups table.
    """
    # NOTE(geguileo): As explained in devref api_conditional_updates we use a
    # subquery to trick MySQL into using the same table in the update and the
    # where clause.
    subq = (
        sql.select(models.ConsistencyGroup)
        .where(
            and_(
                ~models.ConsistencyGroup.deleted,
                models.ConsistencyGroup.status == 'creating',
            )
        )
        .alias('cg2')
    )

    if cg_id:
        match_id = subq.c.source_cgid == cg_id
    elif cgsnapshot_id:
        match_id = subq.c.cgsnapshot_id == cgsnapshot_id
    else:
        msg = _(
            'cg_creating_from_src must be called with cg_id or '
            'cgsnapshot_id parameter.'
        )
        raise exception.ProgrammingError(reason=msg)

    return sql.exists([subq]).where(match_id)


@require_admin_context
@main_context_manager.writer
def consistencygroup_include_in_cluster(
    context, cluster, partial_rename=True, **filters
):
    """Include all consistency groups matching the filters into a cluster."""
    return _include_in_cluster(
        context,
        cluster,
        models.ConsistencyGroup,
        partial_rename,
        filters,
    )


###############################


@require_admin_context
def _group_data_get_for_project(context, project_id):
    query = model_query(
        context,
        func.count(models.Group.id),
        read_deleted="no",
    ).filter_by(project_id=project_id)

    result = query.first()

    return (0, result[0] or 0)


@require_context
def _group_get(context, group_id):
    result = (
        model_query(context, models.Group, project_only=True)
        .filter_by(id=group_id)
        .first()
    )

    if not result:
        raise exception.GroupNotFound(group_id=group_id)

    return result


@require_context
@main_context_manager.reader
def group_get(context, group_id):
    return _group_get(context, group_id)


def _groups_get_query(context, project_only=False):
    return model_query(context, models.Group, project_only=project_only)


def _group_snapshot_get_query(context, project_only=False):
    return model_query(
        context,
        models.GroupSnapshot,
        project_only=project_only,
    )


@apply_like_filters(model=models.Group)
def _process_groups_filters(query, filters):
    if filters:
        # NOTE(xyang): backend_match_level needs to be handled before
        # is_valid_model_filters is called as it is not a column name
        # in the db.
        backend_match_level = filters.pop('backend_match_level', 'backend')
        # host is a valid filter. Filter the query by host and
        # backend_match_level first.
        host = filters.pop('host', None)
        if host:
            query = query.filter(
                _filter_host(
                    models.Group.host, host, match_level=backend_match_level
                )
            )
        # Ensure that filters' keys exist on the model
        if not is_valid_model_filters(models.Group, filters):
            return
        query = query.filter_by(**filters)
    return query


@apply_like_filters(model=models.GroupSnapshot)
def _process_group_snapshot_filters(query, filters):
    if filters:
        # Ensure that filters' keys exist on the model
        if not is_valid_model_filters(models.GroupSnapshot, filters):
            return
        query = query.filter_by(**filters)
    return query


def _group_get_all(
    context,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    # No need to call is_valid_model_filters here. It is called
    # in _process_group_filters when _generate_paginate_query
    # is called below.
    # Generate the paginate query
    query = _generate_paginate_query(
        context,
        marker,
        limit,
        sort_keys,
        sort_dirs,
        filters,
        offset,
        models.Group,
    )

    return query.all() if query else []


@require_admin_context
@main_context_manager.reader
def group_get_all(
    context,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    """Retrieves all groups.

    If no sort parameters are specified then the returned groups are sorted
    first by the 'created_at' key and then by the 'id' key in descending
    order.

    :param context: context to query under
    :param marker: the last item of the previous page, used to determine the
        next page of results to return
    :param limit: maximum number of items to return
    :param sort_keys: list of attributes by which results should be sorted,
        paired with corresponding item in sort_dirs
    :param sort_dirs: list of directions in which results should be sorted,
        paired with corresponding item in sort_keys
    :param filters: Filters for the query in the form of key/value.
    :returns: list of matching  groups
    """
    return _group_get_all(
        context, filters, marker, limit, offset, sort_keys, sort_dirs
    )


@require_context
@main_context_manager.reader
def group_get_all_by_project(
    context,
    project_id,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    """Retrieves all groups in a project.

    If no sort parameters are specified then the returned groups are sorted
    first by the 'created_at' key and then by the 'id' key in descending
    order.

    :param context: context to query under
    :param marker: the last item of the previous page, used to determine the
        next page of results to return
    :param limit: maximum number of items to return
    :param sort_keys: list of attributes by which results should be sorted,
        paired with corresponding item in sort_dirs
    :param sort_dirs: list of directions in which results should be sorted,
        paired with corresponding item in sort_keys
    :param filters: Filters for the query in the form of key/value.
    :returns: list of matching groups
    """
    authorize_project_context(context, project_id)
    if not filters:
        filters = {}
    else:
        filters = filters.copy()

    filters['project_id'] = project_id
    return _group_get_all(
        context, filters, marker, limit, offset, sort_keys, sort_dirs
    )


@handle_db_data_error
@require_context
@main_context_manager.writer
def group_create(
    context, values, group_snapshot_id=None, source_group_id=None
):
    group_model = models.Group

    values = values.copy()
    if not values.get('id'):
        values['id'] = str(uuid.uuid4())

    if group_snapshot_id:
        conditions = [
            group_model.id == models.GroupSnapshot.group_id,
            models.GroupSnapshot.id == group_snapshot_id,
        ]
    elif source_group_id:
        conditions = [group_model.id == source_group_id]
    else:
        conditions = None

    if conditions:
        # We don't want duplicated field values
        values.pop('group_type_id', None)
        values.pop('availability_zone', None)
        values.pop('host', None)
        values.pop('cluster_name', None)
        # NOTE(xyang): Save volume_type_ids to update later.
        volume_type_ids = values.pop('volume_type_ids', [])

        sel = context.session.query(
            group_model.group_type_id,
            group_model.availability_zone,
            group_model.host,
            group_model.cluster_name,
            *(bindparam(k, v) for k, v in values.items()),
        ).filter(*conditions)
        names = ['group_type_id', 'availability_zone', 'host', 'cluster_name']
        names.extend(values.keys())
        insert_stmt = group_model.__table__.insert().from_select(names, sel)
        result = context.session.execute(insert_stmt)
        # If we couldn't insert the row because of the conditions raise
        # the right exception
        if not result.rowcount:
            if source_group_id:
                raise exception.GroupNotFound(group_id=source_group_id)
            raise exception.GroupSnapshotNotFound(
                group_snapshot_id=group_snapshot_id
            )

        for item in volume_type_ids:
            mapping = models.GroupVolumeTypeMapping()
            mapping['volume_type_id'] = item
            mapping['group_id'] = values['id']
            context.session.add(mapping)
    else:
        for item in values.get('volume_type_ids') or []:
            mapping = models.GroupVolumeTypeMapping()
            mapping['volume_type_id'] = item
            mapping['group_id'] = values['id']
            context.session.add(mapping)

        group = group_model()
        group.update(values)
        context.session.add(group)

    return _group_get(context, values['id'])


@handle_db_data_error
@require_context
@main_context_manager.writer
def group_volume_type_mapping_create(context, group_id, volume_type_id):
    """Add group volume_type mapping entry."""
    # Verify group exists
    _group_get(context, group_id)
    # Verify volume type exists
    _volume_type_get_id_from_volume_type(context, volume_type_id)

    existing = _group_volume_type_mapping_get_all_by_group_volume_type(
        context, group_id, volume_type_id
    )
    if existing:
        raise exception.GroupVolumeTypeMappingExists(
            group_id=group_id, volume_type_id=volume_type_id
        )

    mapping = models.GroupVolumeTypeMapping()
    mapping.update({"group_id": group_id, "volume_type_id": volume_type_id})

    try:
        mapping.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.GroupVolumeTypeMappingExists(
            group_id=group_id, volume_type_id=volume_type_id
        )
    return mapping


@handle_db_data_error
@require_context
@main_context_manager.writer
def group_update(context, group_id, values):
    query = model_query(context, models.Group, project_only=True)
    result = query.filter_by(id=group_id).update(values)
    if not result:
        raise exception.GroupNotFound(group_id=group_id)


@require_admin_context
@main_context_manager.writer
def group_destroy(context, group_id):
    query = model_query(context, models.Group).filter_by(id=group_id)
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'status': fields.GroupStatus.DELETED,
            'deleted': True,
            'deleted_at': timeutils.utcnow(),
            'updated_at': entity.updated_at,
        }
    )

    query = context.session.query(
        models.GroupVolumeTypeMapping,
    ).filter_by(group_id=group_id)
    entity = query.column_descriptions[0]['entity']
    query.update(
        {
            'deleted': True,
            'deleted_at': timeutils.utcnow(),
            'updated_at': entity.updated_at,
        }
    )


def group_has_group_snapshot_filter():
    return sql.exists().where(
        and_(
            models.GroupSnapshot.group_id == models.Group.id,
            ~models.GroupSnapshot.deleted,
        )
    )


def group_has_volumes_filter(attached_or_with_snapshots=False):
    query = sql.exists().where(
        and_(models.Volume.group_id == models.Group.id, ~models.Volume.deleted)
    )

    if attached_or_with_snapshots:
        query = query.where(
            or_(
                models.Volume.attach_status == 'attached',
                sql.exists().where(
                    and_(
                        models.Volume.id == models.Snapshot.volume_id,
                        ~models.Snapshot.deleted,
                    )
                ),
            )
        )
    return query


def group_creating_from_src(group_id=None, group_snapshot_id=None):
    # NOTE(geguileo): As explained in devref api_conditional_updates we use a
    # subquery to trick MySQL into using the same table in the update and the
    # where clause.
    subq = (
        sql.select(models.Group)
        .where(and_(~models.Group.deleted, models.Group.status == 'creating'))
        .alias('group2')
    )

    if group_id:
        match_id = subq.c.source_group_id == group_id
    elif group_snapshot_id:
        match_id = subq.c.group_snapshot_id == group_snapshot_id
    else:
        msg = _(
            'group_creating_from_src must be called with group_id or '
            'group_snapshot_id parameter.'
        )
        raise exception.ProgrammingError(reason=msg)

    return sql.exists(subq).where(match_id)


@require_admin_context
@main_context_manager.writer
def group_include_in_cluster(context, cluster, partial_rename=True, **filters):
    """Include all generic groups matching the filters into a cluster."""
    return _include_in_cluster(
        context, cluster, models.Group, partial_rename, filters
    )


###############################


@require_context
def _cgsnapshot_get(context, cgsnapshot_id):
    result = (
        model_query(context, models.CGSnapshot, project_only=True)
        .filter_by(id=cgsnapshot_id)
        .first()
    )

    if not result:
        raise exception.CgSnapshotNotFound(cgsnapshot_id=cgsnapshot_id)

    return result


@require_context
@main_context_manager.reader
def cgsnapshot_get(context, cgsnapshot_id):
    return _cgsnapshot_get(context, cgsnapshot_id)


def is_valid_model_filters(model, filters, exclude_list=None):
    """Return True if filter values exist on the model

    :param model: a Cinder model
    :param filters: dictionary of filters
    """
    for key in filters.keys():
        if exclude_list and key in exclude_list:
            continue

        if key == 'metadata':
            if not isinstance(filters[key], dict):
                LOG.debug("Metadata filter value is not valid dictionary")
                return False
            continue

        try:
            key = key.rstrip('~')
            getattr(model, key)
        except AttributeError:
            LOG.debug("'%s' filter key is not valid.", key)
            return False

    return True


def _cgsnapshot_get_all(context, project_id=None, group_id=None, filters=None):
    query = model_query(context, models.CGSnapshot)

    if filters:
        if not is_valid_model_filters(models.CGSnapshot, filters):
            return []

        query = query.filter_by(**filters)

    if project_id:
        query = query.filter_by(project_id=project_id)

    if group_id:
        query = query.filter_by(consistencygroup_id=group_id)

    return query.all()


@require_admin_context
@main_context_manager.reader
def cgsnapshot_get_all(context, filters=None):
    return _cgsnapshot_get_all(context, filters=filters)


@require_admin_context
@main_context_manager.reader
def cgsnapshot_get_all_by_group(context, group_id, filters=None):
    return _cgsnapshot_get_all(context, group_id=group_id, filters=filters)


@require_context
@main_context_manager.reader
def cgsnapshot_get_all_by_project(context, project_id, filters=None):
    authorize_project_context(context, project_id)
    return _cgsnapshot_get_all(context, project_id=project_id, filters=filters)


@handle_db_data_error
@require_context
@main_context_manager.writer
def cgsnapshot_create(context, values):
    if not values.get('id'):
        values['id'] = str(uuid.uuid4())

    cg_id = values.get('consistencygroup_id')
    model = models.CGSnapshot
    if cg_id:
        # There has to exist at least 1 volume in the CG and the CG cannot
        # be updating the composing volumes or being created.
        conditions = [
            sql.exists().where(
                and_(
                    ~models.Volume.deleted,
                    models.Volume.consistencygroup_id == cg_id,
                ),
            ),
            ~models.ConsistencyGroup.deleted,
            models.ConsistencyGroup.id == cg_id,
            ~models.ConsistencyGroup.status.in_(('creating', 'updating')),
        ]

        # NOTE(geguileo): We build a "fake" from_select clause instead of
        # using transaction isolation on the session because we would need
        # SERIALIZABLE level and that would have a considerable performance
        # penalty.
        binds = (bindparam(k, v) for k, v in values.items())
        sel = context.session.query(*binds).filter(*conditions)
        insert_stmt = model.__table__.insert().from_select(
            values.keys(),
            sel,
        )
        result = context.session.execute(insert_stmt)
        # If we couldn't insert the row because of the conditions raise
        # the right exception
        if not result.rowcount:
            msg = _(
                "Source CG cannot be empty or in 'creating' or "
                "'updating' state. No cgsnapshot will be created."
            )
            raise exception.InvalidConsistencyGroup(reason=msg)
    else:
        cgsnapshot = model()
        cgsnapshot.update(values)
        context.session.add(cgsnapshot)

    return _cgsnapshot_get(context, values['id'])


@require_context
@handle_db_data_error
@main_context_manager.writer
def cgsnapshot_update(context, cgsnapshot_id, values):
    query = model_query(context, models.CGSnapshot, project_only=True)
    result = query.filter_by(id=cgsnapshot_id).update(values)
    if not result:
        raise exception.CgSnapshotNotFound(cgsnapshot_id=cgsnapshot_id)


@require_admin_context
@main_context_manager.writer
def cgsnapshot_destroy(context, cgsnapshot_id):
    query = model_query(context, models.CGSnapshot).filter_by(id=cgsnapshot_id)
    entity = query.column_descriptions[0]['entity']
    updated_values = {
        'status': 'deleted',
        'deleted': True,
        'deleted_at': timeutils.utcnow(),
        'updated_at': entity.updated_at,
    }
    query.update(updated_values)
    del updated_values['updated_at']
    return updated_values


def cgsnapshot_creating_from_src():
    """Get a filter that checks if a CGSnapshot is being created from a CG."""
    return sql.exists().where(
        and_(
            models.CGSnapshot.consistencygroup_id
            == models.ConsistencyGroup.id,
            ~models.CGSnapshot.deleted,
            models.CGSnapshot.status == 'creating',
        )
    )


###############################


@require_context
def _group_snapshot_get(context, group_snapshot_id):
    result = (
        model_query(context, models.GroupSnapshot, project_only=True)
        .filter_by(id=group_snapshot_id)
        .first()
    )
    if not result:
        raise exception.GroupSnapshotNotFound(
            group_snapshot_id=group_snapshot_id
        )

    return result


@require_context
@main_context_manager.reader
def group_snapshot_get(context, group_snapshot_id):
    return _group_snapshot_get(context, group_snapshot_id)


def _group_snapshot_get_all(
    context,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    if filters and not is_valid_model_filters(
        models.GroupSnapshot,
        filters,
    ):
        return []

    # Generate the paginate query
    query = _generate_paginate_query(
        context,
        marker,
        limit,
        sort_keys,
        sort_dirs,
        filters,
        offset,
        models.GroupSnapshot,
    )

    return query.all() if query else []


@require_admin_context
@main_context_manager.reader
def group_snapshot_get_all(
    context,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    return _group_snapshot_get_all(
        context,
        filters,
        marker,
        limit,
        offset,
        sort_keys,
        sort_dirs,
    )


@require_admin_context
@main_context_manager.reader
def group_snapshot_get_all_by_group(
    context,
    group_id,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    if filters is None:
        filters = {}
    if group_id:
        filters['group_id'] = group_id
    return _group_snapshot_get_all(
        context, filters, marker, limit, offset, sort_keys, sort_dirs
    )


@require_context
@main_context_manager.reader
def group_snapshot_get_all_by_project(
    context,
    project_id,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    authorize_project_context(context, project_id)
    if filters is None:
        filters = {}
    if project_id:
        filters['project_id'] = project_id
    return _group_snapshot_get_all(
        context, filters, marker, limit, offset, sort_keys, sort_dirs
    )


@handle_db_data_error
@require_context
@main_context_manager.writer
def group_snapshot_create(context, values):
    if not values.get('id'):
        values['id'] = str(uuid.uuid4())

    group_id = values.get('group_id')
    model = models.GroupSnapshot
    if group_id:
        # There has to exist at least 1 volume in the group and the group
        # cannot be updating the composing volumes or being created.
        conditions = [
            sql.exists().where(
                and_(
                    ~models.Volume.deleted, models.Volume.group_id == group_id
                )
            ),
            ~models.Group.deleted,
            models.Group.id == group_id,
            ~models.Group.status.in_(('creating', 'updating')),
        ]

        # NOTE(geguileo): We build a "fake" from_select clause instead of
        # using transaction isolation on the session because we would need
        # SERIALIZABLE level and that would have a considerable performance
        # penalty.
        binds = (bindparam(k, v) for k, v in values.items())
        sel = context.session.query(*binds).filter(*conditions)
        insert_stmt = model.__table__.insert().from_select(values.keys(), sel)
        result = context.session.execute(insert_stmt)
        # If we couldn't insert the row because of the conditions raise
        # the right exception
        if not result.rowcount:
            msg = _(
                "Source group cannot be empty or in 'creating' or "
                "'updating' state. No group snapshot will be created."
            )
            raise exception.InvalidGroup(reason=msg)
    else:
        group_snapshot = model()
        group_snapshot.update(values)
        context.session.add(group_snapshot)
    return _group_snapshot_get(context, values['id'])


@require_context
@handle_db_data_error
@main_context_manager.writer
def group_snapshot_update(context, group_snapshot_id, values):
    result = (
        model_query(context, models.GroupSnapshot, project_only=True)
        .filter_by(id=group_snapshot_id)
        .first()
    )

    if not result:
        raise exception.GroupSnapshotNotFound(
            _("No group snapshot with id %s") % group_snapshot_id
        )

    result.update(values)
    result.save(context.session)
    return result


@require_admin_context
@main_context_manager.writer
def group_snapshot_destroy(context, group_snapshot_id):
    query = model_query(context, models.GroupSnapshot).filter_by(
        id=group_snapshot_id
    )
    entity = query.column_descriptions[0]['entity']
    updated_values = {
        'status': 'deleted',
        'deleted': True,
        'deleted_at': timeutils.utcnow(),
        'updated_at': entity.updated_at,
    }
    query.update(updated_values)
    del updated_values['updated_at']
    return updated_values


def group_snapshot_creating_from_src():
    """Get a filter to check if a grp snapshot is being created from a grp."""
    return sql.exists().where(
        and_(
            models.GroupSnapshot.group_id == models.Group.id,
            ~models.GroupSnapshot.deleted,
            models.GroupSnapshot.status == 'creating',
        )
    )


###############################


@require_admin_context
@main_context_manager.writer
def purge_deleted_rows(context, age_in_days):
    """Purge deleted rows older than age from cinder tables."""
    try:
        age_in_days = int(age_in_days)
    except ValueError:
        msg = _('Invalid value for age, %(age)s')
        LOG.exception(msg, {'age': age_in_days})
        raise exception.InvalidParameterValue(msg % {'age': age_in_days})

    engine = get_engine()
    metadata = MetaData()
    metadata.reflect(engine)

    deleted_age = timeutils.utcnow() - dt.timedelta(days=age_in_days)
    for table in reversed(metadata.sorted_tables):
        if 'deleted' not in table.columns.keys():
            continue

        LOG.info(
            'Purging deleted rows older than age=%(age)d days '
            'from table=%(table)s',
            {'age': age_in_days, 'table': table},
        )

        try:
            # Delete child records first from quality_of_service_specs
            # table to avoid FK constraints
            if str(table) == 'quality_of_service_specs':
                context.session.query(models.QualityOfServiceSpecs).filter(
                    and_(
                        models.QualityOfServiceSpecs.specs_id.isnot(None),
                        models.QualityOfServiceSpecs.deleted.is_(True),
                        models.QualityOfServiceSpecs.deleted_at < deleted_age,
                    )
                ).delete()
            result = context.session.execute(
                table.delete().where(
                    and_(
                        table.columns.deleted.is_(True),
                        table.c.deleted_at < deleted_age,
                    )
                )
            )
        except db_exc.DBReferenceError as ex:
            LOG.error(
                'DBError detected when purging from %(tablename)s: %(error)s.',
                {'tablename': table, 'error': ex},
            )
            raise

        rows_purged = result.rowcount
        if rows_purged != 0:
            LOG.info(
                'Deleted %(row)d rows from table=%(table)s',
                {'row': rows_purged, 'table': table},
            )


###############################


@require_admin_context
@main_context_manager.writer
def reset_active_backend(
    context,
    enable_replication,
    active_backend_id,
    backend_host,
):

    service = objects.Service.get_by_host_and_topic(
        context,
        backend_host,
        'cinder-volume',
        disabled=True,
    )
    if not service.frozen:
        raise exception.ServiceUnavailable(
            'Service for host %(host)s must first be frozen.'
            % {'host': backend_host},
        )

    actions = {
        'disabled': False,
        'disabled_reason': '',
        'active_backend_id': None,
        'replication_status': 'enabled',
    }

    expectations = {
        'frozen': True,
        'disabled': True,
    }

    if service.is_clustered:
        service.cluster.conditional_update(actions, expectations)
        service.cluster.reset_service_replication()
    else:
        service.conditional_update(actions, expectations)


###############################


def _translate_messages(messages):
    return [_translate_message(message) for message in messages]


def _translate_message(message):
    """Translate the Message model to a dict."""
    return {
        'id': message['id'],
        'project_id': message['project_id'],
        'request_id': message['request_id'],
        'resource_type': message['resource_type'],
        'resource_uuid': message.get('resource_uuid'),
        'event_id': message['event_id'],
        'detail_id': message['detail_id'],
        'action_id': message['action_id'],
        'message_level': message['message_level'],
        'created_at': message['created_at'],
        'expires_at': message.get('expires_at'),
    }


def _message_get(context, message_id):
    query = model_query(
        context,
        models.Message,
        read_deleted="no",
        project_only="yes",
    )
    result = query.filter_by(id=message_id).first()
    if not result:
        raise exception.MessageNotFound(message_id=message_id)
    return result


@require_context
@main_context_manager.reader
def message_get(context, message_id):
    result = _message_get(context, message_id)
    return _translate_message(result)


@require_context
@main_context_manager.reader
def message_get_all(
    context,
    filters=None,
    marker=None,
    limit=None,
    offset=None,
    sort_keys=None,
    sort_dirs=None,
):
    """Retrieves all messages.

    If no sort parameters are specified then the returned messages are
    sorted first by the 'created_at' key and then by the 'id' key in
    descending order.

    :param context: context to query under
    :param marker: the last item of the previous page, used to determine the
        next page of results to return
    :param limit: maximum number of items to return
    :param sort_keys: list of attributes by which results should be sorted,
        paired with corresponding item in sort_dirs
    :param sort_dirs: list of directions in which results should be sorted,
        paired with corresponding item in sort_keys
    :param filters: dictionary of filters; values that are in lists, tuples,
        or sets cause an 'IN' operation, while exact matching is used for other
        values, see _process_messages_filters function for more information
    :returns: list of matching messages
    """
    # Generate the paginate query
    query = _generate_paginate_query(
        context,
        marker,
        limit,
        sort_keys,
        sort_dirs,
        filters,
        offset,
        models.Message,
    )
    if query is None:
        return []
    results = query.all()
    return _translate_messages(results)


@apply_like_filters(model=models.Message)
def _process_messages_filters(query, filters):
    if filters:
        # Ensure that filters' keys exist on the model
        if not is_valid_model_filters(models.Message, filters):
            return None
        query = query.filter_by(**filters)
    return query


def _messages_get_query(context, project_only=False):
    return model_query(context, models.Message, project_only=project_only)


@require_context
@main_context_manager.writer
def message_create(context, values):
    message_ref = models.Message()
    if not values.get('id'):
        values['id'] = str(uuid.uuid4())
    message_ref.update(values)

    context.session.add(message_ref)


@require_admin_context
@main_context_manager.writer
def message_destroy(context, message_id):
    now = timeutils.utcnow()
    query = model_query(context, models.Message).filter_by(id=message_id)
    entity = query.column_descriptions[0]['entity']
    updated_values = {
        'deleted': True,
        'deleted_at': now,
        'updated_at': entity.updated_at,
    }
    query.update(updated_values)
    del updated_values['updated_at']
    return updated_values


@require_admin_context
@main_context_manager.writer
def cleanup_expired_messages(context):
    now = timeutils.utcnow()
    # NOTE(tommylikehu): Directly delete the expired
    # messages here.
    return (
        context.session.query(models.Message)
        .filter(models.Message.expires_at < now)
        .delete()
    )


###############################


@require_context
@main_context_manager.writer
def driver_initiator_data_insert_by_key(
    context,
    initiator,
    namespace,
    key,
    value,
):
    data = models.DriverInitiatorData()
    data.initiator = initiator
    data.namespace = namespace
    data.key = key
    data.value = value
    try:
        data.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.DriverInitiatorDataExists(
            initiator=initiator,
            namespace=namespace,
            key=key,
        )
    return data


@require_context
@main_context_manager.reader
def driver_initiator_data_get(context, initiator, namespace):
    return (
        context.session.query(models.DriverInitiatorData)
        .filter_by(initiator=initiator)
        .filter_by(namespace=namespace)
        .all()
    )


###############################


@require_context
@main_context_manager.writer
def image_volume_cache_create(
    context,
    host,
    cluster_name,
    image_id,
    image_updated_at,
    volume_id,
    size,
):
    cache_entry = models.ImageVolumeCacheEntry()
    cache_entry.host = host
    cache_entry.cluster_name = cluster_name
    cache_entry.image_id = image_id
    cache_entry.image_updated_at = image_updated_at
    cache_entry.volume_id = volume_id
    cache_entry.size = size
    context.session.add(cache_entry)
    return cache_entry


@require_context
@main_context_manager.writer
def image_volume_cache_delete(context, volume_id):
    context.session.query(
        models.ImageVolumeCacheEntry,
    ).filter_by(volume_id=volume_id).delete()


@require_context
@main_context_manager.writer
def image_volume_cache_get_and_update_last_used(context, image_id, **filters):
    filters = _clean_filters(filters)
    entry = (
        context.session.query(models.ImageVolumeCacheEntry)
        .filter_by(image_id=image_id)
        .filter_by(**filters)
        .order_by(desc(models.ImageVolumeCacheEntry.last_used))
        .first()
    )

    if entry:
        entry.last_used = timeutils.utcnow()
        entry.save(context.session)
    return entry


@require_context
@main_context_manager.reader
def image_volume_cache_get_by_volume_id(context, volume_id):
    return (
        context.session.query(models.ImageVolumeCacheEntry)
        .filter_by(volume_id=volume_id)
        .first()
    )


@require_context
@main_context_manager.reader
def image_volume_cache_get_all(context, **filters):
    filters = _clean_filters(filters)
    return (
        context.session.query(models.ImageVolumeCacheEntry)
        .filter_by(**filters)
        .order_by(desc(models.ImageVolumeCacheEntry.last_used))
        .all()
    )


@require_admin_context
@main_context_manager.writer
def image_volume_cache_include_in_cluster(
    context,
    cluster,
    partial_rename=True,
    **filters,
):
    """Include all volumes matching the filters into a cluster."""
    filters = _clean_filters(filters)
    return _include_in_cluster(
        context,
        cluster,
        models.ImageVolumeCacheEntry,
        partial_rename,
        filters,
    )


###################


def _worker_query(
    context,
    until=None,
    db_filters=None,
    ignore_sentinel=True,
    **filters,
):
    # Remove all filters based on the workers table that are set to None
    filters = _clean_filters(filters)

    if filters and not is_valid_model_filters(models.Worker, filters):
        return None

    query = model_query(context, models.Worker)

    # TODO: Once we stop creating the SENTINEL entry in the database (which
    # was only needed to support MySQL 5.5), we can drop this. Probably in the
    # A release or later
    if ignore_sentinel:
        # We don't want to retrieve the workers sentinel
        query = query.filter(models.Worker.resource_type != 'SENTINEL')

    if until:
        db_filters = list(db_filters) if db_filters else []
        # Since we set updated_at at creation time we don't need to check
        # created_at field.
        db_filters.append(models.Worker.updated_at <= until)

    if db_filters:
        query = query.filter(and_(*db_filters))

    if filters:
        query = query.filter_by(**filters)

    return query


def _worker_set_updated_at_field(values):
    updated_at = values.get('updated_at', timeutils.utcnow())
    if isinstance(updated_at, str):
        return
    values['updated_at'] = updated_at


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def worker_create(context, **values):
    """Create a worker entry from optional arguments."""
    _worker_set_updated_at_field(values)
    worker = models.Worker(**values)
    try:
        worker.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.WorkerExists(
            type=values.get('resource_type'),
            id=values.get('resource_id'),
        )
    return worker


@require_context
@main_context_manager.reader
def worker_get(context, **filters):
    """Get a worker or raise exception if it does not exist."""
    query = _worker_query(context, **filters)
    worker = query.first() if query else None
    if not worker:
        raise exception.WorkerNotFound(**filters)
    return worker


@require_context
@main_context_manager.reader
def worker_get_all(context, until=None, db_filters=None, **filters):
    """Get all workers that match given criteria."""
    query = _worker_query(
        context,
        until=until,
        db_filters=db_filters,
        **filters,
    )
    return query.all() if query else []


def _orm_worker_update(worker, values):
    if not worker:
        return
    for key, value in values.items():
        setattr(worker, key, value)


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def worker_update(context, id, filters=None, orm_worker=None, **values):
    """Update a worker with given values."""
    filters = filters or {}
    query = _worker_query(context, id=id, **filters)

    # If we want to update the orm_worker and we don't set the update_at field
    # we set it here instead of letting SQLAlchemy do it to be able to update
    # the orm_worker.
    _worker_set_updated_at_field(values)
    reference = orm_worker or models.Worker
    values['race_preventer'] = reference.race_preventer + 1
    result = query.update(values)
    if not result:
        raise exception.WorkerNotFound(id=id, **filters)
    _orm_worker_update(orm_worker, values)
    return result


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def worker_claim_for_cleanup(context, claimer_id, orm_worker):
    """Claim a worker entry for cleanup."""
    # We set updated_at value so we are sure we update the DB entry even if the
    # service_id is the same in the DB, thus flagging the claim.
    values = {
        'service_id': claimer_id,
        'race_preventer': orm_worker.race_preventer + 1,
        'updated_at': timeutils.utcnow(),
    }
    _worker_set_updated_at_field(values)

    # We only update the worker entry if it hasn't been claimed by other host
    # or thread
    query = _worker_query(
        context,
        status=orm_worker.status,
        service_id=orm_worker.service_id,
        race_preventer=orm_worker.race_preventer,
        until=orm_worker.updated_at,
        id=orm_worker.id,
    )

    result = query.update(values, synchronize_session=False)
    if result:
        _orm_worker_update(orm_worker, values)
    return result


@require_context
@main_context_manager.writer
def worker_destroy(context, **filters):
    """Delete a worker (no soft delete)."""
    query = _worker_query(context, **filters)
    return query.delete()


###############################


# TODO: (D Release) remove method and this comment
@enginefacade.writer
def remove_temporary_admin_metadata_data_migration(context, max_count):
    admin_meta_table = models.VolumeAdminMetadata
    query = model_query(context,
                        admin_meta_table.id).filter_by(key='temporary')
    total = query.count()
    ids_query = query.limit(max_count).subquery()
    update_args = {'synchronize_session': False}

    # We cannot use limit with update or delete so create a new query
    updated = model_query(context, admin_meta_table).\
        filter(admin_meta_table.id.in_(ids_query)).\
        update(admin_meta_table.delete_values(), **update_args)

    return total, updated


###############################


PAGINATION_HELPERS = {
    models.Volume: (_volume_get_query, _process_volume_filters, _volume_get),
    models.Snapshot: (_snaps_get_query, _process_snaps_filters, _snapshot_get),
    models.Backup: (_backups_get_query, _process_backups_filters, _backup_get),
    models.QualityOfServiceSpecs: (
        _qos_specs_get_query,
        _process_qos_specs_filters,
        _qos_specs_get,
    ),
    models.VolumeType: (
        _volume_type_get_query,
        _process_volume_types_filters,
        _volume_type_get_db_object,
    ),
    models.ConsistencyGroup: (
        _consistencygroups_get_query,
        _process_consistencygroups_filters,
        _consistencygroup_get,
    ),
    models.Message: (
        _messages_get_query,
        _process_messages_filters,
        _message_get,
    ),
    models.GroupType: (
        _group_type_get_query,
        _process_group_types_filters,
        _group_type_get_db_object,
    ),
    models.Group: (_groups_get_query, _process_groups_filters, _group_get),
    models.GroupSnapshot: (
        _group_snapshot_get_query,
        _process_group_snapshot_filters,
        _group_snapshot_get,
    ),
    models.VolumeAttachment: (
        _attachment_get_query,
        _process_attachment_filters,
        _attachment_get,
    ),
    models.Transfer: (
        _transfer_get_query,
        _process_transfer_filters,
        _transfer_get,
    ),
}


CALCULATE_COUNT_HELPERS = {
    'volume': (_volume_get_query, _process_volume_filters),
    'snapshot': (_snaps_get_query, _process_snaps_filters),
    'backup': (_backups_get_query, _process_backups_filters),
}


def get_projects(context, model, read_deleted="no"):
    return model_query(context, model, read_deleted=read_deleted).\
        with_entities(sa.Column('project_id')).distinct().all()
