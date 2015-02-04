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
from nova_powervm.virt.powervm import driver
from nova_powervm.virt.powervm import host as pvm_host

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

        self.myentry = entries[0]
        self.wrapper = msentry_wrapper.ManagedSystem(self.myentry)

    def test_driver_create(self):
        """Validates that a driver of the PowerVM type can just be
        initialized.
        """
        test_drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        self.assertIsNotNone(test_drv)

    @mock.patch('pypowervm.adapter.Session')
    @mock.patch('pypowervm.adapter.Adapter')
    @mock.patch('nova_powervm.virt.powervm.host.find_entry_by_mtm_serial')
    @mock.patch('nova_powervm.virt.powervm.localdisk.LocalStorage')
    def test_driver_init(self, mock_disk, mock_find, mock_apt, mock_sess):
        """Validates the PowerVM driver can be initialized for the host."""
        drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        drv.init_host('FakeHost')
        # Nothing to really check here specific to the host.
        self.assertIsNotNone(drv)

    @mock.patch('pypowervm.adapter.Session')
    @mock.patch('pypowervm.adapter.Adapter')
    @mock.patch('nova_powervm.virt.powervm.host.find_entry_by_mtm_serial')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('nova.context.get_admin_context')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('nova_powervm.virt.powervm.localdisk.LocalStorage')
    def test_driver_ops(self, mock_disk, mock_get_flv, mock_get_ctx,
                        mock_getuuid, mock_find, mock_apt, mock_sess):
        """Validates the PowerVM driver operations."""
        drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        drv.init_host('FakeHost')
        drv.adapter = mock_apt

        # get_info()
        inst = fake_instance.fake_instance_obj(mock.sentinel.ctx)
        mock_getuuid.return_value = '1234'
        info = drv.get_info(inst)
        self.assertEqual(info.id, '1234')

        # list_instances()
        tgt_mock = 'nova_powervm.virt.powervm.vm.get_lpar_list'
        with mock.patch(tgt_mock) as mock_get_list:
            fake_lpar_list = ['1', '2']
            mock_get_list.return_value = fake_lpar_list
            inst_list = drv.list_instances()
            self.assertEqual(fake_lpar_list, inst_list)

    @mock.patch('pypowervm.adapter.Session')
    @mock.patch('pypowervm.adapter.Adapter')
    @mock.patch('nova_powervm.virt.powervm.host.find_entry_by_mtm_serial')
    @mock.patch('nova_powervm.virt.powervm.vm.crt_lpar')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('nova_powervm.virt.powervm.localdisk.LocalStorage')
    @mock.patch('pypowervm.jobs.power.power_on')
    def test_spawn_ops(self, mock_pwron, mock_disk, mock_get_flv, mock_cfg_drv,
                       mock_crt, mock_find, mock_apt, mock_sess):

        """Validates the PowerVM driver operations."""
        drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        drv.init_host('FakeHost')
        drv.adapter = mock_apt

        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        my_flavor = inst.get_flavor()
        mock_get_flv.return_value = my_flavor
        mock_crt.return_value = mock.MagicMock()
        mock_cfg_drv.return_value = False

        # Invoke the method.
        drv.spawn('context', inst, mock.Mock(),
                  'injected_files', 'admin_password')

        # Create LPAR was called
        mock_crt.assert_called_with(mock_apt, drv.host_uuid,
                                    inst, my_flavor)
        # Power on was called
        self.assertTrue(mock_pwron.called)

    @mock.patch('pypowervm.adapter.Session')
    @mock.patch('pypowervm.adapter.Adapter')
    @mock.patch('nova_powervm.virt.powervm.host.find_entry_by_mtm_serial')
    @mock.patch('nova_powervm.virt.powervm.vm.crt_lpar')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                'create_cfg_drv_vopt')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('nova_powervm.virt.powervm.vios.add_vscsi_mapping')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('nova_powervm.virt.powervm.localdisk.LocalStorage')
    @mock.patch('pypowervm.jobs.power.power_on')
    def test_spawn_with_cfg(self, mock_pwron, mock_disk, mock_get_flv,
                            mock_cfg_drv, mock_val_vopt, mock_vios_vscsi,
                            mock_cfg_vopt, mock_crt, mock_find,
                            mock_apt, mock_sess):

        """Validates the PowerVM spawn w/ config drive operations."""
        drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        drv.init_host('FakeHost')
        drv.adapter = mock_apt

        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        my_flavor = inst.get_flavor()
        mock_get_flv.return_value = my_flavor
        mock_crt.return_value = mock.MagicMock()
        mock_cfg_drv.return_value = True
        mock_cfg_vopt.return_value = mock.MagicMock()
        mock_val_vopt.return_value = mock.MagicMock()

        # Invoke the method.
        drv.spawn('context', inst, mock.Mock(),
                  'injected_files', 'admin_password')

        # Create LPAR was called
        mock_crt.assert_called_with(mock_apt, drv.host_uuid,
                                    inst, my_flavor)
        # Power on was called
        self.assertTrue(mock_pwron.called)

    @mock.patch('pypowervm.adapter.Session')
    @mock.patch('pypowervm.adapter.Adapter')
    @mock.patch('nova_powervm.virt.powervm.host.find_entry_by_mtm_serial')
    @mock.patch('nova_powervm.virt.powervm.vm.crt_lpar')
    @mock.patch('nova_powervm.virt.powervm.vm.dlt_lpar')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('nova_powervm.virt.powervm.localdisk.LocalStorage')
    @mock.patch('pypowervm.jobs.power.power_on')
    @mock.patch('pypowervm.jobs.power.power_off')
    def test_spawn_ops_rollback(self, mock_pwroff, mock_pwron, mock_disk,
                                mock_get_flv, mock_cfg_drv, mock_dlt, mock_crt,
                                mock_find, mock_apt, mock_sess):
        """Validates the PowerVM driver operations.  Will do a rollback."""
        drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        drv.init_host('FakeHost')
        drv.adapter = mock_apt

        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        my_flavor = inst.get_flavor()
        mock_get_flv.return_value = my_flavor
        mock_crt.return_value = mock.MagicMock()
        mock_cfg_drv.return_value = False

        # Make sure power on fails.
        mock_pwron.side_effect = exc.Forbidden()

        # Invoke the method.
        self.assertRaises(exc.Forbidden, drv.spawn, 'context', inst,
                          mock.Mock(), 'injected_files', 'admin_password')

        # Create LPAR was called
        mock_crt.assert_called_with(mock_apt, drv.host_uuid,
                                    inst, my_flavor)
        # Power on was called
        self.assertTrue(mock_pwron.called)

        # Validate the rollbacks were called
        self.assertTrue(mock_dlt.called)

    @mock.patch('pypowervm.adapter.Session')
    @mock.patch('pypowervm.adapter.Adapter')
    @mock.patch('nova_powervm.virt.powervm.host.find_entry_by_mtm_serial')
    @mock.patch('nova_powervm.virt.powervm.vm.dlt_lpar')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                'dlt_vopt')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('nova_powervm.virt.powervm.vm.UUIDCache')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('nova_powervm.virt.powervm.localdisk.LocalStorage')
    @mock.patch('pypowervm.jobs.power.power_off')
    def test_destroy(self, mock_pwroff, mock_disk, mock_get_flv,
                     mock_uuidcache, mock_val_vopt, mock_dlt_vopt, mock_dlt,
                     mock_find, mock_apt, mock_sess):

        """Validates the basic PowerVM destroy."""
        drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        drv.init_host('FakeHost')
        drv.adapter = mock_apt

        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        inst.task_state = None
        mock_get_flv.return_value = inst.get_flavor()

        # Invoke the method.
        drv.destroy('context', inst, mock.Mock())

        # Power off was called
        self.assertTrue(mock_pwroff.called)

        # Validate that the storage delete was called
        self.assertTrue(mock_dlt.called)

        # Validate that the vopt delete was called
        self.assertTrue(mock_dlt_vopt.called)

        # Delete LPAR was called
        mock_dlt.assert_called_with(mock_apt, mock.ANY)

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
        stats = pvm_host.build_host_resource_from_entry(self.wrapper)
        self.assertIsNotNone(stats)

        # Check for the presence of fields
        fields = (('vcpus', 500), ('vcpus_used', 0),
                  ('memory_mb', 5242880), ('memory_mb_used', 128),
                  'local_gb', 'local_gb_used', 'hypervisor_type',
                  'hypervisor_version', 'hypervisor_hostname', 'cpu_info',
                  'supported_instances', 'stats')
        for fld in fields:
            if isinstance(fld, tuple):
                value = stats.get(fld[0], None)
                self.assertEqual(value, fld[1])
            else:
                value = stats.get(fld, None)
                self.assertIsNotNone(value)
        # Check for individual stats
        hstats = (('proc_units', '500.00'), ('proc_units_used', '0.00'))
        for stat in hstats:
            if isinstance(stat, tuple):
                value = stats['stats'].get(stat[0], None)
                self.assertEqual(value, stat[1])
            else:
                value = stats['stats'].get(stat, None)
                self.assertIsNotNone(value)
