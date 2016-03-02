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

import abc

import oslo_log.log as logging
from oslo_utils import units
import six

from nova import image
import pypowervm.const as pvm_const
import pypowervm.tasks.scsi_mapper as tsk_map
import pypowervm.util as pvm_util
import pypowervm.wrappers.virtual_io_server as pvm_vios

from nova_powervm.virt.powervm import exception as npvmex
from nova_powervm.virt.powervm.i18n import _
from nova_powervm.virt.powervm.i18n import _LW
from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)


class DiskType(object):
    BOOT = 'boot'
    RESCUE = 'rescue'
    IMAGE = 'image'


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
                chunk = next(self.iterator)
        except StopIteration:
            return ''
        return_value = chunk[0:size]
        self.remaining_data = chunk[size:]
        return return_value


@six.add_metaclass(abc.ABCMeta)
class DiskAdapter(object):

    capabilities = {
        'shared_storage': False,
    }

    def __init__(self, connection):
        """Initialize the DiskAdapter

        :param connection: connection information for the underlying driver
        """
        self._connection = connection
        self.adapter = connection['adapter']
        self.host_uuid = connection['host_uuid']
        self.mp_uuid = connection['mp_uuid']
        self.image_api = image.API()

    @property
    def vios_uuids(self):
        """List the UUIDs of the Virtual I/O Servers hosting the storage."""
        raise NotImplementedError()

    def get_info(self):
        """Return disk information for the driver.

        This method is used on cold migration to pass disk information from
        the source to the destination. The data needed to be retrieved and
        validated (see the validate method below) are determined by the disk
        driver implementation.

        Currently this and the validate method will only be called for the SSP
        driver because it's the only one that supports shared storage.

        :return: returns a dict of disk information
        """
        return {}

    def validate(self, disk_info):
        """Validate the disk information is compatible with this driver.

        This method is called during cold migration to ensure the disk
        drivers on the destination host is compatible with the source host.

        :param disk_info: disk information dictionary
        :returns: None if compatible, otherwise a reason for incompatibility
        """
        return _('The configured disk driver does not support migration '
                 'or resize.')

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
        raise NotImplementedError()

    def instance_disk_iter(self, instance, disk_type=DiskType.BOOT,
                           lpar_wrap=None):
        """Return the instance's storage element wrapper of the specified type.

        :param instance: nova.objects.instance.Instance object owning the
                         requested disk.
        :param disk_type: The type of disk to find, one of the DiskType enum
                          values.
        :param lpar_wrap: pypowervm.wrappers.logical_partition.LPAR
                          corresponding to the instance.  If not specified, it
                          will be retrieved; i.e. specify this parameter to
                          save on REST calls.
        :return: Iterator of tuples of (storage_elem, VIOS), where storage_elem
                 is a storage element wrapper (pypowervm.wrappers.storage.VOpt,
                 VDisk, PV, or LU) associated with the instance; and VIOS is
                 the wrapper of the Virtual I/O Server owning that storage
                 element.
        """
        if lpar_wrap is None:
            lpar_wrap = vm.get_instance_wrapper(self.adapter, instance,
                                                self.host_uuid)
        match_func = self.disk_match_func(disk_type, instance)
        for vios_uuid in self.vios_uuids:
            vios_wrap = pvm_vios.VIOS.wrap(self.adapter.read(
                pvm_vios.VIOS.schema_type, root_id=vios_uuid,
                xag=[pvm_const.XAG.VIO_SMAP]))
            for scsi_map in tsk_map.find_maps(
                    vios_wrap.scsi_mappings, client_lpar_id=lpar_wrap.id,
                    match_func=match_func):
                yield scsi_map.backing_storage, vios_wrap

    def connect_instance_disk_to_mgmt(self, instance):
        """Connect an instance's boot disk to the management partition.

        :param instance: The instance whose boot disk is to be mapped.
        :return stg_elem: The storage element (LU, VDisk, etc.) that was mapped
        :return vios: The EntryWrapper of the VIOS from which the mapping was
                      made.
        :raise InstanceDiskMappingFailed: If the mapping could not be done.
        """
        msg_args = {'instance_name': instance.name}
        lpar_wrap = vm.get_instance_wrapper(self.adapter, instance,
                                            self.host_uuid)
        for stg_elem, vios in self.instance_disk_iter(instance,
                                                      lpar_wrap=lpar_wrap):
            msg_args['disk_name'] = stg_elem.name
            msg_args['vios_name'] = vios.name

            # Create a new mapping.  NOTE: If there's an existing mapping on
            # the other VIOS but not this one, we'll create a second mapping
            # here.  It would take an extreme sequence of events to get to that
            # point, and the second mapping would be harmless anyway. The
            # alternative would be always checking all VIOSes for existing
            # mappings, which increases the response time of the common case by
            # an entire GET of VIOS+VIO_SMAP.
            LOG.debug("Mapping boot disk %(disk_name)s of instance "
                      "%(instance_name)s to the management partition from "
                      "Virtual I/O Server %(vios_name)s.", msg_args)
            try:
                tsk_map.add_vscsi_mapping(self.host_uuid, vios, self.mp_uuid,
                                          stg_elem)
                # If that worked, we're done.  add_vscsi_mapping logged.
                return stg_elem, vios
            except Exception as e:
                msg_args['exc'] = e
                LOG.warning(_LW("Failed to map boot disk %(disk_name)s of "
                                "instance %(instance_name)s to the management "
                                "partition from Virtual I/O Server "
                                "%(vios_name)s: %(exc)s"), msg_args)
                # Try the next hit, if available.
        # We either didn't find the boot dev, or failed all attempts to map it.
        raise npvmex.InstanceDiskMappingFailed(**msg_args)

    def disconnect_disk_from_mgmt(self, vios_uuid, disk_name):
        """Disconnect a disk from the management partition.

        :param vios_uuid: The UUID of the Virtual I/O Server serving the
                          mapping.
        :param disk_name: The name of the disk to unmap.
        """
        raise NotImplementedError()

    @property
    def capacity(self):
        """Capacity of the storage in gigabytes

        Default is to make the capacity arbitrarily large
        """
        return 1 << 21

    @property
    def capacity_used(self):
        """Capacity of the storage in gigabytes that is used

        Default is to say none of it is used.
        """
        return 0

    def _get_image_upload(self, context, image_meta):
        """Returns the stream that can be sent to pypowervm.

        The pypowervm API requires a File be sent up for the image.  This
        method will get the appropriate file adapter (IterableToFileAdapter)
        built for the invoker.

        :param context: User context
        :param nova.objects.ImageMeta image_meta:
            The metadata of the image of the instance.
        :return: The stream to send to pypowervm.
        """
        chunks = self.image_api.download(context, image_meta.id)
        return IterableToFileAdapter(chunks)

    @staticmethod
    def _get_disk_name(disk_type, instance, short=False):
        """Generate a name for a virtual disk associated with an instance.

        :param disk_type: One of the DiskType enum values.
        :param instance: The instance for which the disk is to be created.
        :param short: If True, the generated name will be limited to 15
                      characters (the limit for virtual disk).  If False, it
                      will be limited by the API (79 characters currently).
        :return:
        """
        prefix = '%s_' % (disk_type[0] if short else disk_type)
        base = ('%s_%s' % (instance.name[:8], instance.uuid[:4]) if short
                else instance.name)
        return pvm_util.sanitize_file_name_for_api(
            base, prefix=prefix, max_len=pvm_const.MaxLen.VDISK_NAME if short
            else pvm_const.MaxLen.FILENAME_DEFAULT)

    @staticmethod
    def _get_image_name(image_meta, max_len=pvm_const.MaxLen.FILENAME_DEFAULT):
        """Generate a name for a virtual storage copy of an image."""
        return pvm_util.sanitize_file_name_for_api(
            image_meta.name, prefix=DiskType.IMAGE + '_',
            suffix='_' + image_meta.checksum, max_len=max_len)

    @staticmethod
    def _disk_gb_to_bytes(size_gb, floor=None):
        """Convert a GB size (usually of a disk) to bytes, with a minimum.

        :param size_gb: GB size to convert
        :param floor: The minimum value to return.  If specified, and the
                      converted size_gb is smaller, this value will be returned
                      instead.
        :return: A size in bytes.
        """
        disk_bytes = size_gb * units.Gi
        if floor is not None:
            if disk_bytes < floor:
                disk_bytes = floor
        return disk_bytes

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
        pass

    def delete_disks(self, context, instance, storage_elems):
        """Removes the disks specified by the mappings.

        :param context: nova context for operation
        :param instance: instance to delete the disk for.
        :param storage_elems: A list of the storage elements that are to be
                              deleted.  Derived from the return value from
                              disconnect_image_disk.
        """
        pass

    def create_disk_from_image(self, context, instance, image_meta, disk_size,
                               image_type=DiskType.BOOT):
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
        pass

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
        pass

    def extend_disk(self, context, instance, disk_info, size):
        """Extends the disk.

        :param context: nova context for operation.
        :param instance: instance to extend the disk for.
        :param disk_info: dictionary with disk info.
        :param size: the new size in gb.
        """
        raise NotImplementedError()

    def check_instance_shared_storage_local(self, context, instance):
        """Check if instance files located on shared storage.

        This runs check on the destination host, and then calls
        back to the source host to check the results.

        :param context: security context
        :param instance: nova.objects.instance.Instance object
        """
        raise NotImplementedError()

    def check_instance_shared_storage_remote(self, context, data):
        """Check if instance files located on shared storage.

        :param context: security context
        :param data: result of check_instance_shared_storage_local
        """
        raise NotImplementedError()

    def check_instance_shared_storage_cleanup(self, context, data):
        """Do cleanup on host after check_instance_shared_storage calls

        :param context: security context
        :param data: result of check_instance_shared_storage_local
        """
        pass
