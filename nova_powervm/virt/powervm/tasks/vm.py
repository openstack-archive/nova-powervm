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
from pypowervm.tasks import power

from oslo_log import log as logging
from taskflow import task
from taskflow.types import failure as task_fail

from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)


class Get(task.Task):
    """The task for getting a VM entry."""

    def __init__(self, adapter, host_uuid, instance):
        """Creates the Task for getting a VM entry.

        Provides the 'lpar_wrap' for other tasks.

        :param adapter: The adapter for the pypowervm API
        :param host_uuid: The host UUID
        :param instance: The nova instance.
        """
        super(Get, self).__init__(name='get_lpar', provides='lpar_wrap')
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.instance = instance

    def execute(self):
        return vm.get_instance_wrapper(self.adapter, self.instance,
                                       self.host_uuid)


class Create(task.Task):
    """The task for creating a VM."""

    def __init__(self, adapter, host_wrapper, instance, flavor):
        """Creates the Task for creating a VM.

        The revert method is not implemented because the compute manager
        calls the driver destroy operation for spawn errors.  By not deleting
        the lpar, it's a cleaner flow through the destroy operation and
        accomplishes the same result.

        Provides the 'lpar_wrap' for other tasks.

        :param adapter: The adapter for the pypowervm API
        :param host_wrapper: The managed system wrapper
        :param instance: The nova instance.
        :param flavor: The nova flavor.
        """
        super(Create, self).__init__(name='crt_lpar',
                                     provides='lpar_wrap')
        self.adapter = adapter
        self.host_wrapper = host_wrapper
        self.instance = instance
        self.flavor = flavor

    def execute(self):
        LOG.info(_LI('Creating instance: %s'), self.instance.name)
        wrap = vm.crt_lpar(self.adapter, self.host_wrapper, self.instance,
                           self.flavor)
        return wrap


class PowerOn(task.Task):
    """The task to power on the instance."""

    def __init__(self, adapter, host_uuid, instance, pwr_opts=None):
        """Create the Task for the power on of the LPAR.

        Obtains LPAR info through requirement of lpar_wrap (provided by
        tf_crt_lpar).

        :param adapter: The pypowervm adapter.
        :param host_uuid: The host UUID.
        :param instance: The nova instance.
        """
        super(PowerOn, self).__init__(name='pwr_lpar',
                                      requires=['lpar_wrap'])
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.instance = instance
        self.pwr_opts = pwr_opts

    def execute(self, lpar_wrap):
        LOG.info(_LI('Powering on instance: %s'), self.instance.name)
        power.power_on(lpar_wrap, self.host_uuid, add_parms=self.pwr_opts)

    def revert(self, lpar_wrap, result, flow_failures):
        LOG.info(_LI('Powering off instance: %s'), self.instance.name)

        if isinstance(result, task_fail.Failure):
            # The power on itself failed...can't power off.
            LOG.debug('Power on failed.  Not performing power off.')
            return

        power.power_off(lpar_wrap, self.host_uuid, force_immediate=True)


class PowerOff(task.Task):
    """The task to power off a VM."""

    def __init__(self, adapter, host_uuid, lpar_uuid, instance):
        """Creates the Task to power off an LPAR.

        :param adapter: The adapter for the pypowervm API
        :param host_uuid: The host UUID
        :param lpar_uuid: The UUID of the lpar that has media.
        :param instance: The nova instance.
        """
        super(PowerOff, self).__init__(name='pwr_off_lpar')
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.lpar_uuid = lpar_uuid
        self.instance = instance

    def execute(self):
        LOG.info(_LI('Powering off instance %s.'), self.instance.name)
        vm.power_off(self.adapter, self.instance, self.host_uuid,
                     add_parms=dict(immediate='true'))


class Delete(task.Task):
    """The task to delete the instance from the system."""

    def __init__(self, adapter, lpar_uuid, instance):
        """Create the Task to delete the VM from the system.

        :param adapter: The adapter for the pypowervm API.
        :param lpar_uuid: The VM's PowerVM UUID.
        :param instance: The nova instance.
        """
        super(Delete, self).__init__(name='dlt_lpar')
        self.adapter = adapter
        self.lpar_uuid = lpar_uuid
        self.instance = instance

    def execute(self):
        LOG.info(_LI('Deleting instance %s from system.'), self.instance.name)
        vm.dlt_lpar(self.adapter, self.lpar_uuid)
