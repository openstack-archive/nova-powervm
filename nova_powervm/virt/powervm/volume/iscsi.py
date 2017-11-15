# Copyright 2015, 2017 IBM Corp.
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

from nova import exception as nova_exc
from nova_powervm import conf as cfg
from nova_powervm.virt.powervm import exception as p_exc
from nova_powervm.virt.powervm import vm
from nova_powervm.virt.powervm.volume import driver as v_driver
from nova_powervm.virt.powervm.volume import volume as volume
from pypowervm import const as pvm_const
from pypowervm.tasks import hdisk
from pypowervm.utils import transaction as tx
from pypowervm.wrappers import virtual_io_server as pvm_vios

from taskflow import task

import six


LOG = logging.getLogger(__name__)
CONF = cfg.CONF
DEVNAME_KEY = 'target_devname'


class IscsiVolumeAdapter(volume.VscsiVolumeAdapter,
                         v_driver.PowerVMVolumeAdapter):
    """The iSCSI implementation of the Volume Adapter.

    This driver will connect a volume to a VM. First using iSCSI to connect the
    volume to the I/O Host (NovaLink partition). Then using the PowerVM vSCSI
    technology to host it to the VM itself.
    """
    def __init__(self, adapter, host_uuid, instance, connection_info,
                 stg_ftsk=None):
        super(IscsiVolumeAdapter, self).__init__(
            adapter, host_uuid, instance, connection_info, stg_ftsk=stg_ftsk)

    @classmethod
    def vol_type(cls):
        """The type of volume supported by this type."""
        return 'iscsi'

    @classmethod
    def min_xags(cls):
        """List of pypowervm XAGs needed to support this adapter."""
        return [pvm_const.XAG.VIO_SMAP]

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
        raise NotImplementedError()

    def _connect_volume_to_vio(self, vios_w, slot_mgr):
        """Attempts to connect a volume to a given VIO.

        :param vios_w: The Virtual I/O Server wrapper to connect to.
        :param slot_mgr: A NovaSlotManager.  Used to delete the client slots
                         used when a volume is detached from the VM

        :return: True if the volume was connected.  False if the volume was
                 not (could be the Virtual I/O Server does not have
                 connectivity to the hdisk).
        """
        transport_type = self.connection_info["driver_volume_type"]
        host_ip = self.connection_info["data"]["target_portal"]
        iqn = self.connection_info["data"]["target_iqn"]
        password = self.connection_info["data"]["auth_password"]
        user = self.connection_info["data"]["auth_username"]
        target_name = "ISCSI-" + iqn.split(":")[1]
        device_name, udid = hdisk.discover_iscsi(
            self.adapter, host_ip, user, password, iqn, vios_w.uuid,
            transport_type=transport_type)
        slot, lua = slot_mgr.build_map.get_vscsi_slot(vios_w, device_name)
        if device_name is not None and udid is not None:
            device_name = '/dev/' + device_name
            # Found a hdisk on this Virtual I/O Server.  Add the action to
            # map it to the VM when the stg_ftsk is executed.
            with lockutils.lock(hash(self)):
                self._add_append_mapping(
                    vios_w.uuid, device_name, lpar_slot_num=slot, lua=lua,
                    target_name=target_name, udid=udid)

            # Save the devname for the disk in the connection info.  It is
            # used for the detach.
            self._set_devname(device_name)
            self._set_udid(udid)

            LOG.debug('Device attached: %s', device_name,
                      instance=self.instance)

            # Valid attachment
            return True

        return False

    def extend_volume(self):
        """Rescan virtual disk so client VM can see extended size."""
        udid = self._get_udid()
        if udid is None:
            raise nova_exc.InvalidBDM()
        self._extend_volume(udid)

    def _disconnect_volume(self, slot_mgr):
        """Disconnect the volume.

        This is the actual method to implement within the subclass.  Some
        transaction maintenance is done by the parent class.

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
            try:
                device_name = self._get_devname()

                if not device_name:
                    # We lost our bdm data.

                    # If we have no device name, at this point
                    # we should not continue.  Subsequent scrub code on future
                    # deploys will clean this up.
                    LOG.warning(
                        "Disconnect Volume: The backing hdisk for volume "
                        "%(volume_id)s on Virtual I/O Server %(vios)s is "
                        "not in a valid state.  No disconnect "
                        "actions to be taken as volume is not healthy.",
                        {'volume_id': self.volume_id, 'vios': vios_w.name},
                        instance=self.instance)
                    return False

            except Exception:
                LOG.exception(
                    "Disconnect Volume: Failed to find device on Virtual I/O "
                    "Server %(vios_name)s for volume %(volume_id)s.",
                    {'vios_name': vios_w.name, 'volume_id': self.volume_id},
                    instance=self.instance)
                return False

            # We have found the device name
            LOG.info("Disconnect Volume: Discovered the device %(hdisk)s "
                     "on Virtual I/O Server %(vios_name)s for volume "
                     "%(volume_id)s.",
                     {'volume_id': self.volume_id,
                      'vios_name': vios_w.name, 'hdisk': device_name},
                     instance=self.instance)

            # Add the action to remove the mapping when the stg_ftsk is run.
            partition_id = vm.get_vm_id(self.adapter, self.vm_uuid)

            with lockutils.lock(hash(self)):
                self._add_remove_mapping(partition_id, vios_w.uuid,
                                         device_name, slot_mgr)
                target_iqn = self.connection_info["data"]["target_iqn"]

                def logout():
                    hdisk.remove_iscsi(self.adapter, target_iqn, vios_w.uuid)
                self.stg_ftsk.add_post_execute(task.FunctorTask(
                    logout, name='remove_iSCSI_%s' % target_iqn))
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
                LOG.warning(
                    "Disconnect Volume: Failed to disconnect the volume "
                    "%(volume_id)s on ANY of the Virtual I/O Servers.",
                    {'volume_id': self.volume_id}, instance=self.instance)

        except Exception as e:
            LOG.exception('PowerVM error detaching volume from virtual '
                          'machine.', instance=self.instance)
            ex_args = {'volume_id': self.volume_id, 'reason': six.text_type(e),
                       'instance_name': self.instance.name}
            raise p_exc.VolumeDetachFailed(**ex_args)
