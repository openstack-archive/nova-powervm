# Copyright 2013 OpenStack Foundation
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

from oslo_log import log as logging

from nova import exception as nova_exc
from pypowervm import const as pvm_const
from pypowervm import exceptions as pvm_exc
from pypowervm.tasks import scsi_mapper as tsk_map
from pypowervm.tasks import storage as tsk_stg
from pypowervm.wrappers import managed_system as pvm_ms
from pypowervm.wrappers import storage as pvm_stg
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm import conf as cfg
from nova_powervm.virt.powervm.disk import driver as disk_dvr
from nova_powervm.virt.powervm import exception as npvmex
from nova_powervm.virt.powervm.i18n import _LE
from nova_powervm.virt.powervm.i18n import _LI
from nova_powervm.virt.powervm import vios
from nova_powervm.virt.powervm import vm


LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class LocalStorage(disk_dvr.DiskAdapter):
    def __init__(self, connection):
        super(LocalStorage, self).__init__(connection)

        # Query to get the Volume Group UUID
        self.vg_name = CONF.powervm.volume_group_name
        self._vios_uuid, self.vg_uuid = self._get_vg_uuid(self.vg_name)
        LOG.info(_LI("Local Storage driver initialized: volume group: '%s'"),
                 self.vg_name)

    @property
    def vios_uuids(self):
        """List the UUIDs of the Virtual I/O Servers hosting the storage.

        For localdisk, there's only one.
        """
        return [self._vios_uuid]

    def disk_match_func(self, disk_type, instance):
        """Return a matching function to locate the disk for an instance.

        :param disk_type: One of the DiskType enum values.
        :param instance: The instance whose disk is to be found.
        :return: Callable suitable for the match_func parameter of the
                 pypowervm.tasks.scsi_mapper.find_maps method, with the
                 following specification:
            def match_func(storage_elem)
                param storage_elem: A backing storage element wrapper (VOpt,
                                    VDisk, PV, or LU) to be analyzed.
                return: True if the storage_elem's mapping should be included;
                        False otherwise.
        """
        disk_name = self._get_disk_name(disk_type, instance, short=True)
        return tsk_map.gen_match_func(pvm_stg.VDisk, names=[disk_name])

    @property
    def capacity(self):
        """Capacity of the storage in gigabytes."""
        vg_wrap = self._get_vg_wrap()

        return float(vg_wrap.capacity)

    @property
    def capacity_used(self):
        """Capacity of the storage in gigabytes that is used."""
        vg_wrap = self._get_vg_wrap()

        # Subtract available from capacity
        return float(vg_wrap.capacity) - float(vg_wrap.available_size)

    def delete_disks(self, context, instance, storage_elems):
        """Removes the specified disks.

        :param context: nova context for operation
        :param instance: instance to delete the disk for.
        :param storage_elems: A list of the storage elements that are to be
                              deleted.  Derived from the return value from
                              disconnect_image_disk.
        """
        # All of local disk is done against the volume group.  So reload
        # that (to get new etag) and then update against it.
        tsk_stg.rm_vg_storage(self._get_vg_wrap(), vdisks=storage_elems)

    def disconnect_image_disk(self, context, instance, stg_ftsk=None,
                              disk_type=None):
        """Disconnects the storage adapters from the image disk.

        :param context: nova context for operation
        :param instance: instance to disconnect the image for.
        :param stg_ftsk: (Optional) The pypowervm transaction FeedTask for the
                         I/O Operations.  If provided, the Virtual I/O Server
                         mapping updates will be added to the FeedTask.  This
                         defers the updates to some later point in time.  If
                         the FeedTask is not provided, the updates will be run
                         immediately when this method is executed.
        :param disk_type: The list of disk types to remove or None which means
                          to remove all disks from the VM.
        :return: A list of all the backing storage elements that were
                 disconnected from the I/O Server and VM.
        """
        lpar_uuid = vm.get_pvm_uuid(instance)

        # Ensure we have a transaction manager.
        if stg_ftsk is None:
            stg_ftsk = vios.build_tx_feed_task(
                self.adapter, self.host_uuid, name='localdisk',
                xag=[pvm_const.XAG.VIO_SMAP])

        # Build the match function
        match_func = tsk_map.gen_match_func(pvm_stg.VDisk, prefixes=disk_type)

        # Make sure the remove function will run within the transaction manager
        def rm_func(vios_w):
            LOG.info(_LI("Disconnecting instance %(inst)s from storage disks.")
                     % {'inst': instance.name})
            return tsk_map.remove_maps(vios_w, lpar_uuid,
                                       match_func=match_func)

        stg_ftsk.wrapper_tasks[self._vios_uuid].add_functor_subtask(rm_func)

        # Find the disk directly.
        vios_w = stg_ftsk.wrapper_tasks[self._vios_uuid].wrapper
        mappings = tsk_map.find_maps(vios_w.scsi_mappings,
                                     client_lpar_id=lpar_uuid,
                                     match_func=match_func)

        # Run the transaction manager if built locally.  Must be done after
        # the find to make sure the mappings were found previously.
        if stg_ftsk.name == 'localdisk':
            stg_ftsk.execute()

        return [x.backing_storage for x in mappings]

    def disconnect_disk_from_mgmt(self, vios_uuid, disk_name):
        """Disconnect a disk from the management partition.

        :param vios_uuid: The UUID of the Virtual I/O Server serving the
                          mapping.
        :param disk_name: The name of the disk to unmap.
        """
        tsk_map.remove_vdisk_mapping(self.adapter, vios_uuid, self.mp_uuid,
                                     disk_names=[disk_name])
        LOG.info(_LI(
            "Unmapped boot disk %(disk_name)s from the management partition "
            "from Virtual I/O Server %(vios_name)s."), {
                'disk_name': disk_name, 'mp_uuid': self.mp_uuid,
                'vios_name': vios_uuid})

    def create_disk_from_image(self, context, instance, image_meta, disk_size,
                               image_type=disk_dvr.DiskType.BOOT):
        """Creates a disk and copies the specified image to it.

        :param context: nova context used to retrieve image from glance
        :param instance: instance to create the disk for.
        :param nova.objects.ImageMeta image_meta:
            The metadata of the image of the instance.
        :param disk_size: The size of the disk to create in GB.  If smaller
                          than the image, it will be ignored (as the disk
                          must be at least as big as the image).  Must be an
                          int.
        :param image_type: the image type. See disk constants above.
        :return: The backing pypowervm storage object that was created.
        """
        LOG.info(_LI('Create disk.'))

        # Transfer the image
        stream = self._get_image_upload(context, image_meta)
        vol_name = self._get_disk_name(image_type, instance, short=True)

        # Disk size to API is in bytes.  Input from method is in Gb
        disk_bytes = self._disk_gb_to_bytes(disk_size, floor=image_meta.size)

        # This method will create a new disk at our specified size.  It will
        # then put the image in the disk.  If the disk is bigger, user can
        # resize the disk, create a new partition, etc...
        # If the image is bigger than disk, API should make the disk big
        # enough to support the image (up to 1 Gb boundary).
        vdisk, f_wrap = tsk_stg.upload_new_vdisk(
            self.adapter, self._vios_uuid, self.vg_uuid, stream, vol_name,
            image_meta.size, d_size=disk_bytes)

        return vdisk

    def connect_disk(self, context, instance, disk_info, stg_ftsk=None):
        """Connects the disk image to the Virtual Machine.

        :param context: nova context for the transaction.
        :param instance: nova instance to connect the disk to.
        :param disk_info: The pypowervm storage element returned from
                          create_disk_from_image.  Ex. VOptMedia, VDisk, LU,
                          or PV.
        :param stg_ftsk: (Optional) The pypowervm transaction FeedTask for the
                         I/O Operations.  If provided, the Virtual I/O Server
                         mapping updates will be added to the FeedTask.  This
                         defers the updates to some later point in time.  If
                         the FeedTask is not provided, the updates will be run
                         immediately when this method is executed.
        """
        lpar_uuid = vm.get_pvm_uuid(instance)

        # Ensure we have a transaction manager.
        if stg_ftsk is None:
            stg_ftsk = vios.build_tx_feed_task(
                self.adapter, self.host_uuid, name='localdisk',
                xag=[pvm_const.XAG.VIO_SMAP])

        def add_func(vios_w):
            LOG.info(_LI("Adding logical volume disk connection between VM "
                         "%(vm)s and VIOS %(vios)s."),
                     {'vm': instance.name, 'vios': vios_w.name})
            mapping = tsk_map.build_vscsi_mapping(
                self.host_uuid, vios_w, lpar_uuid, disk_info)
            return tsk_map.add_map(vios_w, mapping)

        stg_ftsk.wrapper_tasks[self._vios_uuid].add_functor_subtask(add_func)

        # Run the transaction manager if built locally.
        if stg_ftsk.name == 'localdisk':
            stg_ftsk.execute()

    def extend_disk(self, context, instance, disk_info, size):
        """Extends the disk.

        :param context: nova context for operation.
        :param instance: instance to extend the disk for.
        :param disk_info: dictionary with disk info.
        :param size: the new size in gb.
        """
        def _extend():
            # Get the volume group
            vg_wrap = self._get_vg_wrap()
            # Find the disk by name
            vdisks = vg_wrap.virtual_disks
            disk_found = None
            for vdisk in vdisks:
                if vdisk.name == vol_name:
                    disk_found = vdisk
                    break

            if not disk_found:
                LOG.error(_LE('Disk %s not found during resize.'), vol_name,
                          instance=instance)
                raise nova_exc.DiskNotFound(
                    location=self.vg_name + '/' + vol_name)

            # Set the new size
            disk_found.capacity = size

            # Post it to the VIOS
            vg_wrap.update()

        # Get the disk name based on the instance and type
        vol_name = self._get_disk_name(disk_info['type'], instance, short=True)
        LOG.info(_LI('Extending disk: %s'), vol_name)
        try:
            _extend()
        except pvm_exc.Error:
            # TODO(IBM): Handle etag mismatch and retry
            LOG.exception()
            raise

    def _get_vg_uuid(self, name):
        """Returns the VIOS and VG UUIDs for the volume group.

        Will iterate over the VIOSes to find the VG with the name.

        :param name: The name of the volume group.
        :return vios_uuid: The Virtual I/O Server pypowervm UUID.
        :return vg_uuid: The Volume Group pypowervm UUID.
        """
        if CONF.powervm.volume_group_vios_name:
            # Search for the VIOS if the admin specified it.
            vios_wraps = pvm_vios.VIOS.search(
                self.adapter, name=CONF.powervm.volume_group_vios_name)
        else:
            vios_resp = self.adapter.read(pvm_ms.System.schema_type,
                                          root_id=self.host_uuid,
                                          child_type=pvm_vios.VIOS.schema_type)
            vios_wraps = pvm_vios.VIOS.wrap(vios_resp)

        # Loop through each vios to find the one with the appropriate name.
        for vios_wrap in vios_wraps:
            # Search the feed for the volume group
            resp = self.adapter.read(pvm_vios.VIOS.schema_type,
                                     root_id=vios_wrap.uuid,
                                     child_type=pvm_stg.VG.schema_type)
            vol_grps = pvm_stg.VG.wrap(resp)
            for vol_grp in vol_grps:
                LOG.debug('Volume group: %s', vol_grp.name)
                if name == vol_grp.name:
                    return vios_wrap.uuid, vol_grp.uuid

        raise npvmex.VGNotFound(vg_name=name)

    def _get_vg(self):
        vg_rsp = self.adapter.read(
            pvm_vios.VIOS.schema_type, root_id=self._vios_uuid,
            child_type=pvm_stg.VG.schema_type, child_id=self.vg_uuid)
        return vg_rsp

    def _get_vg_wrap(self):
        return pvm_stg.VG.wrap(self._get_vg())
