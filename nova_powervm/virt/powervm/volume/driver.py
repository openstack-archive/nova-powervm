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
from nova_powervm.virt.powervm import vm
import six


@six.add_metaclass(abc.ABCMeta)
class PowerVMVolumeAdapter(object):
    """The volume adapter connects a Cinder volume to a VM.

    The role of the volume driver is to perform the connection between the
    compute node and the backing physical fabric.

    This volume adapter is a generic adapter for all volume types to extend.

    This is built similarly to the LibvirtBaseVolumeDriver.
    """
    def __init__(self, adapter, host_uuid, instance, connection_info):
        """Initialize the PowerVMVolumeAdapter

        :param adapter: The pypowervm adapter.
        :param host_uuid: The pypowervm UUID of the host.
        :param instance: The nova instance that the volume should connect to.
        :param connection_info: The volume connection info generated from the
                                BDM. Used to determine how to connect the
                                volume to the VM.
        """
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.instance = instance
        self.connection_info = connection_info
        self.vm_uuid = vm.get_pvm_uuid(instance)

    def connect_volume(self):
        """Connects the volume.
        """
        raise NotImplementedError()

    def disconnect_volume(self):
        """Disconnect the volume.
        """
        raise NotImplementedError()


@six.add_metaclass(abc.ABCMeta)
class FibreChannelVolumeAdapter(PowerVMVolumeAdapter):
    """Defines a Fibre Channel specific volume adapter.

    Fibre Channel has a few additional attributes for the volume adapter.
    This class defines the additional attributes so that the multiple FC
    sub classes can support them.
    """

    def wwpns(self):
        """Builds the WWPNs of the adapters that will connect the ports.

        :returns: The list of WWPNs that need to be included in the zone set.
        """
        raise NotImplementedError()

    def host_name(self):
        """Derives the host name that should be used for the storage device.

        :returns: The host name.
        """
        raise NotImplementedError()
