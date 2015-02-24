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


@six.add_metaclass(abc.ABCMeta)
class StorageAdapter(object):

    def __init__(self, connection):
        """Initialize the DiskAdapter

        :param connection: connection information for the underlying driver
        """
        self._connection = connection

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

    def disconnect_image_volume(self, context, instance, lpar_uuid):
        """Disconnects the storage adapters from the image volume.

        :param context: nova context for operation
        :param context: nova context for operation
        :param instance: instance to delete the image for.
        :param lpar_uuid: The UUID for the pypowervm LPAR element.
        :return: A list of Mappings (either pypowervm VirtualSCSIMappings or
                 VirtualFCMappings)
        """
        pass

    def delete_volumes(self, context, instance, mappings):
        """Removes the disks specified by the mappings.

        :param context: nova context for operation
        :param instance: instance to delete the image for.
        :param mappings: The mappings that had been used to identify the
                         backing storage.  List of pypowervm
                         VirtualSCSIMappings or VirtualFCMappings.
                         Typically derived from disconnect_image_volume.
        """
        pass

    def create_volume_from_image(self, context, instance, image, disk_size):
        """Creates a Volume and copies the specified image to it

        :param context: nova context used to retrieve image from glance
        :param instance: instance to create the volume for
        :param image_id: image_id reference used to locate image in glance
        :param disk_size: The size of the disk to create in GB.  If smaller
                          than the image, it will be ignored (as the disk
                          must be at least as big as the image).  Must be an
                          int.
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
