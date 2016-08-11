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

from nova.compute import task_states
from nova import exception
from nova import test

from nova_powervm.virt.powervm.tasks import vm as tf_vm

from pypowervm import const as pvmc
from taskflow import engines as tf_eng
from taskflow.patterns import linear_flow as tf_lf


class TestVMTasks(test.TestCase):
    def setUp(self):
        super(TestVMTasks, self).setUp()
        self.apt = mock.Mock()
        self.instance = mock.Mock()

    @mock.patch('pypowervm.utils.transaction.FeedTask')
    @mock.patch('pypowervm.tasks.partition.build_active_vio_feed_task')
    @mock.patch('pypowervm.tasks.storage.add_lpar_storage_scrub_tasks')
    @mock.patch('nova_powervm.virt.powervm.vm.crt_lpar')
    def test_create(self, mock_vm_crt, mock_stg, mock_bld, mock_ftsk):
        nvram_mgr = mock.Mock()
        nvram_mgr.fetch.return_value = 'data'
        lpar_entry = mock.Mock()

        # Test create with normal (non-recreate) ftsk
        crt = tf_vm.Create(self.apt, 'host_wrapper', self.instance,
                           'flavor', mock_ftsk, nvram_mgr=nvram_mgr,
                           slot_mgr='slot_mgr')
        mock_vm_crt.return_value = lpar_entry
        crt_entry = crt.execute()

        mock_ftsk.execute.assert_not_called()
        mock_vm_crt.assert_called_once_with(self.apt, 'host_wrapper',
                                            self.instance, 'flavor',
                                            nvram='data', slot_mgr='slot_mgr')
        self.assertEqual(lpar_entry, crt_entry)
        nvram_mgr.fetch.assert_called_once_with(self.instance)

        mock_ftsk.name = 'create_scrubber'
        mock_bld.return_value = mock_ftsk
        # Test create with recreate ftsk
        rcrt = tf_vm.Create(self.apt, 'host_wrapper', self.instance,
                            'flavor', None, nvram_mgr=nvram_mgr,
                            slot_mgr='slot_mgr')
        mock_bld.assert_called_once_with(
            self.apt, name='create_scrubber',
            xag={pvmc.XAG.VIO_SMAP, pvmc.XAG.VIO_FMAP})
        rcrt.execute()
        mock_ftsk.execute.assert_called_once_with()

    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('nova_powervm.virt.powervm.tasks.vm.Create.execute')
    @mock.patch('nova_powervm.virt.powervm.vm.dlt_lpar')
    def test_create_revert(self, mock_vm_dlt, mock_crt_exc,
                           mock_get_pvm_uuid):

        mock_crt_exc.side_effect = exception.NovaException()
        crt = tf_vm.Create(self.apt, 'host_wrapper', self.instance,
                           'flavor', 'stg_ftsk', None)

        # Assert that a failure while building does not revert
        crt.instance.task_state = task_states.SPAWNING
        flow_test = tf_lf.Flow("test_revert")
        flow_test.add(crt)
        self.assertRaises(exception.NovaException, tf_eng.run, flow_test)
        mock_vm_dlt.assert_not_called()

        # Assert that a failure when rebuild results in revert
        crt.instance.task_state = task_states.REBUILD_SPAWNING
        flow_test = tf_lf.Flow("test_revert")
        flow_test.add(crt)
        self.assertRaises(exception.NovaException, tf_eng.run, flow_test)
        mock_vm_dlt.assert_called()

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
        rename = tf_vm.Rename(self.apt, self.instance, 'new_name')
        new_entry = rename.execute()
        mock_vm_rename.assert_called_once_with(
            self.apt, self.instance, 'new_name')
        self.assertEqual('new_entry', new_entry)

    def test_store_nvram(self):
        nvram_mgr = mock.Mock()
        store_nvram = tf_vm.StoreNvram(nvram_mgr, self.instance,
                                       immediate=True)
        store_nvram.execute()
        nvram_mgr.store.assert_called_once_with(self.instance,
                                                immediate=True)

        # No exception is raised if the NVRAM could not be stored.
        nvram_mgr.reset_mock()
        nvram_mgr.store.side_effect = ValueError('Not Available')
        store_nvram.execute()
        nvram_mgr.store.assert_called_once_with(self.instance,
                                                immediate=True)

    def test_delete_nvram(self):
        nvram_mgr = mock.Mock()
        delete_nvram = tf_vm.DeleteNvram(nvram_mgr, self.instance)
        delete_nvram.execute()
        nvram_mgr.remove.assert_called_once_with(self.instance)

        # No exception is raised if the NVRAM could not be stored.
        nvram_mgr.reset_mock()
        nvram_mgr.remove.side_effect = ValueError('Not Available')
        delete_nvram.execute()
        nvram_mgr.remove.assert_called_once_with(self.instance)
