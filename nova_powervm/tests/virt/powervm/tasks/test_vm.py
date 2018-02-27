# Copyright 2015, 2018 IBM Corp.
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
from taskflow import task as tf_tsk


class TestVMTasks(test.NoDBTestCase):
    def setUp(self):
        super(TestVMTasks, self).setUp()
        self.apt = mock.Mock()
        self.instance = mock.Mock(uuid='fake-uuid')

    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper',
                autospec=True)
    def test_get(self, mock_inst_wrap):
        get = tf_vm.Get(self.apt, 'host_uuid', self.instance)
        get.execute()
        mock_inst_wrap.assert_called_once_with(self.apt, self.instance)

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_vm.Get(self.apt, 'host_uuid', self.instance)
        tf.assert_called_once_with(name='get_vm', provides='lpar_wrap')

    @mock.patch('pypowervm.utils.transaction.FeedTask', autospec=True)
    @mock.patch('pypowervm.tasks.partition.build_active_vio_feed_task',
                autospec=True)
    @mock.patch('pypowervm.tasks.storage.add_lpar_storage_scrub_tasks',
                autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.create_lpar', autospec=True)
    def test_create(self, mock_vm_crt, mock_stg, mock_bld, mock_ftsk):
        nvram_mgr = mock.Mock()
        nvram_mgr.fetch.return_value = 'data'
        mock_ftsk.name = 'vio_feed_task'
        lpar_entry = mock.Mock()

        # Test create with normal (non-recreate) ftsk
        crt = tf_vm.Create(self.apt, 'host_wrapper', self.instance,
                           stg_ftsk=mock_ftsk, nvram_mgr=nvram_mgr,
                           slot_mgr='slot_mgr')
        mock_vm_crt.return_value = lpar_entry
        crt_entry = crt.execute()

        mock_ftsk.execute.assert_not_called()
        mock_vm_crt.assert_called_once_with(
            self.apt, 'host_wrapper', self.instance, nvram='data',
            slot_mgr='slot_mgr')
        self.assertEqual(lpar_entry, crt_entry)
        nvram_mgr.fetch.assert_called_once_with(self.instance)

        mock_ftsk.name = 'create_scrubber'
        mock_bld.return_value = mock_ftsk
        # Test create with recreate ftsk
        rcrt = tf_vm.Create(self.apt, 'host_wrapper', self.instance,
                            stg_ftsk=None, nvram_mgr=nvram_mgr,
                            slot_mgr='slot_mgr')
        mock_bld.assert_called_once_with(
            self.apt, name='create_scrubber',
            xag={pvmc.XAG.VIO_SMAP, pvmc.XAG.VIO_FMAP})
        rcrt.execute()
        mock_ftsk.execute.assert_called_once_with()

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_vm.Create(self.apt, 'host_wrapper', self.instance)
        tf.assert_called_once_with(name='crt_vm', provides='lpar_wrap')

    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.tasks.vm.Create.execute',
                autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.delete_lpar', autospec=True)
    def test_create_revert(self, mock_vm_dlt, mock_crt_exc,
                           mock_get_pvm_uuid):

        mock_crt_exc.side_effect = exception.NovaException()
        crt = tf_vm.Create(self.apt, 'host_wrapper', self.instance, 'stg_ftsk',
                           None)

        # Assert that a failure while building does not revert
        crt.instance.task_state = task_states.SPAWNING
        flow_test = tf_lf.Flow("test_revert")
        flow_test.add(crt)
        self.assertRaises(exception.NovaException, tf_eng.run, flow_test)
        self.assertEqual(0, mock_vm_dlt.call_count)

        # Assert that a failure when rebuild results in revert
        crt.instance.task_state = task_states.REBUILD_SPAWNING
        flow_test = tf_lf.Flow("test_revert")
        flow_test.add(crt)
        self.assertRaises(exception.NovaException, tf_eng.run, flow_test)
        self.assertEqual(1, mock_vm_dlt.call_count)

    @mock.patch('nova_powervm.virt.powervm.vm.power_on', autospec=True)
    def test_power_on(self, mock_pwron):
        pwron = tf_vm.PowerOn(self.apt, self.instance, pwr_opts='opt')
        pwron.execute()
        mock_pwron.assert_called_once_with(self.apt, self.instance, opts='opt')

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_vm.PowerOn(self.apt, self.instance)
        tf.assert_called_once_with(name='pwr_vm')

    @mock.patch('nova_powervm.virt.powervm.vm.power_on', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.power_off', autospec=True)
    def test_power_on_revert(self, mock_pwroff, mock_pwron):
        flow = tf_lf.Flow('revert_power_on')
        pwron = tf_vm.PowerOn(self.apt, self.instance, pwr_opts='opt')
        flow.add(pwron)

        # Dummy Task that fails, triggering flow revert
        def failure(*a, **k):
            raise ValueError()
        flow.add(tf_tsk.FunctorTask(failure))

        # When PowerOn.execute doesn't fail, revert calls power_off
        self.assertRaises(ValueError, tf_eng.run, flow)
        mock_pwron.assert_called_once_with(self.apt, self.instance, opts='opt')
        mock_pwroff.assert_called_once_with(self.apt, self.instance,
                                            force_immediate=True)

        mock_pwron.reset_mock()
        mock_pwroff.reset_mock()

        # When PowerOn.execute fails, revert doesn't call power_off
        mock_pwron.side_effect = exception.NovaException()
        self.assertRaises(exception.NovaException, tf_eng.run, flow)
        mock_pwron.assert_called_once_with(self.apt, self.instance, opts='opt')
        self.assertEqual(0, mock_pwroff.call_count)

    @mock.patch('nova_powervm.virt.powervm.vm.power_off', autospec=True)
    def test_power_off(self, mock_pwroff):
        # Default force_immediate
        pwroff = tf_vm.PowerOff(self.apt, self.instance)
        pwroff.execute()
        mock_pwroff.assert_called_once_with(self.apt, self.instance,
                                            force_immediate=False)

        mock_pwroff.reset_mock()

        # Explicit force_immediate
        pwroff = tf_vm.PowerOff(self.apt, self.instance, force_immediate=True)
        pwroff.execute()
        mock_pwroff.assert_called_once_with(self.apt, self.instance,
                                            force_immediate=True)

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_vm.PowerOff(self.apt, self.instance)
        tf.assert_called_once_with(name='pwr_off_vm')

    @mock.patch('nova_powervm.virt.powervm.vm.delete_lpar', autospec=True)
    def test_delete(self, mock_dlt):
        delete = tf_vm.Delete(self.apt, self.instance)
        delete.execute()
        mock_dlt.assert_called_once_with(self.apt, self.instance)

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_vm.Delete(self.apt, self.instance)
        tf.assert_called_once_with(name='dlt_vm')

    @mock.patch('nova_powervm.virt.powervm.vm.update', autospec=True)
    def test_resize(self, mock_vm_update):

        resize = tf_vm.Resize(self.apt, 'host_wrapper', self.instance,
                              name='new_name')
        mock_vm_update.return_value = 'resized_entry'
        resized_entry = resize.execute()
        mock_vm_update.assert_called_once_with(
            self.apt, 'host_wrapper', self.instance, entry=None,
            name='new_name')
        self.assertEqual('resized_entry', resized_entry)

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_vm.Resize(self.apt, 'host_wrapper', self.instance)
        tf.assert_called_once_with(name='resize_vm', provides='lpar_wrap')

    @mock.patch('nova_powervm.virt.powervm.vm.rename', autospec=True)
    def test_rename(self, mock_vm_rename):
        mock_vm_rename.return_value = 'new_entry'
        rename = tf_vm.Rename(self.apt, self.instance, 'new_name')
        new_entry = rename.execute()
        mock_vm_rename.assert_called_once_with(
            self.apt, self.instance, 'new_name')
        self.assertEqual('new_entry', new_entry)

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_vm.Rename(self.apt, self.instance, 'new_name')
        tf.assert_called_once_with(
            name='rename_vm_new_name', provides='lpar_wrap')

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

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_vm.StoreNvram(nvram_mgr, self.instance)
        tf.assert_called_once_with(name='store_nvram')

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

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_vm.DeleteNvram(nvram_mgr, self.instance)
        tf.assert_called_once_with(name='delete_nvram')

    @mock.patch('nova_powervm.virt.powervm.vm.update_ibmi_settings',
                autospec=True)
    def test_update_ibmi_settings(self, mock_update):
        update = tf_vm.UpdateIBMiSettings(self.apt, self.instance, 'boot_type')
        update.execute()
        mock_update.assert_called_once_with(self.apt, self.instance,
                                            'boot_type')

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_vm.UpdateIBMiSettings(self.apt, self.instance, 'boot_type')
        tf.assert_called_once_with(name='update_ibmi_settings')
