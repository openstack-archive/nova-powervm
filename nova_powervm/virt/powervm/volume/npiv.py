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


class NPIVVolumeDriver(object):
    """The NPIV implementation of the Volume Driver.

    NPIV stands for N_Port ID Virtualization.  It is a means of providing
    more efficient connections between virtual machines and Fibre Channel
    backed SAN fabrics.

    From a management level, the main difference is that the Virtual Machine
    will have its own WWPNs and own Virtual FC adapter.  The Virtual I/O
    Server only passes through communication directly to the VM itself.
    """

    def connect_volume(self, adapter, instance, connection_info, disk_dev):
        """Connects the volume."""
        pass

    def disconnect_volume(self, adapter, instance, connection_info, disk_dev):
        """Disconnect the volume."""
        pass
