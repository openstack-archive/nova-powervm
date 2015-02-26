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

import six

from nova import image

BOOT_DISK = 'boot'
RESCUE_DISK = 'rescue'


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


@six.add_metaclass(abc.ABCMeta)
class StorageAdapter(object):

    def __init__(self, connection):
        """Initialize the DiskAdapter

        :param connection: connection information for the underlying driver
        """
        self._connection = connection
        self.image_api = image.API()

    @property
    def capacity(self):
        """Capacity of the storage in gigabytes

        Default is to make the capacity arbitrarily large
        """
        return (1 << 21)

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

        Also handles the scenario where a disk is requested, but the boot
        volume is via cinder.  In that case, a stream is still returned.  But
        it consists of zero bytes.

        :param context: User context
        :param image_meta: The image metadata.
        :return: The stream to send to pypowervm.
        """
        chunks = self.image_api.download(context, image_meta['id'])
        return IterableToFileAdapter(chunks)

    def disconnect_image_volume(self, context, instance, lpar_uuid):
        """Disconnects the storage adapters from the image volume.

        :param context: nova context for operation
        :param context: nova context for operation
        :param instance: instance to delete the image for.
        :param lpar_uuid: The UUID for the pypowervm LPAR element.
        :param disk_type: The list of disk types to remove or None which means
            to remove all disks from the VM.
        :return: A list of Mappings (either pypowervm VSCSIMappings or
                 VFCMappings)
        """
        pass

    def delete_volumes(self, context, instance, mappings):
        """Removes the disks specified by the mappings.

        :param context: nova context for operation
        :param instance: instance to delete the image for.
        :param mappings: The mappings that had been used to identify the
                         backing storage.  List of pypowervm VSCSIMappings or
                         VFCMappings. Typically derived from disconnect_volume.
        """
        pass

    def create_volume_from_image(self, context, instance, image, disk_size,
                                 image_type=BOOT_DISK):
        """Creates a Volume and copies the specified image to it

        :param context: nova context used to retrieve image from glance
        :param instance: instance to create the volume for
        :param image_id: image_id reference used to locate image in glance
        :param disk_size: The size of the disk to create in GB.  If smaller
                          than the image, it will be ignored (as the disk
                          must be at least as big as the image).  Must be an
                          int.
        :param image_type: the image type. See disk constants above.
        :returns: dictionary with the name of the created
                  disk device in 'device_name' key
        """
        pass

    def connect_volume(self, context, instance, volume_info, lpar_uuid,
                       **kwds):
        pass

    def extend_volume(self, context, instance, volume_info, size):
        """Extends the disk

        :param context: nova context for operation
        :param instance: instance to create the volume for
        :param volume_info: dictionary with volume info
        :param size: the new size in gb
        """
        raise NotImplementedError()
