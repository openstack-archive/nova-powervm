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

    def delete_volume(self, context, volume_info):
        """Removes the disk and its associated connection

        :param context: nova context for operation
        :param volume_info: dictionary with volume info
        """
        pass

    def create_volume_from_image(self, context, instance, image):
        """Creates a Volume and copies the specified image to it

        :param context: nova context used to retrieve image from glance
        :param instance: instance to create the volume for
        :param image_id: image_id reference used to locate image in glance
        :returns: dictionary with the name of the created
                  disk device in 'device_name' key
        """
        pass

    def connect_volume(self, context, instance, volume, **kwds):
        pass
