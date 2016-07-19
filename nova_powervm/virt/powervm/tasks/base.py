# Copyright 2016 IBM Corp.
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
from oslo_log import log as logging
import six
from taskflow import task
import time

from nova_powervm.virt.powervm.i18n import _LI
from nova_powervm.virt.powervm.i18n import _LW

LOG = logging.getLogger(__name__)


@six.add_metaclass(abc.ABCMeta)
class PowerVMTask(task.Task):
    """Provides a base TaskFlow class for PowerVM tasks.

    This provides additional logging to indicate how long a given task has
    taken for an instance.
    """

    def __init__(self, instance, name, **kwargs):
        self.instance = instance
        self.name = name

        super(PowerVMTask, self).__init__(name=name, **kwargs)

    def execute(self, *args, **kwargs):
        LOG.info(_LI('Running task %(task)s for instance %(inst)s'),
                 {'task': self.name, 'inst': self.instance.name},
                 instance=self.instance)
        start_time = time.time()

        ret = self.execute_impl(*args, **kwargs)

        run_time = time.time() - start_time
        LOG.info(_LI('Task %(task)s completed in %(seconds)d seconds for '
                     'instance %(inst)s'),
                 {'task': self.name, 'inst': self.instance.name,
                  'seconds': run_time}, instance=self.instance)
        return ret

    def execute_impl(self, **kwargs):
        """Execute the task.  Follows the TaskFlow execute signature."""

    def revert(self, *args, **kwargs):
        LOG.info(_LW('Reverting task %(task)s for instance %(inst)s'),
                 {'task': self.name, 'inst': self.instance.name},
                 instance=self.instance)
        start_time = time.time()

        ret = self.revert_impl(*args, **kwargs)

        run_time = time.time() - start_time
        LOG.info(_LW('Revert task %(task)s completed in %(seconds)d seconds '
                     'for instance %(inst)s'),
                 {'task': self.name, 'inst': self.instance.name,
                  'seconds': run_time}, instance=self.instance)
        return ret

    def revert_impl(self, result, flow_failures, **kwargs):
        """(Optional) Revert the task.  Follows TaskFlow revert signature."""
