# Copyright 2017 IBM Corp.
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

import abc
import six
from taskflow import task

from nova_powervm import conf as cfg
from nova_powervm.virt.powervm import exception as p_exc
from nova_powervm.virt.powervm import vm
from nova_powervm.virt.powervm.volume import driver as v_driver
from oslo_log import log as logging
from pypowervm import const as pvm_const
from pypowervm.tasks import client_storage as pvm_c_stor
from pypowervm.tasks import hdisk
from pypowervm.tasks import partition
from pypowervm.tasks import scsi_mapper as tsk_map
from pypowervm.wrappers import storage as pvm_stg

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


@six.add_metaclass(abc.ABCMeta)
class RBDVolumeAdapter(v_driver.PowerVMVolumeAdapter):
    """Base class for connecting ceph based Cinder Volumes to PowerVM VMs."""

    def __init__(self, adapter, host_uuid, instance, connection_info,
                 stg_ftsk=None):
        super(RBDVolumeAdapter, self).__init__(
            adapter, host_uuid, instance, connection_info, stg_ftsk=stg_ftsk)
        self._nl_vios_ids = None

    @classmethod
    def min_xags(cls):
        return [pvm_const.XAG.VIO_SMAP]

    @classmethod
    def vol_type(cls):
        """The type of volume supported by this type."""
        return 'rbd'

    @property
    def vios_uuids(self):
        """List the UUIDs of the Virtual I/O Servers hosting the storage."""
        # Get the hosting UUID
        if self._nl_vios_ids is None:
            nl_vios_wrap = partition.get_mgmt_partition(self.adapter)
            self._nl_vios_ids = [nl_vios_wrap.uuid]
        return self._nl_vios_ids

    def pre_live_migration_on_destination(self, mig_data):
        """Perform pre live migration steps for the volume on the target host.

        This method performs any pre live migration that is needed.

        This method will be called after the pre_live_migration_on_source
        method.  The data from the pre_live call will be passed in via the
        mig_data.  This method should put its output into the dest_mig_data.

        :param mig_data: Dict of migration data for the destination server.
                         If the volume connector needs to provide
                         information to the live_migration command, it
                         should be added to this dictionary.
        """
        for vios_uuid in self.vios_uuids:
            exists, name = self.is_volume_on_vios(vios_uuid)
            if exists and name is not None:
                return
        name = self.connection_info["data"]["name"]
        LOG.warning("RBD %s not found", name, instance=self.instance)
        raise p_exc.VolumePreMigrationFailed(
            volume_id=self.volume_id, instance_name=self.instance.name)

    def _connect_volume(self, slot_mgr):
        name = self.connection_info["data"]["name"]
        volid = self.connection_info["data"]["volume_id"]
        user = CONF.powervm.rbd_user
        rbd = pvm_stg.RBD.bld_ref(self.adapter, name, tag=volid, user=user)

        def add_func(vios_w):
            # If the vios doesn't match, just return
            if vios_w.uuid not in self.vios_uuids:
                return None

            LOG.info("Adding rbd disk connection to VIOS %(vios)s.",
                     {'vios': vios_w.name}, instance=self.instance)
            slot, lua = slot_mgr.build_map.get_vscsi_slot(vios_w, name)
            if slot_mgr.is_rebuild and not slot:
                LOG.debug('Detected a device with path %(path)s on VIOS '
                          '%(vios)s on the rebuild that did not exist on the '
                          'source. Ignoring.',
                          {'path': name, 'vios': vios_w.uuid},
                          instance=self.instance)
                return None

            mapping = tsk_map.build_vscsi_mapping(
                self.host_uuid, vios_w, self.vm_uuid, rbd, lpar_slot_num=slot,
                lua=lua)
            return tsk_map.add_map(vios_w, mapping)

        self.stg_ftsk.add_functor_subtask(add_func)

        # Run after all the deferred tasks the query to save the slots in the
        # slot map.
        def set_slot_info():
            vios_wraps = self.stg_ftsk.feed
            partition_id = vm.get_vm_id(self.adapter, self.vm_uuid)
            for vios_w in vios_wraps:
                scsi_map = pvm_c_stor.udid_to_scsi_mapping(
                    vios_w, name, partition_id)
                if not scsi_map:
                    continue
                slot_mgr.register_vscsi_mapping(scsi_map)

        self.stg_ftsk.add_post_execute(task.FunctorTask(
            set_slot_info, name='rbd_slot_%s' % name))

    def _disconnect_volume(self, slot_mgr):
        # Build the match function
        name = self.connection_info["data"]["name"]
        match_func = tsk_map.gen_match_func(pvm_stg.VDisk,
                                            names=[name])

        # Make sure the remove function will run within the transaction manager
        def rm_func(vios_w):
            # If the vios doesn't match, just return
            if vios_w.uuid not in self.vios_uuids:
                return None

            LOG.info("Disconnecting instance %(inst)s from storage "
                     "disks.", {'inst': self.instance.name},
                     instance=self.instance)
            removed_maps = tsk_map.remove_maps(vios_w, self.vm_uuid,
                                               match_func=match_func)
            for rm_map in removed_maps:
                slot_mgr.drop_vscsi_mapping(rm_map)
            return removed_maps

        self.stg_ftsk.add_functor_subtask(rm_func)
        # Find the disk directly.
        vios_w = self.stg_ftsk.wrapper_tasks[self.vios_uuids[0]].wrapper
        mappings = tsk_map.find_maps(vios_w.scsi_mappings,
                                     client_lpar_id=self.vm_uuid,
                                     match_func=match_func)

        return [x.backing_storage for x in mappings]

    def is_volume_on_vios(self, vio):
        """Returns whether or not the volume file is on a VIOS.

        This method is used during live-migration and rebuild to
        check if the volume is available on the target host.

        :param vio: The Virtual I/O Server wrapper or UDID string of the VIOS.
        :return: True if the file is on the VIOS.  False
                 otherwise.
        :return: The file path.
        """
        try:
            vio_uuid = vio.uuid
        except AttributeError:
            vio_uuid = vio
        if vio_uuid not in self.vios_uuids:
            return False, None
        name = self.connection_info["data"]["name"]
        exists = hdisk.rbd_exists(self.adapter, vio_uuid, name)
        return exists, name if exists else None
