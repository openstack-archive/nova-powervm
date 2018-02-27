# Copyright 2016, 2018 IBM Corp.
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

import mock

from nova import test

from nova_powervm.virt.powervm.tasks import slot


class TestSaveSlotStore(test.NoDBTestCase):

    def setUp(self):
        super(TestSaveSlotStore, self).setUp()

    def test_execute(self):
        slot_mgr = mock.Mock()
        save = slot.SaveSlotStore(mock.MagicMock(), slot_mgr)
        save.execute()
        slot_mgr.save.assert_called_once_with()

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            slot.SaveSlotStore(mock.MagicMock(), slot_mgr)
        tf.assert_called_once_with(name='save_slot_store')


class TestDeleteSlotStore(test.NoDBTestCase):

    def setUp(self):
        super(TestDeleteSlotStore, self).setUp()

    def test_execute(self):
        slot_mgr = mock.Mock()
        delete = slot.DeleteSlotStore(mock.MagicMock(), slot_mgr)
        delete.execute()
        slot_mgr.delete.assert_called_once_with()

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            slot.DeleteSlotStore(mock.MagicMock(), slot_mgr)
        tf.assert_called_once_with(name='delete_slot_store')
