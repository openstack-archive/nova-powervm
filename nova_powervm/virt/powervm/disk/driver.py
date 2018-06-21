# Copyright 2013 OpenStack Foundation
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

import abc

import oslo_log.log as logging
from oslo_utils import excutils
from oslo_utils import units
import random
import six
import time

import pypowervm.const as pvm_const
import pypowervm.tasks.scsi_mapper as tsk_map
import pypowervm.util as pvm_util
import pypowervm.wrappers.virtual_io_server as pvm_vios

from nova_powervm.virt.powervm import exception as npvmex
from nova_powervm.virt.powervm.i18n import _
from nova_powervm.virt.powervm import mgmt
from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)


class DiskType(object):
    BOOT = 'boot'
    RESCUE = 'rescue'
    IMAGE = 'image'


class IterableToFileAdapter(object):
    """A degenerate file-like so that an iterable can be read like a file.

    The Glance client returns an iterable, but PowerVM requires a file.  This
    is the adapter between the two.

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
        'has_imagecache': False,
        'snapshot': False,
    }

    def __init__(self, adapter, host_uuid):
        """Initialize the DiskAdapter

        :param adapter: The pypowervm adapter
        :param host_uuid: The UUID of the PowerVM host.
        """
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.mp_uuid = mgmt.mgmt_uuid(self.adapter)

    @abc.abstractproperty
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

    def manage_image_cache(self, context, all_instances):
        """Update the image cache.

        Only called if implentation has the capability: has_imagecache.

        :param context: nova context
        :param all_instances: List of all instances on the node
        """
        pass

    def validate(self, disk_info):
        """Validate the disk information is compatible with this driver.

        This method is called during cold migration to ensure the disk
        drivers on the destination host is compatible with the source host.

        :param disk_info: disk information dictionary
        :returns: None if compatible, otherwise a reason for incompatibility
        """
        return _('The configured disk driver does not support migration '
                 'or resize.')

    @abc.abstractmethod
    def _disk_match_func(self, disk_type, instance):
        """Return a matching function to locate the disk for an instance.

        :param disk_type: One of the DiskType enum values.
        :param instance: The instance whose disk is to be found.
        :return: Callable suitable for the match_func parameter of the
                 pypowervm.tasks.scsi_mapper.find_maps method.
        """
        raise NotImplementedError()

    def get_bootdisk_path(self, instance, vios_uuid):
        """Find the local path for the instance's boot disk.

        :param instance: nova.objects.instance.Instance object owning the
                         requested disk.
        :param vios_uuid: PowerVM UUID of the VIOS to search for mappings.
        :return: Local path for instance's boot disk.
        """
        vm_uuid = vm.get_pvm_uuid(instance)
        match_func = self._disk_match_func(DiskType.BOOT, instance)
        vios_wrap = pvm_vios.VIOS.get(self.adapter, uuid=vios_uuid,
                                      xag=[pvm_const.XAG.VIO_SMAP])
        maps = tsk_map.find_maps(vios_wrap.scsi_mappings,
                                 client_lpar_id=vm_uuid, match_func=match_func)
        if maps:
            return maps[0].server_adapter.backing_dev_name
        return None

    def _get_bootdisk_iter(self, instance):
        """Return an iterator of (storage_elem, VIOS) tuples for the instance.

        This method returns an iterator of (storage_elem, VIOS) tuples, where
        storage_elem is a pypowervm storage element wrapper associated with
        the instance boot disk and VIOS is the wrapper of the Virtual I/O
        server owning that storage element.

        :param instance: nova.objects.instance.Instance object owning the
                         requested disk.
        :return: Iterator of tuples of (storage_elem, VIOS).
        """
        lpar_wrap = vm.get_instance_wrapper(self.adapter, instance)
        match_func = self._disk_match_func(DiskType.BOOT, instance)
        for vios_uuid in self.vios_uuids:
            vios_wrap = pvm_vios.VIOS.get(
                self.adapter, uuid=vios_uuid, xag=[pvm_const.XAG.VIO_SMAP])
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
        for stg_elem, vios in self._get_bootdisk_iter(instance):
            msg_args = {'disk_name': stg_elem.name, 'vios_name': vios.name}

            # Create a new mapping.  NOTE: If there's an existing mapping on
            # the other VIOS but not this one, we'll create a second mapping
            # here.  It would take an extreme sequence of events to get to that
            # point, and the second mapping would be harmless anyway. The
            # alternative would be always checking all VIOSes for existing
            # mappings, which increases the response time of the common case by
            # an entire GET of VIOS+VIO_SMAP.
            LOG.debug("Mapping boot disk %(disk_name)s to the management "
                      "partition from Virtual I/O Server %(vios_name)s.",
                      msg_args, instance=instance)
            try:
                tsk_map.add_vscsi_mapping(self.host_uuid, vios, self.mp_uuid,
                                          stg_elem)
                # If that worked, we're done.  add_vscsi_mapping logged.
                return stg_elem, vios
            except Exception:
                LOG.exception("Failed to map boot disk %(disk_name)s to the "
                              "management partition from Virtual I/O Server "
                              "%(vios_name)s.", msg_args, instance=instance)
                # Try the next hit, if available.
        # We either didn't find the boot dev, or failed all attempts to map it.
        raise npvmex.InstanceDiskMappingFailed(instance_name=instance.name)

    @abc.abstractmethod
    def disconnect_disk_from_mgmt(self, vios_uuid, disk_name):
        """Disconnect a disk from the management partition.

        :param vios_uuid: The UUID of the Virtual I/O Server serving the
                          mapping.
        :param disk_name: The name of the disk to unmap.
        """
        raise NotImplementedError()

    @abc.abstractproperty
    def capacity(self):
        """Capacity of the storage in gigabytes."""
        raise NotImplementedError()

    @abc.abstractproperty
    def capacity_used(self):
        """Capacity of the storage in gigabytes that is used."""
        raise NotImplementedError()

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
    def get_name_by_uuid(disk_type, uuid, short=False):
        """Generate a name for a DiskType using a given uuid.

        :param disk_type: One of the DiskType enum values.
        :param uuid: The uuid to use for the name
        :param short: If True the generate name will be limited to 15
                      characters.  If False it will be limited by the API.
        :return: A name base off of disk_type and uuid.
        """
        prefix = '%s_' % (disk_type[0] if short else disk_type)
        return pvm_util.sanitize_file_name_for_api(
            uuid, prefix=prefix, max_len=pvm_const.MaxLen.VDISK_NAME if short
            else pvm_const.MaxLen.FILENAME_DEFAULT)

    @staticmethod
    def _get_image_name(image_meta, max_len=pvm_const.MaxLen.FILENAME_DEFAULT):
        """Generate a name for a virtual storage copy of an image.

        :param nova.objects.ImageMeta image_meta:
            The metadata of the image of the instance.
        :param max_len: Maximum string length for the resulting image name.
        :return: String name for the image on the server.
        """
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

    @abc.abstractmethod
    def disconnect_disk(self, instance, stg_ftsk=None, disk_type=None):
        """Disconnects the storage adapters from the image disk.

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
        raise NotImplementedError()

    @abc.abstractmethod
    def delete_disks(self, storage_elems):
        """Removes the disks specified by the mappings.

        :param storage_elems: A list of the storage elements that are to be
                              deleted.  Derived from the return value from
                              disconnect_disk.
        """
        raise NotImplementedError()

    def create_disk_from_image(self, context, instance, image_meta,
                               image_type=DiskType.BOOT):
        """Creates a disk and copies the specified image to it.

        :param context: nova context used to retrieve image from glance
        :param instance: instance to create the disk for.
        :param nova.objects.ImageMeta image_meta:
            The metadata of the image of the instance.
        :param image_type: the image type. See disk constants above.
        :return: The backing pypowervm storage object that was created.
        """

        # Retry 3 times on exception
        for attempt in range(1, 5):
            try:
                return self._create_disk_from_image(
                    context, instance, image_meta, image_type=image_type)
            except Exception:
                with excutils.save_and_reraise_exception(
                    logger=LOG, reraise=False) as sare:
                    if attempt < 4:
                        LOG.exception("Disk Upload attempt #%d failed. "
                                      "Retrying the upload.", attempt,
                                      instance=instance)
                        time.sleep(random.randint(1, 5))
                    else:
                        sare.reraise = True

    def _create_disk_from_image(self, context, instance, image_meta,
                                image_type=DiskType.BOOT):
        """Creates a disk and copies the specified image to it.

        Cleans up created disk if an error occurs.

        :param context: nova context used to retrieve image from glance
        :param instance: instance to create the disk for.
        :param nova.objects.ImageMeta image_meta:
            The metadata of the image of the instance.
        :param image_type: the image type. See disk constants above.
        :return: The backing pypowervm storage object that was created.
        """
        pass

    @abc.abstractmethod
    def connect_disk(self, instance, disk_info, stg_ftsk=None):
        """Connects the disk image to the Virtual Machine.

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
        raise NotImplementedError()

    @abc.abstractmethod
    def extend_disk(self, instance, disk_info, size):
        """Extends the disk.

        :param instance: instance to extend the disk for.
        :param disk_info: dictionary with disk info.
        :param size: the new size in gb.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def check_instance_shared_storage_local(self, context, instance):
        """Check if instance files located on shared storage.

        This runs check on the destination host, and then calls
        back to the source host to check the results.

        :param context: security context
        :param instance: nova.objects.instance.Instance object
        """
        raise NotImplementedError()

    @abc.abstractmethod
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
