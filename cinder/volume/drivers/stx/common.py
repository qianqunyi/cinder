#    Copyright 2014 Objectif Libre
#    Copyright 2015 Dot Hill Systems Corp.
#    Copyright 2016-2019 Seagate Technology or one of its affiliates
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
#
"""Volume driver common utilities for Seagate storage arrays."""

import base64
import uuid

from oslo_config import cfg
from oslo_log import log as logging

from cinder import exception
from cinder.i18n import _
from cinder.objects import fields
from cinder.volume import configuration
from cinder.volume import driver
import cinder.volume.drivers.stx.client as client
import cinder.volume.drivers.stx.exception as stx_exception
from cinder.volume import volume_utils

LOG = logging.getLogger(__name__)

common_opts = [
    cfg.StrOpt('seagate_pool_name',
               default='A',
               help="Pool or vdisk name to use for volume creation."),
    cfg.StrOpt('seagate_pool_type',
               choices=['linear', 'virtual'],
               default='virtual',
               help="linear (for vdisk) or virtual (for virtual pool)."),
]

iscsi_opts = [
    cfg.ListOpt('seagate_iscsi_ips',
                default=[],
                help="List of comma-separated target iSCSI IP addresses."),
]

CONF = cfg.CONF
CONF.register_opts(common_opts, group=configuration.SHARED_CONF_GROUP)
CONF.register_opts(iscsi_opts, group=configuration.SHARED_CONF_GROUP)


class STXCommon(object, metaclass=volume_utils.TraceWrapperMetaclass):
    VERSION = "2.0"

    stats = {}

    def __init__(self, config):
        self.config = config
        self.vendor_name = "Seagate"
        self.backend_name = self.config.seagate_pool_name
        self.backend_type = self.config.seagate_pool_type
        self.api_protocol = 'http'
        if self.config.driver_use_ssl:
            self.api_protocol = 'https'
        ssl_verify = self.config.driver_ssl_cert_verify
        if ssl_verify and self.config.driver_ssl_cert_path:
            ssl_verify = self.config.driver_ssl_cert_path
        self.client = client.STXClient(self.config.san_ip,
                                       self.config.san_login,
                                       self.config.san_password,
                                       self.api_protocol,
                                       ssl_verify)

    def get_version(self):
        return self.VERSION

    def do_setup(self, context):
        self.client_login()
        self._validate_backend()
        self._get_owner_info()
        self._get_serial_number()
        self.client_logout()

    def client_login(self):
        try:
            self.client.login()
        except stx_exception.ConnectionError as ex:
            msg = _("Failed to connect to %(vendor_name)s Array %(host)s: "
                    "%(err)s") % {'vendor_name': self.vendor_name,
                                  'host': self.config.san_ip,
                                  'err': str(ex)}
            LOG.error(msg)
            raise stx_exception.ConnectionError(message=msg)
        except stx_exception.AuthenticationError:
            msg = _("Failed to log on %s Array "
                    "(invalid login?).") % self.vendor_name
            LOG.error(msg)
            raise stx_exception.AuthenticationError(message=msg)

    def _get_serial_number(self):
        self.serialNumber = self.client.get_serial_number()

    def _get_owner_info(self):
        self.owner = self.client.get_owner_info(self.backend_name,
                                                self.backend_type)

    def _validate_backend(self):
        if not self.client.backend_exists(self.backend_name,
                                          self.backend_type):
            self.client_logout()
            raise stx_exception.InvalidBackend(backend=self.backend_name)

    def client_logout(self):
        self.client.logout()

    def _get_vol_name(self, volume_id):
        volume_name = self._encode_name(volume_id)
        return "v%s" % volume_name

    def _get_snap_name(self, snapshot_id):
        snapshot_name = self._encode_name(snapshot_id)
        return "s%s" % snapshot_name

    def _get_backend_volume_name(self, id, type='volume'):
        name = self._encode_name(id)
        return "%s%s" % (type[0], name)

    def _encode_name(self, name):
        """Get converted array volume name.

        Converts the openstack volume id from
        fceec30e-98bc-4ce5-85ff-d7309cc17cc2
        to
        v_O7DDpi8TOWF_9cwnMF
        We convert the 128(32*4) bits of the uuid into a 24 characters long
        base64 encoded string. This still exceeds the limit of 20 characters
        in some models so we return 19 characters because the
        _get_{vol,snap}_name functions prepend a character.
        """
        uuid_str = name.replace("-", "")
        vol_uuid = uuid.UUID('urn:uuid:%s' % uuid_str)
        vol_encoded = base64.urlsafe_b64encode(vol_uuid.bytes).decode('ascii')
        return vol_encoded[:19]

    def check_flags(self, options, required_flags):
        for flag in required_flags:
            if not getattr(options, flag, None):
                msg = _('%s configuration option is not set.') % flag
                LOG.error(msg)
                raise exception.InvalidInput(reason=msg)

    def create_volume(self, volume):
        self.client_login()
        # Use base64 to encode the volume name (UUID is too long)
        volume_name = self._get_vol_name(volume['id'])
        volume_size = "%dGiB" % volume['size']
        LOG.debug("Create Volume having display_name: %(display_name)s "
                  "name: %(name)s id: %(id)s size: %(size)s",
                  {'display_name': volume['display_name'],
                   'name': volume['name'],
                   'id': volume_name,
                   'size': volume_size, })
        try:
            self.client.create_volume(volume_name,
                                      volume_size,
                                      self.backend_name,
                                      self.backend_type)
        except stx_exception.RequestError as ex:
            LOG.exception("Creation of volume %s failed.", volume['id'])
            raise exception.Invalid(ex)

        finally:
            self.client_logout()

    def _assert_enough_space_for_copy(self, volume_size):
        """The array creates a snap pool before trying to copy the volume.

        The pool is 5.27GB or 20% of the volume size, whichever is larger.
        Verify that we have enough space for the pool and then copy.
        """
        pool_size = max(volume_size * 0.2, 5.27)
        required_size = pool_size + volume_size

        if required_size > self.stats['pools'][0]['free_capacity_gb']:
            raise stx_exception.NotEnoughSpace(backend=self.backend_name)

    def _assert_source_detached(self, volume):
        """The array requires volume to be detached before cloning."""
        if (volume['status'] != "available" or
                volume['attach_status'] == fields.VolumeAttachStatus.ATTACHED):
            LOG.error("Volume must be detached for clone operation.")
            raise exception.VolumeAttached(volume_id=volume['id'])

    def create_cloned_volume(self, volume, src_vref):
        self.get_volume_stats(True)
        self._assert_enough_space_for_copy(volume['size'])
        self._assert_source_detached(src_vref)
        LOG.debug("Cloning Volume %(source_id)s to (%(dest_id)s)",
                  {'source_id': src_vref['id'],
                   'dest_id': volume['id'], })

        if src_vref['name_id']:
            orig_name = self._get_vol_name(src_vref['name_id'])
        else:
            orig_name = self._get_vol_name(src_vref['id'])
        dest_name = self._get_vol_name(volume['id'])

        self.client_login()
        try:
            self.client.copy_volume(orig_name, dest_name,
                                    self.backend_name, self.backend_type)
        except stx_exception.RequestError as ex:
            LOG.exception("Cloning of volume %s failed.",
                          src_vref['id'])
            raise exception.Invalid(ex)
        finally:
            self.client_logout()

        if volume['size'] > src_vref['size']:
            self.extend_volume(volume, volume['size'])

    def create_volume_from_snapshot(self, volume, snapshot):
        self.get_volume_stats(True)
        self._assert_enough_space_for_copy(volume['size'])
        LOG.debug("Creating Volume from snapshot %(source_id)s to "
                  "(%(dest_id)s)", {'source_id': snapshot['id'],
                                    'dest_id': volume['id'], })

        orig_name = self._get_snap_name(snapshot['id'])
        dest_name = self._get_vol_name(volume['id'])
        self.client_login()
        try:
            self.client.copy_volume(orig_name, dest_name,
                                    self.backend_name, self.backend_type)
        except stx_exception.RequestError as ex:
            LOG.exception("Create volume failed from snapshot: %s",
                          snapshot['id'])
            raise exception.Invalid(ex)
        finally:
            self.client_logout()

        if volume['size'] > snapshot['volume_size']:
            self.extend_volume(volume, volume['size'])

    def delete_volume(self, volume):
        LOG.debug("Deleting Volume: %s", volume['id'])
        if volume['name_id']:
            volume_name = self._get_vol_name(volume['name_id'])
        else:
            volume_name = self._get_vol_name(volume['id'])

        self.client_login()
        try:
            self.client.delete_volume(volume_name)
        except stx_exception.RequestError as ex:
            # if the volume wasn't found, ignore the error
            if 'The volume was not found on this system.' in ex.args:
                return
            LOG.exception("Deletion of volume %s failed.", volume['id'])
            raise exception.Invalid(ex)
        finally:
            self.client_logout()

    def get_volume_stats(self, refresh):
        if refresh:
            self.client_login()
            try:
                self._update_volume_stats()
            finally:
                self.client_logout()
        return self.stats

    def _update_volume_stats(self):
        # storage_protocol and volume_backend_name are
        # set in the child classes
        stats = {'driver_version': self.VERSION,
                 'storage_protocol': None,
                 'vendor_name': self.vendor_name,
                 'volume_backend_name': None,
                 'pools': []}

        pool = {'QoS_support': False, 'multiattach': True}
        try:
            src_type = "%sVolumeDriver" % self.vendor_name
            backend_stats = self.client.backend_stats(self.backend_name,
                                                      self.backend_type)
            pool.update(backend_stats)
            pool['location_info'] = ('%s:%s:%s:%s' %
                                     (src_type,
                                      self.serialNumber,
                                      self.backend_name,
                                      self.owner))
            pool['pool_name'] = self.backend_name
        except stx_exception.RequestError:
            err = (_("Unable to get stats for backend_name: %s") %
                   self.backend_name)
            LOG.exception(err)
            raise exception.Invalid(err)

        stats['pools'].append(pool)
        self.stats = stats

    def _assert_connector_ok(self, connector, connector_element):
        if not connector[connector_element]:
            msg = _("Connector does not provide: %s") % connector_element
            LOG.error(msg)
            raise exception.InvalidInput(reason=msg)

    def map_volume(self, volume, connector, connector_element):
        self._assert_connector_ok(connector, connector_element)
        if volume['name_id']:
            volume_name = self._get_vol_name(volume['name_id'])
        else:
            volume_name = self._get_vol_name(volume['id'])
        try:
            data = self.client.map_volume(volume_name,
                                          connector,
                                          connector_element)
            return data
        except stx_exception.RequestError as ex:
            LOG.exception("Error mapping volume: %s", volume_name)
            raise exception.Invalid(ex)

    def unmap_volume(self, volume, connector, connector_element):
        self._assert_connector_ok(connector, connector_element)
        if volume['name_id']:
            volume_name = self._get_vol_name(volume['name_id'])
        else:
            volume_name = self._get_vol_name(volume['id'])

        self.client_login()
        try:
            self.client.unmap_volume(volume_name,
                                     connector,
                                     connector_element)
        except stx_exception.RequestError as ex:
            LOG.exception("Error unmapping volume: %s", volume_name)
            raise exception.Invalid(ex)
        finally:
            self.client_logout()

    def get_active_fc_target_ports(self):
        try:
            return self.client.get_active_fc_target_ports()
        except stx_exception.RequestError as ex:
            LOG.exception("Error getting active FC target ports.")
            raise exception.Invalid(ex)

    def get_active_iscsi_target_iqns(self):
        try:
            return self.client.get_active_iscsi_target_iqns()
        except stx_exception.RequestError as ex:
            LOG.exception("Error getting active ISCSI target iqns.")
            raise exception.Invalid(ex)

    def get_active_iscsi_target_portals(self):
        try:
            return self.client.get_active_iscsi_target_portals()
        except stx_exception.RequestError as ex:
            LOG.exception("Error getting active ISCSI target portals.")
            raise exception.Invalid(ex)

    def create_snapshot(self, snapshot):
        LOG.debug("Creating snapshot (%(snap_id)s) from %(volume_id)s)",
                  {'snap_id': snapshot['id'],
                   'volume_id': snapshot['volume_id'], })
        if snapshot['volume']['name_id']:
            vol_name = self._get_vol_name(snapshot['volume']['name_id'])
        else:
            vol_name = self._get_vol_name(snapshot['volume_id'])
        snap_name = self._get_snap_name(snapshot['id'])

        self.client_login()
        try:
            self.client.create_snapshot(vol_name, snap_name)
        except stx_exception.RequestError as ex:
            LOG.exception("Creation of snapshot failed for volume: %s",
                          snapshot['volume_id'])
            raise exception.Invalid(ex)
        finally:
            self.client_logout()

    def delete_snapshot(self, snapshot):
        snap_name = self._get_snap_name(snapshot['id'])
        LOG.debug("Deleting snapshot (%s)", snapshot['id'])

        self.client_login()
        try:
            self.client.delete_snapshot(snap_name, self.backend_type)
        except stx_exception.RequestError as ex:
            # if the volume wasn't found, ignore the error
            if 'The volume was not found on this system.' in ex.args:
                return
            LOG.exception("Deleting snapshot %s failed", snapshot['id'])
            raise exception.Invalid(ex)
        finally:
            self.client_logout()

    def extend_volume(self, volume, new_size):
        if volume['name_id']:
            volume_name = self._get_vol_name(volume['name_id'])
        else:
            volume_name = self._get_vol_name(volume['id'])
        old_size = self.client.get_volume_size(volume_name)
        growth_size = int(new_size) - old_size
        LOG.debug("Extending Volume %(volume_name)s from %(old_size)s to "
                  "%(new_size)s, by %(growth_size)s GiB.",
                  {'volume_name': volume_name,
                   'old_size': old_size,
                   'new_size': new_size,
                   'growth_size': growth_size, })
        if growth_size < 1:
            return
        self.client_login()
        try:
            self.client.extend_volume(volume_name, "%dGiB" % growth_size)
        except stx_exception.RequestError as ex:
            LOG.exception("Extension of volume %s failed.", volume['id'])
            raise exception.Invalid(ex)
        finally:
            self.client_logout()

    def get_chap_record(self, initiator_name):
        try:
            return self.client.get_chap_record(initiator_name)
        except stx_exception.RequestError as ex:
            LOG.exception("Error getting chap record.")
            raise exception.Invalid(ex)

    def create_chap_record(self, initiator_name, chap_secret):
        try:
            self.client.create_chap_record(initiator_name, chap_secret)
        except stx_exception.RequestError as ex:
            LOG.exception("Error creating chap record.")
            raise exception.Invalid(ex)

    def migrate_volume(self, volume, host):
        """Migrate directly if source and dest are managed by same storage.

        :param volume: A dictionary describing the volume to migrate
        :param host: A dictionary describing the host to migrate to, where
                     host['host'] is its name, and host['capabilities'] is a
                     dictionary of its reported capabilities.
        :returns: (False, None) if the driver does not support migration,
                 (True, None) if successful

        """
        false_ret = (False, None)
        if volume['attach_status'] == fields.VolumeAttachStatus.ATTACHED:
            return false_ret
        if 'location_info' not in host['capabilities']:
            return false_ret
        info = host['capabilities']['location_info']
        try:
            (dest_type, dest_id,
             dest_back_name, dest_owner) = info.split(':')
        except ValueError:
            return false_ret

        reqd_dest_type = '%sVolumeDriver' % self.vendor_name
        if not (dest_type == reqd_dest_type and
                dest_id == self.serialNumber and
                dest_owner == self.owner):
            return false_ret
        if volume['name_id']:
            source_name = self._get_vol_name(volume['name_id'])
        else:
            source_name = self._get_vol_name(volume['id'])
        # the array does not support duplicate names
        dest_name = "m%s" % source_name[1:]

        self.client_login()
        try:
            self.client.copy_volume(source_name, dest_name,
                                    dest_back_name, self.backend_type)
            self.client.delete_volume(source_name)
            self.client.modify_volume_name(dest_name, source_name)
            return (True, None)
        except stx_exception.RequestError as ex:
            LOG.exception("Error migrating volume: %s", source_name)
            raise exception.Invalid(ex)
        finally:
            self.client_logout()

    def retype(self, volume, new_type, diff, host):
        ret = self.migrate_volume(volume, host)
        return ret[0]

    def manage_existing(self, volume, existing_ref):
        """Manage an existing non-openstack array volume

        existing_ref is a dictionary of the form:
        {'source-name': <name of the existing volume>}
        """
        target_vol_name = existing_ref['source-name']
        modify_target_vol_name = self._get_vol_name(volume['id'])

        self.client_login()
        try:
            self.client.modify_volume_name(target_vol_name,
                                           modify_target_vol_name)
        except stx_exception.RequestError as ex:
            LOG.exception("Error manage existing volume.")
            raise exception.Invalid(ex)
        finally:
            self.client_logout()

    def manage_existing_snapshot(self, snapshot, existing_ref):
        """Import an existing snapshot into Cinder."""

        old_snap_name = existing_ref['source-name']
        new_snap_name = self._get_snap_name(snapshot.id)
        LOG.info("Renaming existing snapshot %(old_name)s to "
                 "%(new_name)s", {"old_name": old_snap_name,
                                  "new_name": new_snap_name})

        self.client_login()
        try:
            self.client.modify_volume_name(old_snap_name,
                                           new_snap_name)
        except stx_exception.RequestError as ex:
            LOG.exception("Error managing existing snapshot.")
            raise exception.Invalid(ex)
        finally:
            self.client_logout()

        return None

    def manage_existing_get_size(self, volume, existing_ref):
        """Return size of volume to be managed by manage_existing.

        existing_ref is a dictionary of the form:
        {'source-name': <name of the volume>}
        """

        target_vol_name = existing_ref['source-name']

        self.client_login()
        try:
            size = self.client.get_volume_size(target_vol_name)
            return size
        except stx_exception.RequestError as ex:
            LOG.exception("Error manage existing get volume size.")
            raise exception.Invalid(ex)
        finally:
            self.client_logout()

    def manage_existing_snapshot_get_size(self, snapshot, existing_ref):
        """Return size of volume to be managed by manage_existing."""
        return self.manage_existing_get_size(snapshot, existing_ref)

    def _get_manageable_vols(self, cinder_resources, resource_type,
                             marker, limit, offset, sort_keys,
                             sort_dirs):
        """List volumes or snapshots on the backend."""

        # We can't translate a backend volume name into a Cinder id
        # directly, so we create a map to do it.
        volume_name_to_id = {}
        for resource in cinder_resources:
            key = self._get_backend_volume_name(resource['id'], resource_type)
            value = resource['id']
            volume_name_to_id[key] = value

        self.client_login()
        try:
            vols = self.client.get_volumes(filter_type=resource_type)
        except stx_exception.RequestError as ex:
            LOG.exception("Error getting manageable volumes.")
            raise exception.Invalid(ex)
        finally:
            self.client_logout()

        entries = []
        for vol in vols.values():
            vol_info = {'reference': {'source-name': vol['name']},
                        'size': vol['size'],
                        'cinder_id': None,
                        'extra_info': None}

            potential_id = volume_name_to_id.get(vol['name'])
            if potential_id:
                vol_info['safe_to_manage'] = False
                vol_info['reason_not_safe'] = 'already managed'
                vol_info['cinder_id'] = potential_id
            elif vol['mapped']:
                vol_info['safe_to_manage'] = False
                vol_info['reason_not_safe'] = '%s in use' % resource_type
            else:
                vol_info['safe_to_manage'] = True
                vol_info['reason_not_safe'] = None

            if resource_type == 'snapshot':
                origin = vol['parent']
                vol_info['source_reference'] = {'source-name': origin}

            entries.append(vol_info)

        return volume_utils.paginate_entries_list(entries, marker, limit,
                                                  offset, sort_keys, sort_dirs)

    def get_manageable_volumes(self, cinder_volumes, marker, limit, offset,
                               sort_keys, sort_dirs):
        return self._get_manageable_vols(cinder_volumes, 'volume',
                                         marker, limit,
                                         offset, sort_keys, sort_dirs)

    def get_manageable_snapshots(self, cinder_snapshots, marker, limit, offset,
                                 sort_keys, sort_dirs):
        return self._get_manageable_vols(cinder_snapshots, 'snapshot',
                                         marker, limit,
                                         offset, sort_keys, sort_dirs)

    @staticmethod
    def get_driver_options():
        additional_opts = driver.BaseVD._get_oslo_driver_opts(
            'san_ip', 'san_login', 'san_password', 'driver_use_ssl',
            'driver_ssl_cert_verify', 'driver_ssl_cert_path')
        return common_opts + additional_opts
