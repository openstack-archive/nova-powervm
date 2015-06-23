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

from oslo_log import log as logging
from taskflow import task

LOG = logging.getLogger(__name__)


class UpdateTaskState(task.Task):
    def __init__(self, update_task_state, task_state, expected_state=None):
        """Invoke the update_task_state callback with the desired arguments.

        :param update_task_state: update_task_state callable passed into
                                  snapshot.
        :param task_state: The new task state (from nova.compute.task_states)
                           to set.
        :param expected_state: Optional.  The expected state of the task prior
                               to this request.
        """
        self.update_task_state = update_task_state
        self.task_state = task_state
        self.expected_state = expected_state
        super(UpdateTaskState, self).__init__(
            name='update_task_state_%s' % task_state)

    def execute(self):
        self.update_task_state(task_state=self.task_state,
                               expected_state=self.expected_state)
