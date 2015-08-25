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

from pypowervm.utils import transaction as pvm_tx
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.virt.powervm import vm

LOCAL_FEED_TASK = 'local_feed_task'


@six.add_metaclass(abc.ABCMeta)
class PowerVMVolumeAdapter(object):
    """The volume adapter connects a Cinder volume to a VM.

    The role of the volume driver is to perform the connection between the
    compute node and the backing physical fabric.

    This volume adapter is a generic adapter for all volume types to extend.

    This is built similarly to the LibvirtBaseVolumeDriver.
    """
    def __init__(self, adapter, host_uuid, instance, connection_info,
                 tx_mgr=None):
        """Initialize the PowerVMVolumeAdapter

        :param adapter: The pypowervm adapter.
        :param host_uuid: The pypowervm UUID of the host.
        :param instance: The nova instance that the volume should connect to.
        :param connection_info: The volume connection info generated from the
                                BDM. Used to determine how to connect the
                                volume to the VM.
        :param tx_mgr: (Optional) The pypowervm transaction FeedTask for
                       the I/O Operations.  If provided, the Virtual I/O Server
                       mapping updates will be added to the FeedTask.  This
                       defers the updates to some later point in time.  If the
                       FeedTask is not provided, the updates will be run
                       immediately when this method is executed.
        """
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.instance = instance
        self.connection_info = connection_info
        self.vm_uuid = vm.get_pvm_uuid(instance)
        # Lazy-set this
        self._vm_id = None

        self.reset_tx_mgr(tx_mgr=tx_mgr)

    @property
    def vm_id(self):
        """Return the short ID (not UUID) of the LPAR for our instance.

        This method is unavailable during a pre live migration call since
        there is no instance of the VM on the destination host at the time.
        """
        if self._vm_id is None:
            self._vm_id = vm.get_vm_id(self.adapter, self.vm_uuid)
        return self._vm_id

    @property
    def volume_id(self):
        """Method to return the volume id.

        Every driver must implement this method if the default impl will
        not work for their data.
        """
        return self.connection_info['data']['volume_id']

    def reset_tx_mgr(self, tx_mgr=None):
        """Resets the pypowervm transaction FeedTask to a new value.

        The previous updates from the original FeedTask WILL NOT be migrated
        to this new FeedTask.

        :param tx_mgr: (Optional) The pypowervm transaction FeedTask for
                       the I/O Operations.  If provided, the Virtual I/O Server
                       mapping updates will be added to the FeedTask.  This
                       defers the updates to some later point in time.  If the
                       FeedTask is not provided, the updates will be run
                       immediately when this method is executed.
        """
        if tx_mgr is None:
            getter = pvm_vios.VIOS.getter(self.adapter, xag=self.min_xags())
            self.tx_mgr = pvm_tx.FeedTask(LOCAL_FEED_TASK, getter)
        else:
            self.tx_mgr = tx_mgr

    @classmethod
    def min_xags(cls):
        """List of pypowervm XAGs needed to support this adapter."""
        raise NotImplementedError()

    def pre_live_migration_on_destination(self):
        """Perform pre live migration steps for the volume on the target host.

        This method performs any pre live migration that is needed.

        """
        raise NotImplementedError()

    def connect_volume(self):
        """Connects the volume."""
        self._connect_volume()

        if self.tx_mgr.name == LOCAL_FEED_TASK:
            self.tx_mgr.execute()

    def disconnect_volume(self):
        """Disconnect the volume."""
        self._disconnect_volume()

        if self.tx_mgr.name == LOCAL_FEED_TASK:
            self.tx_mgr.execute()

    def _connect_volume(self):
        """Connects the volume.

        This is the actual method to implement within the subclass.  Some
        transaction maintenance is done by the parent class.
        """
        raise NotImplementedError()

    def _disconnect_volume(self):
        """Disconnect the volume.

        This is the actual method to implement within the subclass.  Some
        transaction maintenance is done by the parent class.
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

        :return: The list of WWPNs that need to be included in the zone set.
        """
        raise NotImplementedError()

    def host_name(self):
        """Derives the host name that should be used for the storage device.

        :return: The host name.
        """
        raise NotImplementedError()
