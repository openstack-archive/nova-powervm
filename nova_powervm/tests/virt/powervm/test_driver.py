# Copyright 2014, 2015 IBM Corp.
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
#

import logging

import mock

from nova import exception as exc
from nova import objects
from nova import test
from nova.tests.unit import fake_instance
from nova.virt import fake
from pypowervm.tests.wrappers.util import pvmhttp
from pypowervm.wrappers import constants as wpr_consts
import pypowervm.wrappers.managed_system as msentry_wrapper

from nova_powervm.tests.virt import powervm
from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm import driver

MS_HTTPRESP_FILE = "managedsystem.txt"
MS_NAME = 'HV4'

LOG = logging.getLogger(__name__)
logging.basicConfig()


class TestPowerVMDriver(test.TestCase):
    def setUp(self):
        super(TestPowerVMDriver, self).setUp()

        ms_http = pvmhttp.load_pvm_resp(MS_HTTPRESP_FILE)
        self.assertNotEqual(ms_http, None,
                            "Could not load %s " %
                            MS_HTTPRESP_FILE)

        entries = ms_http.response.feed.findentries(
            wpr_consts.SYSTEM_NAME, MS_NAME)

        self.assertNotEqual(entries, None,
                            "Could not find %s in %s" %
                            (MS_NAME, MS_HTTPRESP_FILE))

        self.ms_entry = entries[0]
        self.wrapper = msentry_wrapper.ManagedSystem(self.ms_entry)

        self.drv_fix = self.useFixture(fx.PowerVMComputeDriver())
        self.drv = self.drv_fix.drv
        self.apt = self.drv_fix.pypvm.apt

    def test_driver_create(self):
        """Validates that a driver of the PowerVM type can just be
        initialized.
        """
        test_drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        self.assertIsNotNone(test_drv)

    @mock.patch('pypowervm.wrappers.managed_system.find_entry_by_mtms')
    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage')
    def test_driver_init(self, mock_disk, mock_find):
        """Validates the PowerVM driver can be initialized for the host."""
        drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        drv.init_host('FakeHost')
        # Nothing to really check here specific to the host.
        self.assertIsNotNone(drv)

    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('nova.context.get_admin_context')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    def test_driver_ops(self, mock_get_flv, mock_get_ctx, mock_getuuid):
        """Validates the PowerVM driver operations."""
        # get_info()
        inst = fake_instance.fake_instance_obj(mock.sentinel.ctx)
        mock_getuuid.return_value = '1234'
        info = self.drv.get_info(inst)
        self.assertEqual(info.id, '1234')

        # list_instances()
        tgt_mock = 'nova_powervm.virt.powervm.vm.get_lpar_list'
        with mock.patch(tgt_mock) as mock_get_list:
            fake_lpar_list = ['1', '2']
            mock_get_list.return_value = fake_lpar_list
            inst_list = self.drv.list_instances()
            self.assertEqual(fake_lpar_list, inst_list)

    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver._plug_vifs')
    @mock.patch('nova_powervm.virt.powervm.vm.crt_lpar')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('pypowervm.jobs.power.power_on')
    def test_spawn_ops(self, mock_pwron, mock_get_flv, mock_cfg_drv,
                       mock_crt, mock_plug_vifs):

        """Validates the PowerVM driver operations."""
        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        my_flavor = inst.get_flavor()
        mock_get_flv.return_value = my_flavor
        mock_crt.return_value = mock.MagicMock()
        mock_cfg_drv.return_value = False

        # Invoke the method.
        self.drv.spawn('context', inst, mock.Mock(),
                       'injected_files', 'admin_password')

        # Create LPAR was called
        mock_crt.assert_called_with(
            self.apt, self.drv.host_uuid, inst, my_flavor)
        # Power on was called
        self.assertTrue(mock_pwron.called)

    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver._plug_vifs')
    @mock.patch('nova_powervm.virt.powervm.vm.crt_lpar')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                'create_cfg_drv_vopt')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('nova_powervm.virt.powervm.vios.add_vscsi_mapping')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('pypowervm.jobs.power.power_on')
    def test_spawn_with_cfg(self, mock_pwron, mock_get_flv,
                            mock_cfg_drv, mock_val_vopt, mock_vios_vscsi,
                            mock_cfg_vopt, mock_crt, mock_plug_vifs):

        """Validates the PowerVM spawn w/ config drive operations."""
        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        my_flavor = inst.get_flavor()
        mock_get_flv.return_value = my_flavor
        mock_cfg_drv.return_value = True

        # Invoke the method.
        self.drv.spawn('context', inst, mock.Mock(),
                       'injected_files', 'admin_password')

        # Create LPAR was called
        mock_crt.assert_called_with(self.apt, self.drv.host_uuid,
                                    inst, my_flavor)
        # Power on was called
        self.assertTrue(mock_pwron.called)

    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver._plug_vifs')
    @mock.patch('nova_powervm.virt.powervm.vm.crt_lpar')
    @mock.patch('nova_powervm.virt.powervm.vm.dlt_lpar')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('pypowervm.jobs.power.power_on')
    @mock.patch('pypowervm.jobs.power.power_off')
    def test_spawn_ops_rollback(self, mock_pwroff, mock_pwron, mock_get_flv,
                                mock_cfg_drv, mock_dlt, mock_crt,
                                mock_plug_vifs):
        """Validates the PowerVM driver operations.  Will do a rollback."""

        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        my_flavor = inst.get_flavor()
        mock_get_flv.return_value = my_flavor
        mock_cfg_drv.return_value = False

        # Make sure power on fails.
        mock_pwron.side_effect = exc.Forbidden()

        # Invoke the method.
        self.assertRaises(exc.Forbidden, self.drv.spawn, 'context', inst,
                          mock.Mock(), 'injected_files', 'admin_password')

        # Create LPAR was called
        mock_crt.assert_called_with(self.apt, self.drv.host_uuid,
                                    inst, my_flavor)
        # Power on was called
        self.assertTrue(mock_pwron.called)

        # Validate the rollbacks were called
        self.assertTrue(mock_dlt.called)

    @mock.patch('nova_powervm.virt.powervm.vm.dlt_lpar')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                'dlt_vopt')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('pypowervm.jobs.power.power_off')
    def test_destroy(self, mock_pwroff, mock_get_flv,
                     mock_pvmuuid, mock_val_vopt, mock_dlt_vopt, mock_dlt):

        """Validates the basic PowerVM destroy."""
        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        inst.task_state = None
        mock_get_flv.return_value = inst.get_flavor()

        # Invoke the method.
        self.drv.destroy('context', inst, mock.Mock())

        # Power off was called
        self.assertTrue(mock_pwroff.called)

        # Validate that the storage delete was called
        self.assertTrue(mock_dlt.called)

        # Validate that the vopt delete was called
        self.assertTrue(mock_dlt_vopt.called)

        # Delete LPAR was called
        mock_dlt.assert_called_with(self.apt, mock.ANY)

    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('nova_powervm.virt.powervm.vm.power_off')
    @mock.patch('nova_powervm.virt.powervm.vm.update')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    def test_resize(
        self, mock_get_flv, mock_update, mock_pwr_off, mock_get_uuid):
        """Validates the PowerVM driver resize operation."""
        # Set up the mocks to the resize operation.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        host = self.drv.get_host_ip_addr()

        # Catch root disk resize smaller.
        small_root = objects.Flavor(vcpus=1, memory_mb=2048, root_gb=9)
        self.assertRaises(
            exc.InstanceFaultRollback, self.drv.migrate_disk_and_power_off,
            'context', inst, 'dest', small_root, 'network_info')

        new_flav = objects.Flavor(vcpus=1, memory_mb=2048, root_gb=10)

        # We don't support resize to different host.
        self.assertRaises(
            NotImplementedError, self.drv.migrate_disk_and_power_off,
            'context', inst, 'bogus host', new_flav, 'network_info')

        self.drv.migrate_disk_and_power_off(
            'context', inst, host, new_flav, 'network_info')
        mock_pwr_off.assert_called_with(
            self.drv.adapter, inst, self.drv.host_uuid, entry=mock.ANY)
        mock_update.assert_called_with(
            self.drv.adapter, self.drv.host_uuid, inst, new_flav,
            entry=mock.ANY)

        # Boot disk resize
        boot_flav = objects.Flavor(vcpus=1, memory_mb=2048, root_gb=12)
        self.drv.migrate_disk_and_power_off(
            'context', inst, host, boot_flav, 'network_info')
        self.drv.block_dvr.extend_volume.assert_called_with(
            'context', inst, dict(type='boot'), 12)

    @mock.patch('nova_powervm.virt.powervm.driver.LOG')
    def test_log_op(self, mock_log):
        """Validates the log_operations."""
        drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        drv._log_operation('fake_op', inst)
        entry = ('Operation: fake_op. Virtual machine display '
                 'name: Fake Instance, name: instance-00000001, '
                 'UUID: 49629a5c-f4c4-4721-9511-9725786ff2e5')
        mock_log.info.assert_called_with(entry)

    def test_host_resources(self):
        # Set the return value of None so we use the cached value in the drv
        self.apt.read.return_value = None
        self.drv.host_wrapper = self.wrapper

        stats = self.drv.get_available_resource('nodename')
        self.assertIsNotNone(stats)

        # Check for the presence of fields added to host stats
        fields = ('local_gb', 'local_gb_used')

        for fld in fields:
            value = stats.get(fld, None)
            self.assertIsNotNone(value)

    @mock.patch('nova_powervm.virt.powervm.vm.crt_vif')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_plug_vifs(
        self, mock_vm_get, mock_vm_crt):
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the CNA response
        cnas = [mock.MagicMock(), mock.MagicMock()]
        cnas[0].mac = 'AABBCCDDEEFF'
        cnas[1].mac = 'AABBCCDDEE11'
        mock_vm_get.return_value = cnas

        # Mock up the network info.  They get sanitized to upper case.
        net_info = [
            {'address': 'aabbccddeeff'},
            {'address': 'aabbccddee22'}
        ]

        # Run method
        self.drv.plug_vifs(inst, net_info)

        # The create should have only been called once.  The other was already
        # existing.
        self.assertEqual(1, mock_vm_crt.call_count)
