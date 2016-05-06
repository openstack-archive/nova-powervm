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

from nova_powervm.virt.powervm import exception as exc
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
                 stg_ftsk=None):
        """Initialize the PowerVMVolumeAdapter

        :param adapter: The pypowervm adapter.
        :param host_uuid: The pypowervm UUID of the host.
        :param instance: The nova instance that the volume should connect to.
        :param connection_info: The volume connection info generated from the
                                BDM. Used to determine how to connect the
                                volume to the VM.
        :param stg_ftsk: (Optional) The pypowervm transaction FeedTask for the
                         I/O Operations.  If provided, the Virtual I/O Server
                         mapping updates will be added to the FeedTask.  This
                         defers the updates to some later point in time.  If
                         the FeedTask is not provided, the updates will be run
                         immediately when the respective method is executed.
        """
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.instance = instance
        self.connection_info = connection_info
        self.vm_uuid = vm.get_pvm_uuid(instance)
        # Lazy-set this
        self._vm_id = None

        self.reset_stg_ftsk(stg_ftsk=stg_ftsk)

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

    def reset_stg_ftsk(self, stg_ftsk=None):
        """Resets the pypowervm transaction FeedTask to a new value.

        The previous updates from the original FeedTask WILL NOT be migrated
        to this new FeedTask.

        :param stg_ftsk: (Optional) The pypowervm transaction FeedTask for the
                         I/O Operations.  If provided, the Virtual I/O Server
                         mapping updates will be added to the FeedTask.  This
                         defers the updates to some later point in time.  If
                         the FeedTask is not provided, the updates will be run
                         immediately when this method is executed.
        """
        if stg_ftsk is None:
            getter = pvm_vios.VIOS.getter(self.adapter, xag=self.min_xags())
            self.stg_ftsk = pvm_tx.FeedTask(LOCAL_FEED_TASK, getter)
        else:
            self.stg_ftsk = stg_ftsk

    @classmethod
    def min_xags(cls):
        """List of pypowervm XAGs needed to support this adapter."""
        raise NotImplementedError()

    @classmethod
    def vol_type(cls):
        """The type of volume supported by this driver."""
        raise NotImplementedError()

    def pre_live_migration_on_destination(self, mig_data):
        """Perform pre live migration steps for the volume on the target host.

        This method performs any pre live migration that is needed.

        Certain volume connectors may need to pass data from the source host
        to the target.  This may be required to determine how volumes connect
        through the Virtual I/O Servers.

        This method will be called after the pre_live_migration_on_source
        method.  The data from the pre_live call will be passed in via the
        mig_data.  This method should put its output into the dest_mig_data.

        :param mig_data: Dict of migration data for the destination server.
                         If the volume connector needs to provide
                         information to the live_migration command, it
                         should be added to this dictionary.
        """
        raise NotImplementedError()

    def pre_live_migration_on_source(self, mig_data):
        """Performs pre live migration steps for the volume on the source host.

        Certain volume connectors may need to pass data from the source host
        to the target.  This may be required to determine how volumes connect
        through the Virtual I/O Servers.

        This method gives the volume connector an opportunity to update the
        mig_data (a dictionary) with any data that is needed for the target
        host during the pre-live migration step.

        Since the source host has no native pre_live_migration step, this is
        invoked from check_can_live_migrate_source in the overall live
        migration flow.

        :param mig_data: A dictionary that the method can update to include
                         data needed by the pre_live_migration_at_destination
                         method.
        """
        pass

    def post_live_migration_at_source(self, migrate_data):
        """Performs post live migration for the volume on the source host.

        This method can be used to handle any steps that need to taken on
        the source host after the VM is on the destination.

        :param migrate_data: volume migration data
        """
        pass

    def post_live_migration_at_destination(self, mig_vol_stor):
        """Perform post live migration steps for the volume on the target host.

        This method performs any post live migration that is needed.  Is not
        required to be implemented.

        :param mig_vol_stor: An unbounded dictionary that will be passed to
                             each volume adapter during the post live migration
                             call.  Adapters can store data in here that may
                             be used by subsequent volume adapters.
        """
        pass

    def cleanup_volume_at_destination(self, migrate_data):
        """Performs volume cleanup after LPM failure on the dest host.

        This method can be used to handle any steps that need to taken on
        the destination host after the migration has failed.

        :param migrate_data: migration data
        """
        pass

    def connect_volume(self, slot_mgr):
        """Connects the volume.

        :param slot_mgr: A NovaSlotManager.  Used to store/retrieve the client
                         slots used when a volume is attached to the VM
        """
        # Check if the VM is in a state where the attach is acceptable.
        lpar_w = vm.get_instance_wrapper(self.adapter, self.instance,
                                         self.host_uuid)
        capable, reason = lpar_w.can_modify_io()
        if not capable:
            raise exc.VolumeAttachFailed(
                volume_id=self.volume_id, instance_name=self.instance.name,
                reason=reason)

        # Run the connect
        self._connect_volume(slot_mgr)

        if self.stg_ftsk.name == LOCAL_FEED_TASK:
            self.stg_ftsk.execute()

    def disconnect_volume(self, slot_mgr):
        """Disconnect the volume.

        :param slot_mgr: A NovaSlotManager.  Used to store/retrieve the client
                         slots used when a volume is detached from the VM.
        """
        # Check if the VM is in a state where the detach is acceptable.
        lpar_w = vm.get_instance_wrapper(self.adapter, self.instance,
                                         self.host_uuid)
        capable, reason = lpar_w.can_modify_io()
        if not capable:
            raise exc.VolumeDetachFailed(
                volume_id=self.volume_id, instance_name=self.instance.name,
                reason=reason)

        # Run the disconnect
        self._disconnect_volume(slot_mgr)

        if self.stg_ftsk.name == LOCAL_FEED_TASK:
            self.stg_ftsk.execute()

    def _connect_volume(self, slot_mgr):
        """Connects the volume.

        This is the actual method to implement within the subclass.  Some
        transaction maintenance is done by the parent class.

        :param slot_mgr: A NovaSlotStore.  Used to store/retrieve the client
                         slots used when a volume is attached to the VM.
        """
        raise NotImplementedError()

    def _disconnect_volume(self, slot_mgr):
        """Disconnect the volume.

        This is the actual method to implement within the subclass.  Some
        transaction maintenance is done by the parent class.

        :param slot_mgr: A NovaSlotManager.  Used to delete the client slots
                         used when a volume is detached from the VM
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
