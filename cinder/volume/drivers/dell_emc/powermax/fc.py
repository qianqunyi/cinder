# Copyright (c) 2020 Dell Inc. or its subsidiaries.
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

import ast

from oslo_log import log as logging

from cinder.common import constants
from cinder import coordination
from cinder import exception
from cinder import interface
from cinder.volume import driver
from cinder.volume.drivers.dell_emc.powermax import common
from cinder.volume.drivers.san import san
from cinder.zonemanager import utils as fczm_utils

LOG = logging.getLogger(__name__)


@interface.volumedriver
class PowerMaxFCDriver(san.SanDriver, driver.FibreChannelDriver):
    """FC Drivers for PowerMax using REST.

    Version history:

    .. code-block:: none

        1.0.0 - Initial driver
        1.1.0 - Multiple pools and thick/thin provisioning,
                performance enhancement.
        2.0.0 - Add driver requirement functions
        2.1.0 - Add consistency group functions
        2.1.1 - Fixed issue with mismatched config (bug #1442376)
        2.1.2 - Clean up failed clones (bug #1440154)
        2.1.3 - Fixed a problem with FAST support (bug #1435069)
        2.2.0 - Add manage/unmanage
        2.2.1 - Support for SE 8.0.3
        2.2.2 - Update Consistency Group
        2.2.3 - Pool aware scheduler(multi-pool) support
        2.2.4 - Create CG from CG snapshot
        2.3.0 - Name change for MV and SG for FAST (bug #1515181)
              - Fix for randomly choosing port group. (bug #1501919)
              - get_short_host_name needs to be called in find_device_number
                (bug #1520635)
              - Proper error handling for invalid SLOs (bug #1512795)
              - Extend Volume for VMAX3, SE8.1.0.3
              https://blueprints.launchpad.net/cinder/+spec/vmax3-extend-volume
              - Incorrect SG selected on an attach (#1515176)
              - Cleanup Zoning (bug #1501938)  NOTE: FC only
              - Last volume in SG fix
              - _remove_last_vol_and_delete_sg is not being called
                for VMAX3 (bug #1520549)
              - necessary updates for CG changes (#1534616)
              - Changing PercentSynced to CopyState (bug #1517103)
              - Getting iscsi ip from port in existing masking view
              - Replacement of EMCGetTargetEndpoints api (bug #1512791)
              - VMAX3 snapvx improvements (bug #1522821)
              - Operations and timeout issues (bug #1538214)
        2.4.0 - EMC VMAX - locking SG for concurrent threads (bug #1554634)
              - SnapVX licensing checks for VMAX3 (bug #1587017)
              - VMAX oversubscription Support (blueprint vmax-oversubscription)
              - QoS support (blueprint vmax-qos)
        2.5.0 - Attach and detach snapshot (blueprint vmax-attach-snapshot)
              - MVs and SGs not reflecting correct protocol (bug #1640222)
              - Storage assisted volume migration via retype
                (bp vmax-volume-migration)
              - Support for compression on All Flash
              - Volume replication 2.1 (bp add-vmax-replication)
              - rename and restructure driver (bp vmax-rename-dell-emc)
        3.0.0 - REST based driver
              - Retype (storage-assisted migration)
              - QoS support
              - Support for compression on All Flash
              - Support for volume replication
              - Support for live migration
              - Support for Generic Volume Group
        3.1.0 - Support for replication groups (Tiramisu)
              - Deprecate backend xml configuration
              - Support for async replication (vmax-replication-enhancements)
              - Support for SRDF/Metro (vmax-replication-enhancements)
              - Support for manage/unmanage snapshots
                (vmax-manage-unmanage-snapshot)
              - Support for revert to volume snapshot
        3.2.0 - Support for retyping replicated volumes (bp
                vmax-retype-replicated-volumes)
              - Support for multiattach volumes (bp vmax-allow-multi-attach)
              - Support for list manageable volumes and snapshots
                (bp/vmax-list-manage-existing)
              - Fix for SSL verification/cert application (bug #1772924)
              - Log VMAX metadata of a volume (bp vmax-metadata)
              - Fix for get-pools command (bug #1784856)
        4.0.0 - Fix for initiator retrieval and short hostname unmapping
                (bugs #1783855 #1783867)
              - Fix for HyperMax OS Upgrade Bug (bug #1790141)
              - Support for failover to secondary Unisphere
                (bp/vmax-unisphere-failover)
              - Rebrand from VMAX to PowerMax(bp/vmax-powermax-rebrand)
              - Change from 84 to 90 REST endpoints (bug #1808539)
              - Fix for PowerMax OS replication settings (bug #1812685)
              - Support for storage-assisted in-use retype
                (bp/powermax-storage-assisted-inuse-retype)
        4.1.0 - Changing from 90 to 91 rest endpoints
              - Support for Rapid TDEV Delete (bp powermax-tdev-deallocation)
              - PowerMax OS Metro formatted volumes fix (bug #1829876)
              - Support for Metro ODE (bp/powermax-metro-ode)
              - Removal of san_rest_port from PowerMax cinder.conf config
              - SnapVX noCopy mode enabled for all links
              - Volume/Snapshot backed metadata inclusion
              - Debug metadata compression and service level info fix
        4.2.0 - Support of Unisphere storage group and array tags
              - User defined override for short host name and port group name
                (bp powermax-user-defined-hostname-portgroup)
              - Switch to Unisphere REST API public replication endpoints
              - Support for multiple replication devices
              - Pools bug fix allowing 'None' variants (bug #1873253)
        4.3.0 - Changing from 91 to 92 REST endpoints
              - Support for Port Group and Port load balancing
                (bp powermax-port-load-balance)
              - Fix to enable legacy volumes to live migrate (#1867163)
              - Use of snap id instead of generation (bp powermax-snapset-ids)
              - Support for Failover Abilities (bp/powermax-failover-abilities)
        4.4.0 - Early check for status of port
        4.4.1 - Report trim/discard support
        4.5.0 - Add PowerMax v4 support
        4.5.1 - Add active/active compliance
        4.5.2 - Add 'disable_protected_snap' option
    """

    VERSION = "4.5.2"
    SUPPORTS_ACTIVE_ACTIVE = True

    # ThirdPartySystems wiki
    CI_WIKI_NAME = "DellEMC_PowerMAX_CI"

    driver_prefix = 'powermax'

    def __init__(self, *args, **kwargs):

        super(PowerMaxFCDriver, self).__init__(*args, **kwargs)
        self.active_backend_id = kwargs.get('active_backend_id', None)
        self.common = common.PowerMaxCommon(
            'FC',
            self.VERSION,
            configuration=self.configuration,
            active_backend_id=self.active_backend_id)
        self.performance = self.common.performance
        self.rest = self.common.rest
        self.zonemanager_lookup_service = fczm_utils.create_lookup_service()

    @classmethod
    def get_driver_options(cls):
        additional_opts = cls._get_oslo_driver_opts(
            'san_ip', 'san_login', 'san_password', 'driver_ssl_cert_verify',
            'max_over_subscription_ratio', 'reserved_percentage',
            'replication_device')
        return common.powermax_opts + additional_opts

    def check_for_setup_error(self):
        pass

    def _init_vendor_properties(self):
        return self.common.get_vendor_properties(self)

    def create_volume(self, volume):
        """Creates a PowerMax/VMAX volume.

        :param volume: the cinder volume object
        :returns: provider location dict
        """
        return self.common.create_volume(volume)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot.

        :param volume: the cinder volume object
        :param snapshot: the cinder snapshot object
        :returns: provider location dict
        """
        return self.common.create_volume_from_snapshot(
            volume, snapshot)

    def create_cloned_volume(self, volume, src_vref):
        """Creates a cloned volume.

        :param volume: the cinder volume object
        :param src_vref: the source volume reference
        :returns: provider location dict
        """
        return self.common.create_cloned_volume(volume, src_vref)

    def delete_volume(self, volume):
        """Deletes a PowerMax/VMAX volume.

        :param volume: the cinder volume object
        """
        self.common.delete_volume(volume)

    def create_snapshot(self, snapshot):
        """Creates a snapshot.

        :param snapshot: the cinder snapshot object
        :returns: provider location dict
        """
        src_volume = snapshot.volume
        return self.common.create_snapshot(snapshot, src_volume)

    def delete_snapshot(self, snapshot):
        """Deletes a snapshot.

        :param snapshot: the cinder snapshot object
        """
        src_volume = snapshot.volume
        self.common.delete_snapshot(snapshot, src_volume)

    def ensure_export(self, context, volume):
        """Driver entry point to get the export info for an existing volume.

        :param context: the context
        :param volume: the cinder volume object
        """
        pass

    def create_export(self, context, volume, connector):
        """Driver entry point to get the export info for a new volume.

        :param context: the context
        :param volume: the cinder volume object
        :param connector: the connector object
        """
        pass

    def remove_export(self, context, volume):
        """Driver entry point to remove an export for a volume.

        :param context: the context
        :param volume: the cinder volume object
        """
        pass

    @staticmethod
    def check_for_export(context, volume_id):
        """Make sure volume is exported.

        :param context: the context
        :param volume_id: the volume id
        """
        pass

    @coordination.synchronized('{self.driver_prefix}-{volume.id}')
    def initialize_connection(self, volume, connector):
        """Initializes the connection and returns connection info.

        Assign any created volume to a compute node/host so that it can be
        used from that host.

        The  driver returns a driver_volume_type of 'fibre_channel'.
        The target_wwn can be a single entry or a list of wwns that
        correspond to the list of remote wwn(s) that will export the volume.
        Example return values:

        .. code-block:: json

            {
                'driver_volume_type': 'fibre_channel'
                'data': {
                    'target_discovered': True,
                    'target_lun': 1,
                    'target_wwn': '1234567890123',
                }
            }

            or

            {
                'driver_volume_type': 'fibre_channel'
                'data': {
                    'target_discovered': True,
                    'target_lun': 1,
                    'target_wwn': ['1234567890123', '0987654321321'],
                }
            }

        :param volume: the cinder volume object
        :param connector: the connector object
        :returns: dict -- the target_wwns and initiator_target_map
        """
        device_info = self.common.initialize_connection(
            volume, connector)
        if device_info:
            conn_info = self.populate_data(device_info, volume, connector)
            fczm_utils.add_fc_zone(conn_info)
            return conn_info
        else:
            return {}

    def populate_data(self, device_info, volume, connector):
        """Populate data dict.

        Add relevant data to data dict, target_lun, target_wwn and
        initiator_target_map.
        :param device_info: device_info
        :param volume: the volume object
        :param connector: the connector object
        :returns: dict -- the target_wwns and initiator_target_map
        """
        device_number = device_info['hostlunid']
        target_wwns, init_targ_map = self._build_initiator_target_map(
            volume, connector, device_info)

        data = {'driver_volume_type': 'fibre_channel',
                'data': {'target_lun': device_number,
                         'target_discovered': True,
                         'target_wwn': target_wwns,
                         'discard': True,
                         'initiator_target_map': init_targ_map}}

        LOG.debug("Return FC data for zone addition: %(data)s.",
                  {'data': data})

        return data

    @coordination.synchronized('{self.driver_prefix}-{volume.id}')
    def terminate_connection(self, volume, connector, **kwargs):
        """Disallow connection from connector.

        Return empty data if other volumes are in the same zone.
        The FibreChannel ZoneManager doesn't remove zones
        if there isn't an initiator_target_map in the
        return of terminate_connection.

        :param volume: the volume object
        :param connector: the connector object
        :returns: dict -- the target_wwns and initiator_target_map if the
            zone is to be removed, otherwise empty
        """
        data = {'driver_volume_type': 'fibre_channel', 'data': {}}
        zoning_mappings = {}
        if connector:
            zoning_mappings = self._get_zoning_mappings(volume, connector)

        if zoning_mappings:
            self.common.terminate_connection(volume, connector)
            data = self._cleanup_zones(zoning_mappings)
        fczm_utils.remove_fc_zone(data)
        return data

    def _get_zoning_mappings(self, volume, connector):
        """Get zoning mappings by building up initiator/target map.

        :param volume: the volume object
        :param connector: the connector object
        :returns: dict -- the target_wwns and initiator_target_map if the
            zone is to be removed, otherwise empty
        """
        loc = volume.provider_location
        name = ast.literal_eval(loc)
        host_label = self.common.utils.get_host_name_label(
            connector['host'], self.common.powermax_short_host_name_template)
        zoning_mappings = {}
        try:
            array = name['array']
            device_id = name['device_id']
        except KeyError:
            array = name['keybindings']['SystemName'].split('+')[1].strip('-')
            device_id = name['keybindings']['DeviceID']
        LOG.debug("Start FC detach process for volume: %(volume)s.",
                  {'volume': volume.name})

        masking_views, is_metro = (
            self.common.get_masking_views_from_volume(
                array, volume, device_id, host_label))
        if not masking_views:
            # Backward compatibility with pre Ussuri short host name.
            host_label = self.common.utils.get_host_short_name(
                connector['host'])
            masking_views, is_metro = (
                self.common.get_masking_views_from_volume(
                    array, volume, device_id, host_label))
        if masking_views:
            portgroup = (
                self.common.get_port_group_from_masking_view(
                    array, masking_views[0]))
            initiator_group = (
                self.common.get_initiator_group_from_masking_view(
                    array, masking_views[0]))

            LOG.debug("Found port group: %(portGroup)s "
                      "in masking view %(maskingView)s.",
                      {'portGroup': portgroup,
                       'maskingView': masking_views[0]})
            # Map must be populated before the terminate_connection
            target_wwns, init_targ_map = self._build_initiator_target_map(
                volume, connector)
            zoning_mappings = {'port_group': portgroup,
                               'initiator_group': initiator_group,
                               'target_wwns': target_wwns,
                               'init_targ_map': init_targ_map,
                               'array': array}
        if is_metro:
            rep_data = volume.replication_driver_data
            name = ast.literal_eval(rep_data)
            try:
                metro_array = name['array']
                metro_device_id = name['device_id']
            except KeyError:
                LOG.error("Cannot get remote Metro device information "
                          "for zone cleanup. Attempting terminate "
                          "connection...")
            else:
                masking_views, __ = (
                    self.common.get_masking_views_from_volume(
                        metro_array, volume, metro_device_id, host_label))
                if masking_views:
                    metro_portgroup = (
                        self.common.get_port_group_from_masking_view(
                            metro_array, masking_views[0]))
                    metro_ig = (
                        self.common.get_initiator_group_from_masking_view(
                            metro_array, masking_views[0]))
                    zoning_mappings.update(
                        {'metro_port_group': metro_portgroup,
                         'metro_ig': metro_ig, 'metro_array': metro_array})
        if not masking_views:
            LOG.warning("Volume %(volume)s is not in any masking view.",
                        {'volume': volume.name})
        return zoning_mappings

    def _cleanup_zones(self, zoning_mappings):
        """Cleanup zones after terminate connection.

        :param zoning_mappings: zoning mapping dict
        :returns: data - dict
        """
        data = {'driver_volume_type': 'fibre_channel', 'data': {}}
        try:
            LOG.debug("Looking for masking views still associated with "
                      "Port Group %s.", zoning_mappings['port_group'])
            masking_views = self.common.get_common_masking_views(
                zoning_mappings['array'], zoning_mappings['port_group'],
                zoning_mappings['initiator_group'])
        except (KeyError, ValueError, TypeError):
            masking_views = []

        if masking_views:
            LOG.debug("Found %(numViews)d MaskingViews.",
                      {'numViews': len(masking_views)})
        else:  # no masking views found
            # Check if there any Metro masking views
            if zoning_mappings.get('metro_array'):
                masking_views = self.common.get_common_masking_views(
                    zoning_mappings['metro_array'],
                    zoning_mappings['metro_port_group'],
                    zoning_mappings['metro_ig'])
            if not masking_views:
                LOG.debug("No MaskingViews were found. Deleting zone.")
                data = {'driver_volume_type': 'fibre_channel',
                        'data': {'target_wwn': zoning_mappings['target_wwns'],
                                 'initiator_target_map':
                                     zoning_mappings['init_targ_map']}}

                LOG.debug("Return FC data for zone removal: %(data)s.",
                          {'data': data})

        return data

    def _build_initiator_target_map(self, volume, connector, device_info=None):
        """Build the target_wwns and the initiator target map.

        :param volume: the cinder volume object
        :param connector: the connector object
        :param device_info: device_info
        :returns: target_wwns -- list, init_targ_map -- dict
        """
        target_wwns, init_targ_map = [], {}
        initiator_wwns = connector['wwpns']
        fc_targets, metro_fc_targets = (
            self.common.get_target_wwns_from_masking_view(
                volume, connector))

        # If load balance is enabled we want to select only the FC target that
        # has the lowest load of all ports in selected port group.
        # Note: device_info in if condition as this method is called also for
        # terminate connection, we only want to calculate load on initialise
        # connection.
        if device_info and self.performance.config.get('load_balance'):
            try:
                array_id = device_info.get('array')
                masking_view = device_info.get('maskingview')
                # Get PG from MV
                port_group = self.rest.get_element_from_masking_view(
                    array_id, masking_view, portgroup=True)
                # Get port list from PG
                port_list = self.rest.get_port_ids(array_id, port_group)
                # Get lowest load port in PG
                load, metric, port = self.performance.process_port_load(
                    array_id, port_list)
                LOG.info("Lowest %(met)s load port is %(port)s: %(load)s",
                         {'met': metric, 'port': port, 'load': load})
                # Get target WWN
                port_details = self.rest.get_port(array_id, port)
                port_info = port_details.get('symmetrixPort')
                port_wwn = port_info.get('identifier')
                LOG.info("Port %(p)s WWN: %(wwn)s",
                         {'p': port, 'wwn': port_wwn})
                # Set lowest load port WWN as FC target for connection
                fc_targets = [port_wwn]
            except exception.VolumeBackendAPIException:
                LOG.error("There was an error calculating port load, "
                          "reverting to default target selection.")
                fc_targets, __ = (
                    self.common.get_target_wwns_from_masking_view(
                        volume, connector))

        if self.zonemanager_lookup_service:
            fc_targets.extend(metro_fc_targets)
            mapping = (
                self.zonemanager_lookup_service.
                get_device_mapping_from_network(initiator_wwns, fc_targets))
            for entry in mapping:
                map_d = mapping[entry]
                target_wwns.extend(map_d['target_port_wwn_list'])
                for initiator in map_d['initiator_port_wwn_list']:
                    init_targ_map[initiator] = map_d['target_port_wwn_list']
        else:  # No lookup service, pre-zoned case.
            target_wwns = fc_targets
            fc_targets.extend(metro_fc_targets)
            for initiator in initiator_wwns:
                init_targ_map[initiator] = fc_targets

        return list(set(target_wwns)), init_targ_map

    def extend_volume(self, volume, new_size):
        """Extend an existing volume.

        :param volume: the cinder volume object
        :param new_size: the required new size
        """
        self.common.extend_volume(volume, new_size)

    def _update_volume_stats(self):
        """Retrieve stats info from volume group."""
        LOG.debug("Updating volume stats")
        data = self.common.update_volume_stats()
        data['storage_protocol'] = constants.FC
        data['driver_version'] = self.VERSION
        self._stats = data

    def manage_existing(self, volume, external_ref):
        """Manages an existing PowerMax/VMAX Volume (import to Cinder).

        Renames the Volume to match the expected name for the volume.
        Also need to consider things like QoS, Emulation, account/tenant.
        :param volume: the volume object
        :param external_ref: the reference for the PowerMax/VMAX volume
        :returns: model_update
        """
        return self.common.manage_existing(volume, external_ref)

    def manage_existing_get_size(self, volume, external_ref):
        """Return size of an existing PowerMax/VMAX volume to manage_existing.

        :param self: reference to class
        :param volume: the volume object including the volume_type_id
        :param external_ref: reference to the existing volume
        :returns: size of the volume in GB
        """
        return self.common.manage_existing_get_size(volume, external_ref)

    def unmanage(self, volume):
        """Export PowerMax/VMAX volume from Cinder.

        Leave the volume intact on the backend array.
        """
        return self.common.unmanage(volume)

    def manage_existing_snapshot(self, snapshot, existing_ref):
        """Manage an existing PowerMax/VMAX Snapshot (import to Cinder).

        Renames the Snapshot to prefix it with OS- to indicate
        it is managed by Cinder.

        :param snapshot: the snapshot object
        :param existing_ref: the snapshot name on the backend PowerMax/VMAX
        :returns: model_update
        """
        return self.common.manage_existing_snapshot(snapshot, existing_ref)

    def manage_existing_snapshot_get_size(self, snapshot, existing_ref):
        """Return the size of the source volume for manage-existing-snapshot.

        :param snapshot: the snapshot object
        :param existing_ref: the snapshot name on the backend PowerMax/VMAX
        :returns: size of the source volume in GB
        """
        return self.common.manage_existing_snapshot_get_size(snapshot)

    def unmanage_snapshot(self, snapshot):
        """Export PowerMax/VMAX Snapshot from Cinder.

        Leaves the snapshot intact on the backend PowerMax/VMAX.

        :param snapshot: the snapshot object
        """
        self.common.unmanage_snapshot(snapshot)

    def get_manageable_volumes(self, cinder_volumes, marker, limit, offset,
                               sort_keys, sort_dirs):
        """Lists all manageable volumes.

        :param cinder_volumes: List of currently managed Cinder volumes.
                               Unused in driver.
        :param marker: Begin returning volumes that appear later in the volume
                       list than that represented by this reference.
        :param limit: Maximum number of volumes to return. Default=1000.
        :param offset: Number of volumes to skip after marker.
        :param sort_keys: Results sort key. Valid keys: size, reference.
        :param sort_dirs: Results sort direction. Valid dirs: asc, desc.
        :returns: List of dicts containing all manageable volumes.
        """
        return self.common.get_manageable_volumes(marker, limit, offset,
                                                  sort_keys, sort_dirs)

    def get_manageable_snapshots(self, cinder_snapshots, marker, limit, offset,
                                 sort_keys, sort_dirs):
        """Lists all manageable snapshots.

        :param cinder_snapshots: List of currently managed Cinder snapshots.
                                 Unused in driver.
        :param marker: Begin returning volumes that appear later in the
                       snapshot list than that represented by this reference.
        :param limit: Maximum number of snapshots to return. Default=1000.
        :param offset: Number of snapshots to skip after marker.
        :param sort_keys: Results sort key. Valid keys: size, reference.
        :param sort_dirs: Results sort direction. Valid dirs: asc, desc.
        :returns: List of dicts containing all manageable snapshots.
        """
        return self.common.get_manageable_snapshots(marker, limit, offset,
                                                    sort_keys, sort_dirs)

    def retype(self, ctxt, volume, new_type, diff, host):
        """Migrate volume to another host using retype.

        :param ctxt: context
        :param volume: the volume object including the volume_type_id
        :param new_type: the new volume type.
        :param diff: difference between old and new volume types.
            Unused in driver.
        :param host: the host dict holding the relevant
            target(destination) information
        :returns: boolean -- True if retype succeeded, False if error
        """
        return self.common.retype(volume, new_type, host)

    def failover_host(self, context, volumes, secondary_id=None, groups=None):
        """Failover volumes to a secondary host/ backend.

        :param context: the context
        :param volumes: the list of volumes to be failed over
        :param secondary_id: the backend to be failed over to, is 'default'
                             if fail back
        :param groups: replication groups
        :returns: secondary_id, volume_update_list, group_update_list
        """
        active_backend_id, volume_update_list, group_update_list = (
            self.common.failover(volumes, secondary_id, groups))
        self.common.failover_completed(secondary_id, False)
        return active_backend_id, volume_update_list, group_update_list

    def failover(self, context, volumes, secondary_id=None, groups=None):
        """Like failover but for a host that is clustered."""
        return self.common.failover(volumes, secondary_id, groups)

    def failover_completed(self, context, active_backend_id=None):
        """This method is called after failover for clustered backends."""
        return self.common.failover_completed(active_backend_id, True)

    def create_group(self, context, group):
        """Creates a generic volume group.

        :param context: the context
        :param group: the group object
        :returns: model_update
        """
        return self.common.create_group(context, group)

    def delete_group(self, context, group, volumes):
        """Deletes a generic volume group.

        :param context: the context
        :param group: the group object
        :param volumes: the member volumes
        """
        return self.common.delete_group(
            context, group, volumes)

    def create_group_snapshot(self, context, group_snapshot, snapshots):
        """Creates a group snapshot.

        :param context: the context
        :param group_snapshot: the grouop snapshot
        :param snapshots: snapshots list
        """
        return self.common.create_group_snapshot(context,
                                                 group_snapshot, snapshots)

    def delete_group_snapshot(self, context, group_snapshot, snapshots):
        """Deletes a group snapshot.

        :param context: the context
        :param group_snapshot: the grouop snapshot
        :param snapshots: snapshots list
        """
        return self.common.delete_group_snapshot(context,
                                                 group_snapshot, snapshots)

    def update_group(self, context, group,
                     add_volumes=None, remove_volumes=None):
        """Updates LUNs in generic volume group.

        :param context: the context
        :param group: the group object
        :param add_volumes: flag for adding volumes
        :param remove_volumes: flag for removing volumes
        """
        return self.common.update_group(group, add_volumes,
                                        remove_volumes)

    def create_group_from_src(
            self, context, group, volumes, group_snapshot=None,
            snapshots=None, source_group=None, source_vols=None):
        """Creates the volume group from source.

        :param context: the context
        :param group: the group object to be created
        :param volumes: volumes in the group
        :param group_snapshot: the source volume group snapshot
        :param snapshots: snapshots of the source volumes
        :param source_group: the dictionary of a volume group as source.
        :param source_vols: a list of volume dictionaries in the source_group.
        """
        return self.common.create_group_from_src(
            context, group, volumes, group_snapshot, snapshots, source_group,
            source_vols)

    def enable_replication(self, context, group, volumes):
        """Enable replication for a group.

        :param context: the context
        :param group: the group object
        :param volumes: the list of volumes
        :returns: model_update, None
        """
        return self.common.enable_replication(context, group, volumes)

    def disable_replication(self, context, group, volumes):
        """Disable replication for a group.

        :param context: the context
        :param group: the group object
        :param volumes: the list of volumes
        :returns: model_update, None
        """
        return self.common.disable_replication(context, group, volumes)

    def failover_replication(self, context, group, volumes,
                             secondary_backend_id=None):
        """Failover replication for a group.

        :param context: the context
        :param group: the group object
        :param volumes: the list of volumes
        :param secondary_backend_id: the secondary backend id - default None
        :returns: model_update, vol_model_updates
        """
        return self.common.failover_replication(
            context, group, volumes, secondary_backend_id)

    def revert_to_snapshot(self, context, volume, snapshot):
        """Revert volume to snapshot

        :param context: the context
        :param volume: the cinder volume object
        :param snapshot: the cinder snapshot object
        """
        self.common.revert_to_snapshot(volume, snapshot)

    @classmethod
    def clean_volume_file_locks(cls, volume_id):
        coordination.synchronized_remove(f'{cls.driver_prefix}-{volume_id}')
