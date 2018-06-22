# Copyright IBM Corp. and contributors
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

from nova_powervm import conf as cfg
from nova_powervm.virt.powervm import exception as p_exc
from nova_powervm.virt.powervm import vm
from nova_powervm.virt.powervm.volume import driver as v_driver
from nova_powervm.virt.powervm.volume import volume

from pypowervm import const as pvm_const
from pypowervm.tasks import hdisk
from pypowervm.tasks import partition as pvm_tpar
from pypowervm.utils import transaction as tx
from pypowervm.wrappers import virtual_io_server as pvm_vios

import six


CONF = cfg.CONF
LOG = logging.getLogger(__name__)

UDID_KEY = 'target_UDID'

# A global variable that will cache the physical WWPNs on the system.
_vscsi_pfc_wwpns = None


class PVVscsiFCVolumeAdapter(volume.VscsiVolumeAdapter,
                             v_driver.FibreChannelVolumeAdapter):
    """The vSCSI implementation of the Volume Adapter.

    For physical volumes, hosted to the VIOS through Fibre Channel, that
    connect to the VMs with vSCSI.

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
        super(PVVscsiFCVolumeAdapter, self).__init__(
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
        vios_wraps = pvm_vios.VIOS.get(self.adapter,
                                       xag=[pvm_const.XAG.VIO_STOR])

        # Iterate through host vios list to find valid hdisks.
        for vios_w in vios_wraps:
            status, device_name, udid = self._discover_volume_on_vios(
                vios_w, volume_id)
            # If we found one, no need to check the others.
            found = found or hdisk.good_discovery(status, device_name)
            # if valid udid is returned save in mig_data
            volume_key = 'vscsi-' + volume_id
            if udid is not None:
                mig_data[volume_key] = udid

        if not found or volume_key not in mig_data:
            ex_args = dict(volume_id=volume_id,
                           instance_name=self.instance.name)
            raise p_exc.VolumePreMigrationFailed(**ex_args)

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

    def extend_volume(self):
        # The compute node does not need to take any additional steps for the
        # client to see the extended volume.
        pass

    def _discover_volume_on_vios(self, vios_w, volume_id):
        """Discovers an hdisk on a single vios for the volume.

        :param vios_w: VIOS wrapper to process
        :param volume_id: Volume to discover
        :returns: Status of the volume or None
        :returns: Device name or None
        :returns: UDID or None
        """
        # Get the initiatior WWPNs, targets and Lun for the given VIOS.
        vio_wwpns, t_wwpns, lun = self._get_hdisk_itls(vios_w)

        # Build the ITL map and discover the hdisks on the Virtual I/O
        # Server (if any).
        itls = hdisk.build_itls(vio_wwpns, t_wwpns, lun)
        if len(itls) == 0:
            LOG.debug('No ITLs for VIOS %(vios)s for volume %(volume_id)s.',
                      {'vios': vios_w.name, 'volume_id': volume_id},
                      instance=self.instance)
            return None, None, None

        status, device_name, udid = hdisk.discover_hdisk(self.adapter,
                                                         vios_w.uuid, itls)

        if hdisk.good_discovery(status, device_name):
            LOG.info('Discovered %(hdisk)s on vios %(vios)s for volume '
                     '%(volume_id)s. Status code: %(status)s.',
                     {'hdisk': device_name, 'vios': vios_w.name,
                      'volume_id': volume_id, 'status': status},
                     instance=self.instance)
        elif status == hdisk.LUAStatus.DEVICE_IN_USE:
            LOG.warning('Discovered device %(dev)s for volume %(volume)s '
                        'on %(vios)s is in use. Error code: %(status)s.',
                        {'dev': device_name, 'volume': volume_id,
                         'vios': vios_w.name, 'status': status},
                        instance=self.instance)

        return status, device_name, udid

    def _connect_volume_to_vio(self, vios_w, slot_mgr):
        """Attempts to connect a volume to a given VIO.

        :param vios_w: The Virtual I/O Server wrapper to connect to.
        :param slot_mgr: A NovaSlotManager.  Used to delete the client slots
                         used when a volume is detached from the VM

        :return: True if the volume was connected.  False if the volume was
                 not (could be the Virtual I/O Server does not have
                 connectivity to the hdisk).
        """
        status, device_name, udid = self._discover_volume_on_vios(
            vios_w, self.volume_id)

        # Get the slot and LUA to assign.
        slot, lua = slot_mgr.build_map.get_vscsi_slot(vios_w, udid)

        if slot_mgr.is_rebuild and not slot:
            LOG.debug('Detected a device with UDID %(udid)s on VIOS '
                      '%(vios)s on the rebuild that did not exist on the '
                      'source. Ignoring.', {'udid': udid, 'vios': vios_w.uuid},
                      instance=self.instance)
            return False

        if hdisk.good_discovery(status, device_name):
            volume_id = self.connection_info["data"]["volume_id"]
            # Found a hdisk on this Virtual I/O Server.  Add the action to
            # map it to the VM when the stg_ftsk is executed.
            with lockutils.lock(hash(self)):
                self._add_append_mapping(vios_w.uuid, device_name,
                                         lpar_slot_num=slot, lua=lua,
                                         tag=volume_id)

            # Save the UDID for the disk in the connection info.  It is
            # used for the detach.
            self._set_udid(udid)
            LOG.debug('Added deferred task to attach device %(device_name)s '
                      'to vios %(vios_name)s.',
                      {'device_name': device_name, 'vios_name': vios_w.name},
                      instance=self.instance)

            # Valid attachment
            return True

        return False

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
                      dict(vol=self.volume_id, uuid=vios_w.uuid),
                      instance=self.instance)
            device_name = None
            udid = self._get_udid()
            try:
                if udid:
                    # This will only work if vios_w has the Storage XAG.
                    device_name = vios_w.hdisk_from_uuid(udid)

                if not udid or not device_name:
                    # We lost our bdm data. We'll need to discover it.
                    status, device_name, udid = self._discover_volume_on_vios(
                        vios_w, self.volume_id)

                    # Check if the hdisk is in a bad state in the I/O Server.
                    # Subsequent scrub code on future deploys will clean it up.
                    if not hdisk.good_discovery(status, device_name):
                        LOG.warning(
                            "Disconnect Volume: The backing hdisk for volume "
                            "%(volume_id)s on Virtual I/O Server %(vios)s is "
                            "not in a valid state.  This may be the result of "
                            "an evacuate.",
                            {'volume_id': self.volume_id, 'vios': vios_w.name},
                            instance=self.instance)
                        return False

            except Exception:
                LOG.exception(
                    "Disconnect Volume: Failed to find disk on Virtual I/O "
                    "Server %(vios_name)s for volume %(volume_id)s. Volume "
                    "UDID: %(volume_uid)s.",
                    {'vios_name': vios_w.name, 'volume_id': self.volume_id,
                     'volume_uid': udid}, instance=self.instance)
                return False

            # We have found the device name
            LOG.info("Disconnect Volume: Discovered the device %(hdisk)s "
                     "on Virtual I/O Server %(vios_name)s for volume "
                     "%(volume_id)s.  Volume UDID: %(volume_uid)s.",
                     {'volume_uid': udid, 'volume_id': self.volume_id,
                      'vios_name': vios_w.name, 'hdisk': device_name},
                     instance=self.instance)

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
                LOG.warning("Disconnect Volume: Failed to disconnect the "
                            "volume %(volume_id)s on ANY of the Virtual "
                            "I/O Servers.", {'volume_id': self.volume_id},
                            instance=self.instance)

        except Exception as e:
            LOG.exception('PowerVM error detaching volume from virtual '
                          'machine.', instance=self.instance)
            ex_args = {'volume_id': self.volume_id, 'reason': six.text_type(e),
                       'instance_name': self.instance.name}
            raise p_exc.VolumeDetachFailed(**ex_args)

    @lockutils.synchronized('vscsi_wwpns')
    def wwpns(self):
        """Builds the WWPNs of the adapters that will connect the ports.

        :return: The list of WWPNs that need to be included in the zone set.
        """
        # Use a global variable so this is pulled once when the process starts.
        global _vscsi_pfc_wwpns
        if _vscsi_pfc_wwpns is None:
            _vscsi_pfc_wwpns = pvm_tpar.get_physical_wwpns(self.adapter)
        return _vscsi_pfc_wwpns

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
