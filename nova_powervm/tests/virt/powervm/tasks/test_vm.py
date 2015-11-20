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

import mock

from nova import test

from nova_powervm.virt.powervm.tasks import vm as tf_vm


class TestVMTasks(test.TestCase):
    def setUp(self):
        super(TestVMTasks, self).setUp()
        self.apt = mock.Mock()
        self.instance = mock.Mock()

    @mock.patch('nova_powervm.virt.powervm.vm.update')
    def test_resize(self, mock_vm_update):

        resize = tf_vm.Resize(self.apt, 'host_wrapper', self.instance,
                              'flavor', name='new_name')
        mock_vm_update.return_value = 'resized_entry'
        resized_entry = resize.execute()
        mock_vm_update.assert_called_once_with(self.apt, 'host_wrapper',
                                               self.instance, 'flavor',
                                               entry=None, name='new_name')
        self.assertEqual('resized_entry', resized_entry)

    @mock.patch('nova_powervm.virt.powervm.vm.rename')
    def test_rename(self, mock_vm_rename):
        mock_vm_rename.return_value = 'new_entry'
        rename = tf_vm.Rename(self.apt, 'host_uuid', self.instance, 'new_name')
        new_entry = rename.execute()
        mock_vm_rename.assert_called_once_with(self.apt, 'host_uuid',
                                               self.instance, 'new_name')
        self.assertEqual('new_entry', new_entry)
