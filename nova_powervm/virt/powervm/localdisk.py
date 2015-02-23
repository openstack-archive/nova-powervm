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

import abc

from oslo.config import cfg
from oslo.utils import units
from oslo_log import log as logging
import six

from nova import exception as nova_exc
from nova import image
from nova.i18n import _LI, _LE
from pypowervm import exceptions as pvm_exc
from pypowervm.jobs import upload_lv
from pypowervm.wrappers import constants as pvm_consts
from pypowervm.wrappers import storage as pvm_st
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.virt.powervm import blockdev
from nova_powervm.virt.powervm import vios

localdisk_opts = [
    cfg.StrOpt('volume_group_name',
               default='',
               help='Volume Group to use for block device operations.')
]


LOG = logging.getLogger(__name__)
CONF = cfg.CONF
CONF.register_opts(localdisk_opts)


@six.add_metaclass(abc.ABCMeta)
class AbstractLocalStorageException(Exception):
    def __init__(self, **kwds):
        msg = self.msg_fmt % kwds
        super(AbstractLocalStorageException, self).__init__(msg)


class VGNotFound(AbstractLocalStorageException):
    msg_fmt = _LE('Unable to locate the volume group \'%(vg_name)s\''
                  ' for this operation.')


class IterableToFileAdapter(object):
    """A degenerate file-like so that an iterable could be read like a file.

    As Glance client returns an iterable, but PowerVM requires a file,
    this is the adapter between the two.

    Taken from xenapi/image/apis.py
    """

    def __init__(self, iterable):
        self.iterator = iterable.__iter__()
        self.remaining_data = ''

    def read(self, size):
        chunk = self.remaining_data
        try:
            while not chunk:
                chunk = self.iterator.next()
        except StopIteration:
            return ''
        return_value = chunk[0:size]
        self.remaining_data = chunk[size:]
        return return_value


class LocalStorage(blockdev.StorageAdapter):
    def __init__(self, connection):
        super(LocalStorage, self).__init__(connection)
        self.adapter = connection['adapter']
        self.host_uuid = connection['host_uuid']
        self.vios_name = connection['vios_name']
        self.vios_uuid = connection['vios_uuid']
        self.vg_name = CONF.volume_group_name
        self.vg_uuid = self._get_vg_uuid(self.adapter, self.vios_uuid,
                                         CONF.volume_group_name)
        self.image_api = image.API()
        LOG.info(_LI('Local Storage driver initialized: '
                     'volume group: \'%s\'') % self.vg_name)

    @property
    def capacity(self):
        """Capacity of the storage in gigabytes

        """
        vg_wrap = self._get_vg_wrap()

        return float(vg_wrap.capacity)

    @property
    def capacity_used(self):
        """Capacity of the storage in gigabytes that is used

        """
        vg_wrap = self._get_vg_wrap()

        # Subtract available from capacity
        return (float(vg_wrap.capacity) - float(vg_wrap.available_size))

    def delete_volumes(self, context, instance, mappings):
        LOG.info('Deleting local volumes for instance %s'
                 % instance.name)
        # All of local disk is done against the volume group.  So reload
        # that (to get new etag) and then do an update against it.
        vg_wrap = self._get_vg_wrap()

        # The mappings are from the VIOS and they don't 100% line up with
        # the elements from the VG.  Need to find the matching ones based on
        # the UDID of the disk.
        # TODO(thorst) I think this should be handled down in pypowervm.  Have
        # the wrapper strip out self references.
        removals = []
        for scsi_map in mappings:
            for vdisk in vg_wrap.virtual_disks:
                if vdisk.udid == scsi_map.backing_storage.udid:
                    removals.append(vdisk)
                    break

        # We know that the mappings are VirtualSCSIMappings.  Remove the
        # storage that resides in the scsi map from the volume group
        existing_vds = vg_wrap.virtual_disks
        for removal in removals:
            existing_vds.remove(removal)

        # Now update the volume group to remove the storage.
        self.adapter.update(vg_wrap._element, vg_wrap.etag, pvm_vios.VIO_ROOT,
                            self.vios_uuid, child_type=pvm_st.VG_ROOT,
                            child_id=self.vg_uuid)

    def disconnect_image_volume(self, context, instance, lpar_uuid):
        LOG.info('Disconnecting ephemeral local volume from instance %s'
                 % instance.name)
        # Quick read the VIOS, using specific extended attribute group
        vios_resp = self.adapter.read(pvm_vios.VIO_ROOT, self.vios_uuid,
                                      xag=[pvm_vios.XAG_VIOS_SCSI_MAPPING])
        vios_w = pvm_vios.VirtualIOServer.load_from_response(vios_resp)

        # Find the existing mappings, and then pull them off the VIOS
        existing_vios_mappings = vios_w.scsi_mappings
        existing_maps = vios.get_vscsi_mappings(self.adapter, lpar_uuid,
                                                vios_w, pvm_st.VirtualDisk)
        for scsi_map in existing_maps:
            existing_vios_mappings.remove(scsi_map)

        # Update the VIOS
        self.adapter.update(vios_w._element, vios_w.etag, pvm_vios.VIO_ROOT,
                            vios_w.uuid, xag=[pvm_vios.XAG_VIOS_SCSI_MAPPING])

        # Return the mappings that we just removed.
        return existing_maps

    def create_volume_from_image(self, context, instance, image, disk_size):
        LOG.info(_LI('Create volume.'))

        # Transfer the image
        chunks = self.image_api.download(context, image['id'])
        stream = IterableToFileAdapter(chunks)
        vol_name = self._get_disk_name('boot', instance)

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
        upload_lv.upload_new_vdisk(self.adapter, self.vios_uuid, self.vg_uuid,
                                   stream, vol_name, image['size'],
                                   d_size=disk_bytes)

        return {'device_name': vol_name}

    def connect_volume(self, context, instance, volume_info, lpar_uuid,
                       **kwds):
        vol_name = volume_info['device_name']
        # Create the mapping structure
        scsi_map = pvm_vios.crt_scsi_map_to_vdisk(self.adapter, self.host_uuid,
                                                  lpar_uuid, vol_name)
        # Add the mapping to the VIOS
        vios.add_vscsi_mapping(self.adapter, self.vios_uuid, self.vios_name,
                               scsi_map)

    def extend_volume(self, context, instance, volume_info, size):
        """Extends the disk

        :param context: nova context for operation
        :param instance: instance to create the volume for
        :param volume_info: dictionary with volume info
        :param size: the new size in gb
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
            self.adapter.update(vg_wrap._element, vg_wrap.etag,
                                pvm_consts.VIOS, root_id=self.vios_uuid,
                                child_type=pvm_consts.VOL_GROUP,
                                child_id=self.vg_uuid, xag=None)

        # Get the volume name based on the instance and type
        vol_name = self._get_disk_name(volume_info['type'], instance)
        LOG.info(_LI('Extending disk: %s') % vol_name)
        try:
            _extend()
        except pvm_exc.Error:
            # TODO(IBM): Handle etag mismatch and retry
            LOG.exception()
            raise

    def _get_disk_name(self, type_, instance):
        return type_[:6] + '_' + instance.uuid[:8]

    def _get_vg_uuid(self, adapter, vios_uuid, name):
        try:
            resp = adapter.read(pvm_consts.VIOS,
                                root_id=vios_uuid,
                                child_type=pvm_consts.VOL_GROUP)
        except Exception as e:
            LOG.exception(e)
            raise e

        # Search the feed for the volume group
        vol_grps = pvm_st.VolumeGroup.load_from_response(resp)
        for vol_grp in vol_grps:
            LOG.info(_LI('Volume group: %s') % vol_grp.name)
            if name == vol_grp.name:
                return vol_grp.uuid

        raise VGNotFound(vg_name=name)

    def _get_vg(self):
        vg_rsp = self.adapter.read(pvm_vios.VIO_ROOT, root_id=self.vios_uuid,
                                   child_type=pvm_st.VG_ROOT,
                                   child_id=self.vg_uuid)
        return vg_rsp

    def _get_vg_wrap(self):
        return pvm_st.VolumeGroup.load_from_response(self._get_vg())
