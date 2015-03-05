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


class VscsiVolumeDriver(object):
    """The vSCSI implementation of the Volume Driver.

    vSCSI is the internal mechanism to link a given hdisk on the Virtual
    I/O Server to a Virtual Machine.  This volume driver will take the
    information from the driver and link it to a given virtual machine.
    """

    def connect_volume(self, adapter, instance, connection_info, disk_dev):
        """Connects the volume."""
        pass

    def disconnect_volume(self, adapter, instance, connection_info, disk_dev):
        """Disconnect the volume."""
        pass
