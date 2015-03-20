# Copyright 2013 OpenStack Foundation
# Copyright 2015 IBM Corp.
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


from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import units

from nova import exception as nova_exc
from nova.i18n import _LI, _LE
from pypowervm import exceptions as pvm_exc
from pypowervm.tasks import scsi_mapper as tsk_map
from pypowervm.tasks import storage as tsk_stg
from pypowervm.wrappers import storage as pvm_stg
from pypowervm.wrappers import virtual_io_server as pvm_vios

import nova_powervm.virt.powervm.disk as disk
from nova_powervm.virt.powervm.disk import driver as disk_dvr
from nova_powervm.virt.powervm import vios
from nova_powervm.virt.powervm import vm

localdisk_opts = [
    cfg.StrOpt('volume_group_name',
               default='',
               help='Volume Group to use for block device operations.')
]


LOG = logging.getLogger(__name__)
CONF = cfg.CONF
CONF.register_opts(localdisk_opts)


class VGNotFound(disk.AbstractDiskException):
    msg_fmt = _LE('Unable to locate the volume group \'%(vg_name)s\' '
                  'for this operation.')


class LocalStorage(disk_dvr.DiskAdapter):
    def __init__(self, connection):
        super(LocalStorage, self).__init__(connection)
        self.adapter = connection['adapter']
        self.host_uuid = connection['host_uuid']

        # TODO(IBM) Query the VIOSes to find the one containing the VG.
        # This is a temporary method to find the VIOS.
        vios_map = vios.get_vios_name_map(self.adapter, self.host_uuid)
        self.vios_name, self.vios_uuid = vios_map.items()[0]

        self.vg_name = CONF.volume_group_name
        self.vg_uuid = self._get_vg_uuid(self.adapter, self.vios_uuid,
                                         CONF.volume_group_name)
        LOG.info(_LI('Local Storage driver initialized: '
                     'volume group: \'%s\'') % self.vg_name)

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
        """Removes the disks specified by the mappings.

        :param context: nova context for operation
        :param instance: instance to delete the disk for.
        :param storage_elems: A list of the storage elements that are to be
                              deleted.  Derived from the return value from
                              disconnect_image_disk.
        """
        # All of local disk is done against the volume group.  So reload
        # that (to get new etag) and then do an update against it.
        vg_wrap = self._get_vg_wrap()

        # We know that the mappings are VSCSIMappings.  Remove the storage that
        # resides in the scsi map from the volume group.
        existing_vds = vg_wrap.virtual_disks
        for removal in storage_elems:
            LOG.info(_LI('Deleting disk: %s') % removal.name,
                     instance=instance)

            # Can't just call direct on remove, because attribs are off.
            # May want to evaluate change in pypowervm for this.
            match = None
            for existing_vd in existing_vds:
                if existing_vd.name == removal.name:
                    match = existing_vd
                    break

            if match is not None:
                existing_vds.remove(match)

        # Now update the volume group to remove the storage.
        vg_wrap.update(self.adapter)

    def disconnect_image_disk(self, context, instance, lpar_uuid,
                              disk_type=None):
        """Disconnects the storage adapters from the image disk.

        :param context: nova context for operation
        :param instance: instance to disconnect the image for.
        :param lpar_uuid: The UUID for the pypowervm LPAR element.
        :param disk_type: The list of disk types to remove or None which means
            to remove all disks from the VM.
        :return: A list of all the backing storage elements that were
                 disconnected from the I/O Server and VM.
        """
        partition_id = vm.get_vm_id(self.adapter, lpar_uuid)
        return tsk_map.remove_vdisk_mapping(self.adapter, self.vios_uuid,
                                            partition_id,
                                            disk_prefixes=disk_type)

    def create_disk_from_image(self, context, instance, image, disk_size,
                               image_type=disk_dvr.BOOT_DISK):
        """Creates a disk and copies the specified image to it.

        :param context: nova context used to retrieve image from glance
        :param instance: instance to create the disk for.
        :param image_id: image_id reference used to locate image in glance
        :param disk_size: The size of the disk to create in GB.  If smaller
                          than the image, it will be ignored (as the disk
                          must be at least as big as the image).  Must be an
                          int.
        :param image_type: the image type. See disk constants above.
        :returns: The backing pypowervm storage object that was created.
        """
        LOG.info(_LI('Create disk.'))

        # Transfer the image
        stream = self._get_image_upload(context, image)
        vol_name = self._get_disk_name(image_type, instance)

        # Disk size to API is in bytes.  Input from method is in Gb
        disk_bytes = disk_size * units.Gi
        if disk_bytes < image['size']:
            # If the image is bigger than the disk, then change the disk size
            # to match the image (so that the image fits).
            disk_bytes = image['size']

        # This method will create a new disk at our specified size.  It will
        # then put the image in the disk.  If the disk is bigger, user can
        # resize the disk, create a new partition, etc...
        # If the image is bigger than disk, API should make the disk big
        # enough to support the image (up to 1 Gb boundary).
        vdisk, f_uuid = tsk_stg.upload_new_vdisk(
            self.adapter, self.vios_uuid, self.vg_uuid, stream, vol_name,
            image['size'], d_size=disk_bytes)

        return vdisk

    def connect_disk(self, context, instance, disk_info, lpar_uuid):
        """Connects the disk image to the Virtual Machine.

        :param context: nova context for the transaction.
        :param instance: nova instance to connect the disk to.
        :param disk_info: The pypowervm storage element returned from
                          create_disk_from_image.  Ex. VOptMedia, VDisk, LU,
                          or PV.
        :param: lpar_uuid: The pypowervm UUID that corresponds to the VM.
        """
        # Create the mapping structure
        scsi_map = pvm_vios.VSCSIMapping.bld_to_vdisk(
            self.adapter, self.host_uuid, lpar_uuid, disk_info.name)

        # Add the mapping to the VIOS
        tsk_map.add_vscsi_mapping(self.adapter, self.vios_uuid, scsi_map)

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
                LOG.error(_LE('Disk %s not found during resize.') % vol_name,
                          instance=instance)
                raise nova_exc.DiskNotFound(
                    location=self.vg_name + '/' + vol_name)

            # Set the new size
            disk_found.capacity = size

            # Post it to the VIOS
            vg_wrap.update(self.adapter)

        # Get the disk name based on the instance and type
        vol_name = self._get_disk_name(disk_info['type'], instance)
        LOG.info(_LI('Extending disk: %s') % vol_name)
        try:
            _extend()
        except pvm_exc.Error:
            # TODO(IBM): Handle etag mismatch and retry
            LOG.exception()
            raise

    @staticmethod
    def _get_disk_name(disk_type, instance):
        return disk_type[:6] + '_' + instance.uuid[:8]

    @staticmethod
    def _get_vg_uuid(adapter, vios_uuid, name):
        try:
            resp = adapter.read(pvm_vios.VIOS.schema_type, root_id=vios_uuid,
                                child_type=pvm_stg.VG.schema_type)
        except Exception as e:
            LOG.exception(e)
            raise e

        # Search the feed for the volume group
        vol_grps = pvm_stg.VG.wrap(resp)
        for vol_grp in vol_grps:
            LOG.info(_LI('Volume group: %s') % vol_grp.name)
            if name == vol_grp.name:
                return vol_grp.uuid

        raise VGNotFound(vg_name=name)

    def _get_vg(self):
        vg_rsp = self.adapter.read(
            pvm_vios.VIOS.schema_type, root_id=self.vios_uuid,
            child_type=pvm_stg.VG.schema_type, child_id=self.vg_uuid)
        return vg_rsp

    def _get_vg_wrap(self):
        return pvm_stg.VG.wrap(self._get_vg())
