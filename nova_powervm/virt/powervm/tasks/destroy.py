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

from nova.i18n import _LI
from nova.openstack.common import log as logging
from pypowervm.jobs import power
from pypowervm.wrappers import logical_partition as lpar_w

from taskflow import task

from nova_powervm.virt.powervm import media
from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)


class PowerOffVM(task.Task):
    """The task to power off a VM."""

    def __init__(self, adapter, host_uuid, lpar_uuid, instance):
        """Creates the Task to power off an LPAR.

        :param adapter: The adapter for the pypowervm API
        :param host_uuid: The host UUID
        :param lpar_uuid: The UUID of the lpar that has media.
        :param instance: The nova instance.
        """
        super(PowerOffVM, self).__init__(name='pwr_off_lpar')
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.lpar_uuid = lpar_uuid
        self.instance = instance

    def execute(self):
        LOG.info(_LI('Powering off instance %s for deletion')
                 % self.instance.name)
        resp = self.adapter.read(lpar_w.LPAR_ROOT, self.lpar_uuid)
        lpar = lpar_w.LogicalPartition.load_from_response(resp)
        power.power_off(self.adapter, lpar, self.host_uuid,
                        force_immediate=True)


class DeleteVOpt(task.Task):
    """The task to delete the virtual optical."""

    def __init__(self, adapter, host_uuid, vios_uuid, instance, lpar_uuid):
        """Creates the Task to delete the instances virtual optical media.

        :param adapter: The adapter for the pypowervm API
        :param host_uuid: The host UUID of the system.
        :param vios_uuid: The VIOS UUID the media is being deleted from.
        :param instance: The nova instance.
        :param lpar_uuid: The UUID of the lpar that has media.
        """
        super(DeleteVOpt, self).__init__(name='vopt_delete')
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.vios_uuid = vios_uuid
        self.instance = instance
        self.lpar_uuid = lpar_uuid

    def execute(self):
        LOG.info(_LI('Deleting Virtual Optical Media for instance %s')
                 % self.instance.name)
        media_builder = media.ConfigDrivePowerVM(self.adapter, self.host_uuid,
                                                 self.vios_uuid)
        media_builder.dlt_vopt(self.lpar_uuid)


class DetachStorage(task.Task):
    """The task to detach the storage from the instance."""

    def __init__(self, block_dvr, context, instance, lpar_uuid):
        """Creates the Task to detach the storage adapters.

        Provides the stor_adpt_mappings.  A list of pypowervm
        VirtualSCSIMappings or VirtualFCMappings (depending on the storage
        adapter).

        :param block_dvr: The StorageAdapter for the VM.
        :param context: The nova context.
        :param instance: The nova instance.
        :param lpar_uuid: The UUID of the lpar..
        """
        super(DetachStorage, self).__init__(name='detach_storage',
                                            provides='stor_adpt_mappings')
        self.block_dvr = block_dvr
        self.context = context
        self.instance = instance
        self.lpar_uuid = lpar_uuid

    def execute(self):
        LOG.info(_LI('Detaching disk storage adapters for instance %s')
                 % self.instance.name)
        return self.block_dvr.disconnect_image_volume(self.context,
                                                      self.instance,
                                                      self.lpar_uuid)


class DeleteStorage(task.Task):
    """The task to delete the backing storage."""

    def __init__(self, block_dvr, context, instance):
        """Creates the Task to delete the storage from the system.

        Requires the stor_adpt_mappings.

        :param block_dvr: The StorageAdapter for the VM.
        :param context: The nova context.
        :param instance: The nova instance.
        """
        req = ['stor_adpt_mappings']
        super(DeleteStorage, self).__init__(name='dlt_storage',
                                            requires=req)
        self.block_dvr = block_dvr
        self.context = context
        self.instance = instance

    def execute(self, stor_adpt_mappings):
        LOG.info(_LI('Deleting storage for instance %s.') % self.instance.name)
        self.block_dvr.delete_volumes(self.context, self.instance,
                                      stor_adpt_mappings)


class DeleteVM(task.Task):
    """The task to delete the instance from the system."""

    def __init__(self, adapter, lpar_uuid, instance):
        """Create the Task to delete the VM from the system.

        :param adapter: The adapter for the pypowervm API.
        :param lpar_uuid: The VM's PowerVM UUID.
        :param instance: The nova instance.
        """
        super(DeleteVM, self).__init__(name='dlt_lpar')
        self.adapter = adapter
        self.lpar_uuid = lpar_uuid
        self.instance = instance

    def execute(self):
        LOG.info(_LI('Deleting instance %s from system.') % self.instance.name)
        vm.dlt_lpar(self.adapter, self.lpar_uuid)
