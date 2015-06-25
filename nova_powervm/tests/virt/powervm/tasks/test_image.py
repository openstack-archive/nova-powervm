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

from nova import test

from nova_powervm.virt.powervm.tasks import image as tsk_img


class TestImage(test.TestCase):
    def test_update_task_state(self):
        def func(task_state, expected_state):
            self.assertEqual('task_state', task_state)
            self.assertIsNone(expected_state)
        tf = tsk_img.UpdateTaskState(func, 'task_state')
        self.assertEqual('update_task_state_task_state', tf.name)
        tf.execute()

        def func2(task_state, expected_state):
            self.assertEqual('task_state', task_state)
            self.assertEqual('expected_state', expected_state)
        tf = tsk_img.UpdateTaskState(func2, 'task_state',
                                     expected_state='expected_state')
        tf.execute()
