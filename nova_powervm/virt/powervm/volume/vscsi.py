# Copyright 2015, 2016 IBM Corp.
#
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

from oslo_concurrency import lockutils
from oslo_log import log as logging
from taskflow import task

from nova_powervm import conf as cfg
from nova_powervm.virt.powervm import exception as p_exc
from nova_powervm.virt.powervm.i18n import _
from nova_powervm.virt.powervm.i18n import _LE
from nova_powervm.virt.powervm.i18n import _LI
from nova_powervm.virt.powervm.i18n import _LW
from nova_powervm.virt.powervm import vios
from nova_powervm.virt.powervm import vm
from nova_powervm.virt.powervm.volume import driver as v_driver

from pypowervm import const as pvm_const
from pypowervm.tasks import client_storage as pvm_c_stor
from pypowervm.tasks import hdisk
from pypowervm.tasks import scsi_mapper as tsk_map
from pypowervm.utils import transaction as tx
from pypowervm.wrappers import storage as pvm_stor
from pypowervm.wrappers import virtual_io_server as pvm_vios

import six

CONF = cfg.CONF
LOG = logging.getLogger(__name__)

UDID_KEY = 'target_UDID'

# A global variable that will cache the physical WWPNs on the system.
_vscsi_pfc_wwpns = None


class VscsiVolumeAdapter(v_driver.FibreChannelVolumeAdapter):
    """The vSCSI implementation of the Volume Adapter.

    vSCSI is the internal mechanism to link a given hdisk on the Virtual
    I/O Server to a Virtual Machine.  This volume driver will take the
    information from the driver and link it to a given virtual machine.
    """

    def __init__(self, adapter, host_uuid, instance, connection_info,
                 stg_ftsk=None):
        """Initializes the vSCSI Volume Adapter.

        :param adapter: The pypowervm adapter.
        :param host_uuid: The pypowervm UUID of the host.
        :param instance: The nova instance that the volume should connect to.
        :param connection_info: Comes from the BDM.
        :param stg_ftsk: (Optional) The pypowervm transaction FeedTask for the
                         I/O Operations.  If provided, the Virtual I/O Server
                         mapping updates will be added to the FeedTask.  This
                         defers the updates to some later point in time.  If
                         the FeedTask is not provided, the updates will be run
                         immediately when the respective method is executed.
        """
        super(VscsiVolumeAdapter, self).__init__(
            adapter, host_uuid, instance, connection_info, stg_ftsk=stg_ftsk)
        self._pfc_wwpns = None

    @classmethod
    def min_xags(cls):
        """List of pypowervm XAGs needed to support this adapter."""
        # SCSI mapping is for the connections between VIOS and client VM
        return [pvm_const.XAG.VIO_SMAP]

    @classmethod
    def vol_type(cls):
        """The type of volume supported by this type."""
        return 'vscsi'

    def pre_live_migration_on_destination(self, mig_data):
        """Perform pre live migration steps for the volume on the target host.

        This method performs any pre live migration that is needed.

        Certain volume connectors may need to pass data from the source host
        to the target.  This may be required to determine how volumes connect
        through the Virtual I/O Servers.

        This method will be called after the pre_live_migration_on_source
        method.  The data from the pre_live call will be passed in via the
        mig_data.  This method should put its output into the dest_mig_data.

        :param mig_data: Dict of migration data for the destination server.
                         If the volume connector needs to provide
                         information to the live_migration command, it
                         should be added to this dictionary.
        """
        volume_id = self.volume_id
        found = False

        # See the connect_volume for why this is a direct call instead of
        # using the tx_mgr.feed
        vios_feed = self.adapter.read(pvm_vios.VIOS.schema_type,
                                      xag=[pvm_const.XAG.VIO_STOR])
        vios_wraps = pvm_vios.VIOS.wrap(vios_feed)

        # Iterate through host vios list to find valid hdisks.
        for vios_w in vios_wraps:
            status, device_name, udid = self._discover_volume_on_vios(
                vios_w, volume_id)
            # If we found one, no need to check the others.
            found = found or hdisk.good_discovery(status, device_name)

        if not found:
            ex_args = dict(volume_id=volume_id,
                           instance_name=self.instance.name)
            raise p_exc.VolumePreMigrationFailed(**ex_args)

        mig_data['vscsi-' + volume_id] = udid

    def _cleanup_volume(self, udid):
        """Cleanup the hdisk associated with this udid."""

        if not udid:
            LOG.warning(_LW('Could not remove hdisk for volume: %s')
                        % self.volume_id)
            return

        LOG.info(_LI('Removing hdisk for udid: %s') % udid)

        def find_hdisk_to_remove(vios_w):
            device_name = vios_w.hdisk_from_uuid(udid)
            if device_name is None:
                return
            LOG.info(_LI('Removing %(hdisk)s from VIOS %(vios)s'),
                     {'hdisk': device_name, 'vios': vios_w.name})
            self._add_remove_hdisk(vios_w, device_name,
                                   stg_ftsk=rmv_hdisk_ftsk)

        # Create a feed task to get the vios, find the hdisk and remove it.
        rmv_hdisk_ftsk = tx.FeedTask(
            'find_hdisk_to_remove', pvm_vios.VIOS.getter(
                self.adapter, xag=[pvm_const.XAG.VIO_STOR]))
        # Find vios hdisks for this udid to remove.
        rmv_hdisk_ftsk.add_functor_subtask(
            find_hdisk_to_remove, flag_update=False)
        rmv_hdisk_ftsk.execute()

    def post_live_migration_at_source(self, migrate_data):
        """Performs post live migration for the volume on the source host.

        This method can be used to handle any steps that need to taken on
        the source host after the VM is on the destination.

        :param migrate_data: volume migration data
        """
        # Get the udid of the volume to remove the hdisk for.  We can't
        # use the connection information because LPM 'refreshes' it, which
        # wipes out our data, so we use the data from the destination host
        # to avoid having to discover the hdisk to get the udid.
        udid = migrate_data.get('vscsi-' + self.volume_id)
        self._cleanup_volume(udid)

    def cleanup_volume_at_destination(self, migrate_data):
        """Performs volume cleanup after LPM failure on the dest host.

        This method can be used to handle any steps that need to taken on
        the destination host after the migration has failed.

        :param migrate_data: migration data
        """
        udid = migrate_data.get('vscsi-' + self.volume_id)
        self._cleanup_volume(udid)

    def is_volume_on_vios(self, vios_w):
        """Returns whether or not the volume is on a VIOS.

        :param vios_w: The Virtual I/O Server wrapper.
        :return: True if the volume driver's volume is on the VIOS.  False
                 otherwise.
        :return: The udid of the device.
        """
        status, device_name, udid = self._discover_volume_on_vios(
            vios_w, self.volume_id)
        return hdisk.good_discovery(status, device_name), udid

    def _discover_volume_on_vios(self, vios_w, volume_id):
        """Discovers an hdisk on a single vios for the volume.

        :param vios_w: VIOS wrapper to process
        :param volume_id: Volume to discover
        :returns: Status of the volume or None
        :returns: Device name or None
        :returns: LUN or None
        """
        # Get the initiatior WWPNs, targets and Lun for the given VIOS.
        vio_wwpns, t_wwpns, lun = self._get_hdisk_itls(vios_w)

        # Build the ITL map and discover the hdisks on the Virtual I/O
        # Server (if any).
        itls = hdisk.build_itls(vio_wwpns, t_wwpns, lun)
        if len(itls) == 0:
            LOG.debug('No ITLs for VIOS %(vios)s for volume %(volume_id)s.'
                      % {'vios': vios_w.name, 'volume_id': volume_id})
            return None, None, None

        status, device_name, udid = hdisk.discover_hdisk(self.adapter,
                                                         vios_w.uuid, itls)

        if hdisk.good_discovery(status, device_name):
            LOG.info(_LI('Discovered %(hdisk)s on vios %(vios)s for '
                     'volume %(volume_id)s. Status code: %(status)s.'),
                     {'hdisk': device_name, 'vios': vios_w.name,
                      'volume_id': volume_id, 'status': str(status)})
        elif status == hdisk.LUAStatus.DEVICE_IN_USE:
            LOG.warning(_LW('Discovered device %(dev)s for volume %(volume)s '
                            'on %(vios)s is in use. Error code: %(status)s.'),
                        {'dev': device_name, 'volume': volume_id,
                         'vios': vios_w.name, 'status': str(status)})

        return status, device_name, udid

    def _connect_volume(self, slot_mgr):
        """Connects the volume.

        :param slot_mgr: A NovaSlotManager.  Used to store/retrieve the client
                         slots used when a volume is attached to the VM
        """

        def connect_volume_to_vio(vios_w):
            """Attempts to connect a volume to a given VIO.

            :param vios_w: The Virtual I/O Server wrapper to connect to.
            :return: True if the volume was connected.  False if the volume was
                     not (could be the Virtual I/O Server does not have
                     connectivity to the hdisk).
            """
            status, device_name, udid = self._discover_volume_on_vios(
                vios_w, self.volume_id)

            # Get the slot and LUA to assign.
            slot, lua = slot_mgr.build_map.get_vscsi_slot(vios_w, udid)

            if hdisk.good_discovery(status, device_name):
                # Found a hdisk on this Virtual I/O Server.  Add the action to
                # map it to the VM when the stg_ftsk is executed.
                with lockutils.lock(hash(self)):
                    self._add_append_mapping(vios_w.uuid, device_name,
                                             lpar_slot_num=slot, lua=lua)

                # Save the UDID for the disk in the connection info.  It is
                # used for the detach.
                self._set_udid(udid)
                LOG.debug('Device attached: %s', device_name)

                # Valid attachment
                return True

            return False

        # Its about to get weird.  The transaction manager has a list of
        # VIOSes.  We could use those, but they only have SCSI mappings (by
        # design).  They do not have storage (super expensive).
        #
        # We need the storage xag when we are determining which mappings to
        # add to the system.  But we don't want to tie it to the stg_ftsk.  If
        # we do, every retry, every etag gather, etc... takes MUCH longer.
        #
        # So we get the VIOSes with the storage xag here, separately, to save
        # the stg_ftsk from potentially having to run it multiple times.
        connect_ftsk = tx.FeedTask(
            'connect_volume_to_vio', pvm_vios.VIOS.getter(
                self.adapter, xag=[pvm_const.XAG.VIO_STOR,
                                   pvm_const.XAG.VIO_SMAP]))

        # Find valid hdisks and map to VM.
        connect_ftsk.add_functor_subtask(
            connect_volume_to_vio, provides='vio_modified', flag_update=False)

        ret = connect_ftsk.execute()

        # Check the number of VIOSes
        vioses_modified = 0
        for result in ret['wrapper_task_rets'].values():
            if result['vio_modified']:
                vioses_modified += 1

        partition_id = vm.get_vm_id(self.adapter, self.vm_uuid)

        # Update the slot information
        def set_slot_info():
            vios_wraps = self.stg_ftsk.feed
            for vios_w in vios_wraps:
                scsi_map = pvm_c_stor.udid_to_scsi_mapping(
                    vios_w, self._get_udid(), partition_id)
                if not scsi_map:
                    continue
                slot_mgr.register_vscsi_mapping(scsi_map)

        self._validate_vios_on_connection(vioses_modified)
        self.stg_ftsk.add_post_execute(task.FunctorTask(
            set_slot_info, name='hdisk_slot_%s' % self._get_udid()))

    def _validate_vios_on_connection(self, num_vioses_found):
        """Validates that the correct number of VIOSes were discovered.

        Certain environments may have redundancy requirements.  For PowerVM
        this is achieved by having multiple Virtual I/O Servers.  This method
        will check to ensure that the operator's requirements for redundancy
        have been met.  If not, a specific error message will be raised.

        :param num_vioses_found: The number of VIOSes the hdisk was found on.
        """
        # Is valid as long as the vios count exceeds the conf value.
        if num_vioses_found >= CONF.powervm.vscsi_vios_connections_required:
            return

        # Should have a custom message based on zero or 'some but not enough'
        # I/O Servers.
        if num_vioses_found == 0:
            msg = (_('Failed to discover valid hdisk on any Virtual I/O '
                     'Server for volume %(volume_id)s.') %
                   {'volume_id': self.volume_id})
        else:
            msg = (_('Failed to discover the hdisk on the required number of '
                     'Virtual I/O Servers.  Volume %(volume_id)s required '
                     '%(vios_req)d Virtual I/O Servers, but the disk was only '
                     'found on %(vios_act)d Virtual I/O Servers.') %
                   {'volume_id': self.volume_id, 'vios_act': num_vioses_found,
                    'vios_req': CONF.powervm.vscsi_vios_connections_required})
        LOG.error(msg)
        ex_args = {'volume_id': self.volume_id, 'reason': msg,
                   'instance_name': self.instance.name}
        raise p_exc.VolumeAttachFailed(**ex_args)

    def _disconnect_volume(self, slot_mgr):
        """Disconnect the volume.

        :param slot_mgr: A NovaSlotManager.  Used to delete the client slots
                         used when a volume is detached from the VM
        """
        def discon_vol_for_vio(vios_w):
            """Removes the volume from a specific Virtual I/O Server.

            :param vios_w: The VIOS wrapper.
            :return: True if a remove action was done against this VIOS.  False
                     otherwise.
            """
            LOG.debug("Disconnect volume %(vol)s from vios uuid %(uuid)s",
                      dict(vol=self.volume_id, uuid=vios_w.uuid))
            udid, device_name = None, None
            try:
                udid = self._get_udid()
                if not udid:
                    # We lost our bdm data. We'll need to discover it.
                    status, device_name, udid = self._discover_volume_on_vios(
                        vios_w, self.volume_id)

                    # If we have a device name, but not a udid, at this point
                    # we should not continue.  The hdisk is in a bad state
                    # in the I/O Server.  Subsequent scrub code on future
                    # deploys will clean this up.
                    if not hdisk.good_discovery(status, device_name):
                        LOG.warning(_LW(
                            "Disconnect Volume: The backing hdisk for volume "
                            "%(volume_id)s on Virtual I/O Server %(vios)s is "
                            "not in a valid state.  No disconnect "
                            "actions to be taken as volume is not healthy."),
                            {'volume_id': self.volume_id, 'vios': vios_w.name})
                        return False

                if udid and not device_name:
                    device_name = vios_w.hdisk_from_uuid(udid)

                if not device_name:
                    LOG.warning(_LW(
                        "Disconnect Volume: No mapped device found on Virtual "
                        "I/O Server %(vios)s for volume %(volume_id)s.  "
                        "Volume UDID: %(volume_uid)s"),
                        {'volume_uid': udid, 'volume_id': self.volume_id,
                         'vios': vios_w.name})
                    return False

            except Exception as e:
                LOG.warning(_LW(
                    "Disconnect Volume: Failed to find disk on Virtual I/O "
                    "Server %(vios_name)s for volume %(volume_id)s. Volume "
                    "UDID: %(volume_uid)s.  Error: %(error)s"),
                    {'error': e, 'volume_uid': udid, 'vios_name': vios_w.name,
                     'volume_id': self.volume_id})
                return False

            # We have found the device name
            LOG.info(_LI("Disconnect Volume: Discovered the device %(hdisk)s "
                         "on Virtual I/O Server %(vios_name)s for volume "
                         "%(volume_id)s.  Volume UDID: %(volume_uid)s."),
                     {'volume_uid': udid, 'volume_id': self.volume_id,
                      'vios_name': vios_w.name, 'hdisk': device_name})

            # Add the action to remove the mapping when the stg_ftsk is run.
            partition_id = vm.get_vm_id(self.adapter, self.vm_uuid)

            with lockutils.lock(hash(self)):
                self._add_remove_mapping(partition_id, vios_w.uuid,
                                         device_name, slot_mgr)

                # Add a step to also remove the hdisk
                self._add_remove_hdisk(vios_w, device_name)

            # Found a valid element to remove
            return True

        try:
            # See logic in _connect_volume for why this new FeedTask is here.
            discon_ftsk = tx.FeedTask(
                'discon_volume_from_vio', pvm_vios.VIOS.getter(
                    self.adapter, xag=[pvm_const.XAG.VIO_STOR]))
            # Find hdisks to disconnect
            discon_ftsk.add_functor_subtask(
                discon_vol_for_vio, provides='vio_modified', flag_update=False)
            ret = discon_ftsk.execute()

            # Warn if no hdisks disconnected.
            if not any([result['vio_modified']
                        for result in ret['wrapper_task_rets'].values()]):
                LOG.warning(_LW("Disconnect Volume: Failed to disconnect the "
                                "volume %(volume_id)s on ANY of the Virtual "
                                "I/O Servers for instance %(inst)s."),
                            {'inst': self.instance.name,
                             'volume_id': self.volume_id})

        except Exception as e:
            LOG.error(_LE('Cannot detach volumes from virtual machine: %s'),
                      self.vm_uuid)
            LOG.exception(_LE('Error: %s'), e)
            ex_args = {'volume_id': self.volume_id, 'reason': six.text_type(e),
                       'instance_name': self.instance.name}
            raise p_exc.VolumeDetachFailed(**ex_args)

    def _check_host_mappings(self, vios_wrap, device_name):
        """Checks if the given hdisk has multiple mappings

        :param vio_wrap: The Virtual I/O Server wrapper to remove the disk
                         from.
        :param device_name: The hdisk name to remove.

        :return: True is there are multiple instances using the given hdisk
        """
        vios_scsi_mappings = next(v.scsi_mappings for v in self.stg_ftsk.feed
                                  if v.uuid == vios_wrap.uuid)
        mappings = tsk_map.find_maps(
            vios_scsi_mappings, None,
            tsk_map.gen_match_func(pvm_stor.PV, names=[device_name]))

        LOG.info(_LI("%(num)d Storage Mappings found for %(dev)s"),
                 {'num': len(mappings), 'dev': device_name})
        # the mapping is still present as the task feed removes
        # the mapping later
        return len(mappings) > 1

    def _add_remove_hdisk(self, vio_wrap, device_name,
                          stg_ftsk=None):
        """Adds a post-mapping task to remove the hdisk from the VIOS.

        This removal is only done after the mapping updates have completed.
        This method is also used during migration to remove hdisks that remain
        on the source host after the VM is migrated to the destination.

        :param vio_wrap: The Virtual I/O Server wrapper to remove the disk
                         from.
        :param device_name: The hdisk name to remove.
        :param stg_ftsk: The feed task to add to. If None, then self.stg_ftsk
        """
        def rm_hdisk():
            LOG.info(_LI("Running remove for hdisk: '%s'") % device_name)
            try:
                # Attempt to remove the hDisk
                hdisk.remove_hdisk(self.adapter, CONF.host, device_name,
                                   vio_wrap.uuid)
            except Exception as e:
                # If there is a failure, log it, but don't stop the process
                LOG.warning(_LW("There was an error removing the hdisk "
                            "%(disk)s from the Virtual I/O Server."),
                            {'disk': device_name})
                LOG.warning(e)

        # Check if there are not multiple mapping for the device
        if not self._check_host_mappings(vio_wrap, device_name):
            name = 'rm_hdisk_%s_%s' % (vio_wrap.name, device_name)
            stg_ftsk = stg_ftsk or self.stg_ftsk
            stg_ftsk.add_post_execute(task.FunctorTask(rm_hdisk, name=name))
        else:
            LOG.info(_LI("hdisk %(disk)s is not removed because it has "
                         "existing storage mappings"), {'disk': device_name})

    @lockutils.synchronized('vscsi_wwpns')
    def wwpns(self):
        """Builds the WWPNs of the adapters that will connect the ports.

        :return: The list of WWPNs that need to be included in the zone set.
        """
        # Use a global variable so this is pulled once when the process starts.
        global _vscsi_pfc_wwpns
        if _vscsi_pfc_wwpns is None:
            _vscsi_pfc_wwpns = vios.get_physical_wwpns(self.adapter,
                                                       self.host_uuid)
        return _vscsi_pfc_wwpns

    def host_name(self):
        """Derives the host name that should be used for the storage device.

        :return: The host name.
        """
        return CONF.host

    def _add_remove_mapping(self, vm_uuid, vios_uuid, device_name, slot_mgr):
        """Adds a transaction to remove the storage mapping.

        :param vm_uuid: The UUID of the VM instance
        :param vios_uuid: The UUID of the vios for the pypowervm adapter.
        :param device_name: The The hdisk device name.
        :param slot_mgr: A NovaSlotManager.  Used to delete the client slots
                         used when a volume is detached from the VM.
        """
        def rm_func(vios_w):
            LOG.info(_LI("Removing vSCSI mapping from Physical Volume %(dev)s "
                         "to VM %(vm)s") % {'dev': device_name, 'vm': vm_uuid})
            removed_maps = tsk_map.remove_maps(
                vios_w, vm_uuid,
                tsk_map.gen_match_func(pvm_stor.PV, names=[device_name]))
            for rm_map in removed_maps:
                slot_mgr.drop_vscsi_mapping(rm_map)
            return removed_maps
        self.stg_ftsk.wrapper_tasks[vios_uuid].add_functor_subtask(rm_func)

    def _add_append_mapping(self, vios_uuid, device_name, lpar_slot_num=None,
                            lua=None):
        """Update the stg_ftsk to append the mapping to the VIOS.

        :param vios_uuid: The UUID of the vios for the pypowervm adapter.
        :param device_name: The The hdisk device name.
        :param lpar_slot_num: (Optional, Default:None) If specified, the client
                              lpar slot number to use on the mapping.  If left
                              as None, it will use the next available slot
                              number.
        :param lua: (Optional.  Default: None) Logical Unit Address to set on
                    the TargetDevice.  If None, the LUA will be assigned by the
                    server.  Should be specified for all of the VSCSIMappings
                    for a particular bus, or none of them.
        """
        def add_func(vios_w):
            LOG.info(_LI("Adding vSCSI mapping to Physical Volume %(dev)s "
                         "to VM %(vm)s") % {'dev': device_name,
                                            'vm': self.vm_uuid})
            pv = pvm_stor.PV.bld(self.adapter, device_name)
            v_map = tsk_map.build_vscsi_mapping(
                self.host_uuid, vios_w, self.vm_uuid, pv,
                lpar_slot_num=lpar_slot_num, lua=lua)
            return tsk_map.add_map(vios_w, v_map)
        self.stg_ftsk.wrapper_tasks[vios_uuid].add_functor_subtask(add_func)

    def _set_udid(self, udid):
        """This method will set the hdisk udid in the connection_info.

        :param udid: The hdisk target_udid to be stored in system_metadata
        """
        self.connection_info['data'][UDID_KEY] = udid

    def _get_hdisk_itls(self, vios_w):
        """Returns the mapped ITLs for the hdisk for the given VIOS.

        A PowerVM system may have multiple Virtual I/O Servers to virtualize
        the I/O to the virtual machines. Each Virtual I/O server may have their
        own set of initiator WWPNs, target WWPNs and Lun on which hdisk is
        mapped. It will determine and return the ITLs for the given VIOS.

        :param vios_w: A virtual I/O Server wrapper.
        :return: List of the i_wwpns that are part of the vios_w,
        :return: List of the t_wwpns that are part of the vios_w,
        :return: Target lun id of the hdisk for the vios_w.
        """
        it_map = self.connection_info['data']['initiator_target_map']
        i_wwpns = it_map.keys()

        active_wwpns = vios_w.get_active_pfc_wwpns()
        vio_wwpns = [x for x in i_wwpns if x in active_wwpns]

        t_wwpns = []
        for it_key in vio_wwpns:
            t_wwpns.extend(it_map[it_key])
        lun = self.connection_info['data']['target_lun']

        return vio_wwpns, t_wwpns, lun

    def _get_udid(self):
        """This method will return the hdisk udid stored in connection_info.

        :return: The target_udid associated with the hdisk
        """
        try:
            return self.connection_info['data'][UDID_KEY]
        except (KeyError, ValueError):
            # It's common to lose our specific data in the BDM.  The connection
            # information can be 'refreshed' by operations like LPM and resize
            LOG.info(_LI(u'Failed to retrieve device_id key from BDM for '
                         'volume id %s'), self.volume_id)
            return None
