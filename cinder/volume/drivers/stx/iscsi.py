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

from oslo_log import log as logging

from cinder.common import constants
from cinder import exception
from cinder.i18n import _
import cinder.volume.driver
import cinder.volume.drivers.san.san as san
import cinder.volume.drivers.stx.common as common
import cinder.volume.drivers.stx.exception as stx_exception


DEFAULT_ISCSI_PORT = "3260"
LOG = logging.getLogger(__name__)


class STXISCSIDriver(cinder.volume.driver.ISCSIDriver):
    """OpenStack iSCSI Cinder driver for Seagate storage arrays.

    .. code:: text

      Version history:
          0.1    - Base structure for DotHill iSCSI drivers based on HPMSA FC
                   drivers:
                       "https://github.com/openstack/cinder/tree/stable/juno/
                        cinder/volume/drivers/san/hp"
          1.0    - Version developed for DotHill arrays with the following
                   modifications:
                       - added iSCSI support
                       - added CHAP support in iSCSI
                       - added support for v3 API(virtual pool feature)
                       - added support for retype volume
                       - added support for manage/unmanage volume
                       - added https support
          1.6    - Add management path redundancy and reduce load placed
                   on management controller.
          1.7    - Modified so it can't be invoked except as a superclass
          2.0    - Reworked to create a new Seagate (STX) array driver.
    """

    VERSION = "2.0"

    CI_WIKI_NAME = 'Seagate_CI'

    def __init__(self, *args, **kwargs):
        super(STXISCSIDriver, self).__init__(*args, **kwargs)
        self.common = None
        self.configuration.append_config_values(san.san_opts)
        if type(self) is not STXISCSIDriver:
            return
        self.configuration.append_config_values(common.common_opts)
        self.configuration.append_config_values(common.iscsi_opts)
        self.iscsi_ips = self.configuration.seagate_iscsi_ips

    def _init_common(self):
        return common.STXCommon(self.configuration)

    def _check_flags(self):
        required_flags = ['san_ip', 'san_login', 'san_password']
        self.common.check_flags(self.configuration, required_flags)

    def do_setup(self, context):
        self.common = self._init_common()
        self._check_flags()
        self.common.do_setup(context)
        self.initialize_iscsi_ports()

    def initialize_iscsi_ports(self):
        iscsi_ips = []
        if self.iscsi_ips:
            for ip_addr in self.iscsi_ips:
                ip = ip_addr.split(':')
                if len(ip) == 1:
                    iscsi_ips.append([ip_addr, DEFAULT_ISCSI_PORT])
                elif len(ip) == 2:
                    iscsi_ips.append([ip[0], ip[1]])
                else:
                    msg = _("Invalid IP address format: '%s'") % ip_addr
                    LOG.error(msg)
                    raise exception.InvalidInput(reason=(msg))
            self.iscsi_ips = iscsi_ips
        else:
            msg = _('At least one valid iSCSI IP address must be set.')
            LOG.error(msg)
            raise exception.InvalidInput(reason=(msg))

    def check_for_setup_error(self):
        self._check_flags()

    def create_volume(self, volume):
        self.common.create_volume(volume)

    def create_volume_from_snapshot(self, volume, src_vref):
        self.common.create_volume_from_snapshot(volume, src_vref)

    def create_cloned_volume(self, volume, src_vref):
        self.common.create_cloned_volume(volume, src_vref)

    def delete_volume(self, volume):
        self.common.delete_volume(volume)

    def initialize_connection(self, volume, connector):
        self.common.client_login()
        try:
            data = {}
            data['target_lun'] = self.common.map_volume(volume,
                                                        connector,
                                                        'initiator')
            iqns = self.common.get_active_iscsi_target_iqns()
            data['target_discovered'] = True
            data['target_iqn'] = iqns[0]
            iscsi_portals = self.common.get_active_iscsi_target_portals()

            for ip_port in self.iscsi_ips:
                if (ip_port[0] in iscsi_portals):
                    data['target_portal'] = ":".join(ip_port)
                    break

            if 'target_portal' not in data:
                raise stx_exception.NotTargetPortal()

            if self.configuration.use_chap_auth:
                chap_secret = self.common.get_chap_record(
                    connector['initiator']
                )
                if not chap_secret:
                    chap_secret = self.create_chap_record(
                        connector['initiator']
                    )
                data['auth_password'] = chap_secret
                data['auth_username'] = connector['initiator']
                data['auth_method'] = 'CHAP'

            info = {'driver_volume_type': 'iscsi',
                    'data': data}
            return info
        finally:
            self.common.client_logout()

    def terminate_connection(self, volume, connector, **kwargs):
        if type(connector) is dict and 'initiator' in connector:
            # multiattach volumes cannot be unmapped here, but will
            # be implicity unmapped when the volume is deleted.
            if not volume.get('multiattach'):
                self.common.unmap_volume(volume, connector, 'initiator')

    def get_volume_stats(self, refresh=False):
        stats = self.common.get_volume_stats(refresh)
        stats['storage_protocol'] = constants.ISCSI
        stats['driver_version'] = self.VERSION
        backend_name = self.configuration.safe_get('volume_backend_name')
        stats['volume_backend_name'] = (backend_name or
                                        self.__class__.__name__)
        return stats

    def create_export(self, context, volume, connector=None):
        pass

    def ensure_export(self, context, volume):
        pass

    def remove_export(self, context, volume):
        pass

    def create_snapshot(self, snapshot):
        self.common.create_snapshot(snapshot)

    def delete_snapshot(self, snapshot):
        self.common.delete_snapshot(snapshot)

    def extend_volume(self, volume, new_size):
        self.common.extend_volume(volume, new_size)

    def create_chap_record(self, initiator_name):
        chap_secret = self.configuration.chap_password
        # Chap secret length should be 12 to 16 characters
        if 12 <= len(chap_secret) <= 16:
            self.common.create_chap_record(initiator_name, chap_secret)
        else:
            msg = _('CHAP secret should be 12-16 bytes.')
            LOG.error(msg)
            raise exception.InvalidInput(reason=(msg))
        return chap_secret

    def retype(self, context, volume, new_type, diff, host):
        return self.common.retype(volume, new_type, diff, host)

    def manage_existing(self, volume, existing_ref):
        self.common.manage_existing(volume, existing_ref)

    def manage_existing_get_size(self, volume, existing_ref):
        return self.common.manage_existing_get_size(volume, existing_ref)

    def manage_existing_snapshot(self, snapshot, existing_ref):
        return self.common.manage_existing_snapshot(snapshot, existing_ref)

    def manage_existing_snapshot_get_size(self, snapshot, existing_ref):
        return self.common.manage_existing_snapshot_get_size(snapshot,
                                                             existing_ref)

    def unmanage(self, volume):
        pass

    def unmanage_snapshot(self, snapshot):
        pass

    def get_manageable_volumes(self, cinder_volumes, marker, limit, offset,
                               sort_keys, sort_dirs):
        return self.common.get_manageable_volumes(cinder_volumes,
                                                  marker, limit, offset,
                                                  sort_keys, sort_dirs)

    def get_manageable_snapshots(self, cinder_snapshots, marker, limit,
                                 offset, sort_keys, sort_dirs):
        return self.common.get_manageable_snapshots(cinder_snapshots,
                                                    marker, limit, offset,
                                                    sort_keys, sort_dirs)

    @staticmethod
    def get_driver_options():
        return (common.STXCommon.get_driver_options() + common.iscsi_opts)
