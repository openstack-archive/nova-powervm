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
from oslo_config import cfg

from nova import exception as exc
from nova import objects
from nova import test
from nova.tests.unit import fake_instance
from nova.virt import fake
import pypowervm.adapter as pvm_adp
from pypowervm.tests.wrappers.util import pvmhttp
import pypowervm.wrappers.logical_partition as pvm_lpar
import pypowervm.wrappers.managed_system as pvm_ms

from nova_powervm.tests.virt import powervm
from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm import driver

MS_HTTPRESP_FILE = "managedsystem.txt"
MS_NAME = 'HV4'

LOG = logging.getLogger(__name__)
logging.basicConfig()

CONF = cfg.CONF
CONF.import_opt('my_ip', 'nova.netconf')


class FakeClass(object):
    """Used for the test_inst_dict."""
    pass


class TestPowerVMDriver(test.TestCase):
    def setUp(self):
        super(TestPowerVMDriver, self).setUp()

        ms_http = pvmhttp.load_pvm_resp(MS_HTTPRESP_FILE)
        self.assertNotEqual(ms_http, None,
                            "Could not load %s " %
                            MS_HTTPRESP_FILE)

        entries = ms_http.response.feed.findentries(pvm_ms._SYSTEM_NAME,
                                                    MS_NAME)

        self.assertNotEqual(entries, None,
                            "Could not find %s in %s" %
                            (MS_NAME, MS_HTTPRESP_FILE))

        self.ms_entry = entries[0]
        self.wrapper = pvm_ms.System.wrap(self.ms_entry)

        cfg.CONF.set_override('disk_driver', 'localdisk')
        self.drv_fix = self.useFixture(fx.PowerVMComputeDriver())
        self.drv = self.drv_fix.drv
        self.apt = self.drv_fix.pypvm.apt

        self.fc_vol_drv = self.drv.vol_drvs['fibre_channel']
        self.disk_dvr = self.drv.disk_dvr

        self.crt_lpar_p = mock.patch('nova_powervm.virt.powervm.vm.crt_lpar')
        self.crt_lpar = self.crt_lpar_p.start()

        resp = pvm_adp.Response('method', 'path', 'status', 'reason', {})
        resp.entry = pvm_lpar.LPAR._bld().entry
        self.crt_lpar.return_value = pvm_lpar.LPAR.wrap(resp)

    def tearDown(self):
        super(TestPowerVMDriver, self).tearDown()
        self.crt_lpar_p.stop()

    def test_driver_create(self):
        """Validates that a driver of the PowerVM type can just be
        initialized.
        """
        test_drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        self.assertIsNotNone(test_drv)

    def test_get_volume_connector(self):
        vol_connector = self.drv.get_volume_connector(None)
        self.assertIsNotNone(vol_connector['wwpns'])
        self.assertIsNotNone(vol_connector['host'])

    @mock.patch('pypowervm.wrappers.managed_system.find_entry_by_mtms')
    @mock.patch('nova_powervm.virt.powervm.vios.get_vios_name_map')
    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage')
    def test_driver_init(self, mock_disk, mock_vio_name_map, mock_find):
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
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('pypowervm.tasks.power.power_on')
    def test_spawn_ops(self, mock_pwron, mock_get_flv, mock_cfg_drv,
                       mock_plug_vifs):

        """Validates the PowerVM driver operations."""
        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        my_flavor = inst.get_flavor()
        mock_get_flv.return_value = my_flavor
        mock_cfg_drv.return_value = False

        # Invoke the method.
        self.drv.spawn('context', inst, mock.Mock(),
                       'injected_files', 'admin_password')

        # Create LPAR was called
        self.crt_lpar.assert_called_with(
            self.apt, self.drv.host_wrapper, inst, my_flavor)
        # Power on was called
        self.assertTrue(mock_pwron.called)

    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver._plug_vifs')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                'create_cfg_drv_vopt')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('nova_powervm.virt.powervm.vios.add_vscsi_mapping')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('pypowervm.tasks.power.power_on')
    def test_spawn_with_cfg(self, mock_pwron, mock_get_flv,
                            mock_cfg_drv, mock_val_vopt, mock_vios_vscsi,
                            mock_cfg_vopt, mock_plug_vifs):

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
        self.crt_lpar.assert_called_with(self.apt, self.drv.host_wrapper,
                                         inst, my_flavor)
        # Power on was called
        self.assertTrue(mock_pwron.called)

    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver._plug_vifs')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                'create_cfg_drv_vopt')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('nova_powervm.virt.powervm.vios.add_vscsi_mapping')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('pypowervm.tasks.power.power_on')
    def test_spawn_with_bdms(self, mock_pwron, mock_get_flv,
                             mock_cfg_drv, mock_val_vopt, mock_vios_vscsi,
                             mock_cfg_vopt, mock_plug_vifs):

        """Validates the PowerVM spawn w/ config drive operations."""
        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        my_flavor = inst.get_flavor()
        mock_get_flv.return_value = my_flavor
        mock_cfg_drv.return_value = True

        # Create some fake BDMs
        block_device_info = self._fake_bdms()

        # Invoke the method.
        self.drv.spawn('context', inst, mock.Mock(),
                       'injected_files', 'admin_password',
                       block_device_info=block_device_info)

        # Create LPAR was called
        self.crt_lpar.assert_called_with(self.apt, self.drv.host_wrapper,
                                         inst, my_flavor)
        # Power on was called
        self.assertTrue(mock_pwron.called)

        # Check that the connect volume was called
        self.assertEqual(2, self.fc_vol_drv.connect_volume.call_count)

    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver._plug_vifs')
    @mock.patch('nova_powervm.virt.powervm.vm.dlt_lpar')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('pypowervm.tasks.power.power_on')
    @mock.patch('pypowervm.tasks.power.power_off')
    def test_spawn_ops_rollback(self, mock_pwroff, mock_pwron, mock_get_flv,
                                mock_cfg_drv, mock_dlt, mock_plug_vifs):
        """Validates the PowerVM driver operations.  Will do a rollback."""

        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        my_flavor = inst.get_flavor()
        mock_get_flv.return_value = my_flavor
        mock_cfg_drv.return_value = False
        block_device_info = self._fake_bdms()

        # Make sure power on fails.
        mock_pwron.side_effect = exc.Forbidden()

        # Invoke the method.
        self.assertRaises(exc.Forbidden, self.drv.spawn, 'context', inst,
                          mock.Mock(), 'injected_files', 'admin_password',
                          block_device_info=block_device_info)

        # Create LPAR was called
        self.crt_lpar.assert_called_with(self.apt, self.drv.host_wrapper,
                                         inst, my_flavor)
        self.assertEqual(2, self.fc_vol_drv.connect_volume.call_count)

        # Power on was called
        self.assertTrue(mock_pwron.called)

        # Validate the rollbacks were called
        self.assertTrue(mock_dlt.called)
        self.assertEqual(2, self.fc_vol_drv.disconnect_volume.call_count)

    @mock.patch('nova_powervm.virt.powervm.vm.dlt_lpar')
    @mock.patch('nova_powervm.virt.powervm.vm.power_off')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                'dlt_vopt')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('nova_powervm.virt.powervm.vm.UUIDCache')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    def test_destroy(
        self, mock_get_flv, mock_cache, mock_pvmuuid, mock_inst_wrap,
        mock_val_vopt, mock_dlt_vopt, mock_pwroff, mock_dlt):

        """Validates the basic PowerVM destroy."""
        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        inst.task_state = None
        mock_get_flv.return_value = inst.get_flavor()

        singleton = mock.Mock()
        mock_cache.get_cache.return_value = singleton
        # BDMs
        mock_bdms = self._fake_bdms()

        # Invoke the method.
        self.drv.destroy('context', inst, mock.Mock(),
                         block_device_info=mock_bdms)

        # Power off was called
        self.assertTrue(mock_pwroff.called)

        # Validate that the storage delete was called
        self.assertTrue(mock_dlt.called)

        # Validate that the vopt delete was called
        self.assertTrue(mock_dlt_vopt.called)

        # Validate that the volume detach was called
        self.assertEqual(2, self.fc_vol_drv.disconnect_volume.call_count)

        # Delete LPAR was called, and removed from the cache
        mock_dlt.assert_called_with(self.apt, mock.ANY)
        singleton.remove.assert_called_with(inst.name)

    @mock.patch('nova_powervm.virt.powervm.volume.vscsi.VscsiVolumeAdapter.'
                'connect_volume')
    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper')
    def test_attach_volume(self, mock_inst_wrap, mock_conn_volume):

        """Validates the basic PowerVM destroy."""
        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        inst.task_state = None

        # BDMs
        mock_bdm = self._fake_bdms()['block_device_mapping'][0]

        # Invoke the method.
        self.drv.attach_volume('context', mock_bdm['connection_info'],
                               inst, mock.Mock())

        # Verify the connect volume was invoked
        self.assertEqual(1, mock_conn_volume.call_count)

    @mock.patch('nova_powervm.virt.powervm.volume.vscsi.VscsiVolumeAdapter.'
                'disconnect_volume')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    def test_detach_volume(self, mock_pvmuuid, mock_disconn_volume):

        """Validates the basic PowerVM destroy."""
        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        inst.task_state = None

        # BDMs
        mock_bdm = self._fake_bdms()['block_device_mapping'][0]

        # Invoke the method.
        self.drv.detach_volume(mock_bdm['connection_info'], inst, mock.Mock())

        # Verify the connect volume was invoked
        self.assertEqual(1, mock_disconn_volume.call_count)

    @mock.patch('nova_powervm.virt.powervm.vm.dlt_lpar')
    @mock.patch('nova_powervm.virt.powervm.vm.power_off')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                'dlt_vopt')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    def test_destroy_rollback(self, mock_get_flv, mock_pvmuuid, mock_inst_wrap,
                              mock_val_vopt, mock_dlt_vopt, mock_pwroff,
                              mock_dlt):

        """Validates the basic PowerVM destroy rollback mechanism works."""
        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        inst.task_state = None
        mock_get_flv.return_value = inst.get_flavor()

        # BDMs
        mock_bdms = self._fake_bdms()

        # Fire a failure in the power off.
        mock_dlt.side_effect = exc.Forbidden()

        # Invoke the method.
        self.assertRaises(exc.Forbidden, self.drv.destroy, 'context', inst,
                          mock.Mock(), block_device_info=mock_bdms)

        # Validate that the vopt delete was called
        self.assertTrue(mock_dlt_vopt.called)

        # Validate that the volume detach was called
        self.assertEqual(2, self.fc_vol_drv.disconnect_volume.call_count)

        # Delete LPAR was called
        mock_dlt.assert_called_with(self.apt, mock.ANY)

        # Validate the rollbacks were called.
        self.assertEqual(2, self.fc_vol_drv.connect_volume.call_count)

    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('nova_powervm.virt.powervm.vm.power_off')
    @mock.patch('nova_powervm.virt.powervm.vm.update')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    def test_resize(self, mock_get_flv, mock_update, mock_pwr_off,
                    mock_get_uuid):
        """Validates the PowerVM driver resize operation."""
        # Set up the mocks to the resize operation.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        host = self.drv.get_host_ip_addr()
        resp = pvm_adp.Response('method', 'path', 'status', 'reason', {})
        resp.entry = pvm_lpar.LPAR._bld().entry
        self.apt.read.return_value = resp

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
            self.drv.adapter, self.drv.host_wrapper, inst, new_flav,
            entry=mock.ANY)

        # Boot disk resize
        boot_flav = objects.Flavor(vcpus=1, memory_mb=2048, root_gb=12)
        self.drv.migrate_disk_and_power_off(
            'context', inst, host, boot_flav, 'network_info')
        self.drv.disk_dvr.extend_disk.assert_called_with(
            'context', inst, dict(type='boot'), 12)

    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('nova_powervm.virt.powervm.driver.vm')
    @mock.patch('nova_powervm.virt.powervm.tasks.vm.vm')
    @mock.patch('nova_powervm.virt.powervm.tasks.vm.power')
    def test_rescue(self, mock_task_pwr, mock_task_vm,
                    mock_dvr_vm, mock_get_flv):

        """Validates the PowerVM driver rescue operation."""
        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        self.drv.disk_dvr = mock.Mock()

        # Invoke the method.
        self.drv.rescue('context', inst, mock.MagicMock(),
                        mock.MagicMock(), 'rescue_psswd')

        self.assertTrue(mock_task_vm.power_off.called)
        self.assertTrue(self.drv.disk_dvr.create_disk_from_image.called)
        self.assertTrue(self.drv.disk_dvr.connect_disk.called)
        # TODO(IBM): Power on not called until bootmode=sms is supported
        # self.assertTrue(mock_task_pwr.power_on.called)

    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('nova_powervm.virt.powervm.driver.vm')
    @mock.patch('nova_powervm.virt.powervm.tasks.vm.vm')
    @mock.patch('nova_powervm.virt.powervm.tasks.vm.power')
    def test_unrescue(self, mock_task_pwr, mock_task_vm,
                      mock_dvr_vm, mock_get_flv):

        """Validates the PowerVM driver rescue operation."""
        # Set up the mocks to the tasks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        self.drv.disk_dvr = mock.Mock()

        # Invoke the method.
        self.drv.unrescue(inst, 'network_info')

        self.assertTrue(mock_task_vm.power_off.called)
        self.assertTrue(self.drv.disk_dvr.disconnect_image_disk.called)
        self.assertTrue(self.drv.disk_dvr.delete_disks.called)
        self.assertTrue(mock_task_pwr.power_on.called)

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

    @mock.patch('nova_powervm.virt.powervm.vm.crt_secure_rmc_vif')
    @mock.patch('nova_powervm.virt.powervm.vm.get_secure_rmc_vswitch')
    @mock.patch('nova_powervm.virt.powervm.vm.crt_vif')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_plug_vifs(self, mock_vm_get, mock_vm_crt, mock_get_rmc_vswitch,
                       mock_crt_rmc_vif):
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the CNA response
        cnas = [mock.MagicMock(), mock.MagicMock()]
        cnas[0].mac = 'AABBCCDDEEFF'
        cnas[0].vswitch_uri = 'fake_uri'
        cnas[1].mac = 'AABBCCDDEE11'
        cnas[1].vswitch_uri = 'fake_mgmt_uri'
        mock_vm_get.return_value = cnas

        # Mock up the network info.  They get sanitized to upper case.
        net_info = [
            {'address': 'aabbccddeeff'},
            {'address': 'aabbccddee22'}
        ]

        # Mock up the rmc vswitch
        vswitch_w = mock.MagicMock()
        vswitch_w.href = 'fake_mgmt_uri'
        mock_get_rmc_vswitch.return_value = vswitch_w

        # Run method
        self.drv.plug_vifs(inst, net_info)

        # The create should have only been called once.  The other was already
        # existing.
        self.assertEqual(1, mock_vm_crt.call_count)
        self.assertEqual(0, mock_crt_rmc_vif.call_count)

    @mock.patch('nova_powervm.virt.powervm.vm.crt_secure_rmc_vif')
    @mock.patch('nova_powervm.virt.powervm.vm.get_secure_rmc_vswitch')
    @mock.patch('nova_powervm.virt.powervm.vm.crt_vif')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_plug_vifs_rmc(self, mock_vm_get, mock_vm_crt,
                           mock_get_rmc_vswitch, mock_crt_rmc_vif):
        """Tests that a crt vif can be done with secure RMC."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the CNA response.  None are the 'fake_mgmt_uri', so this
        # will force a RMC to be created.
        cnas = [mock.MagicMock(), mock.MagicMock()]
        cnas[0].mac = 'AABBCCDDEEFF'
        cnas[0].vswitch_uri = 'fake_uri'
        cnas[1].mac = 'AABBCCDDEE11'
        cnas[1].vswitch_uri = 'fake_uri'
        mock_vm_get.return_value = cnas

        # Mock up the network info.  They get sanitized to upper case.
        net_info = [
            {'address': 'aabbccddeeff'},
            {'address': 'aabbccddee22'}
        ]

        # Mock up the rmc vswitch
        vswitch_w = mock.MagicMock()
        vswitch_w.href = 'fake_mgmt_uri'
        mock_get_rmc_vswitch.return_value = vswitch_w

        # Run method
        self.drv.plug_vifs(inst, net_info)

        # The create should have only been called once.  A RMC create should
        # also be invoked.
        self.assertEqual(1, mock_vm_crt.call_count)
        self.assertEqual(1, mock_crt_rmc_vif.call_count)

    def test_extract_bdm(self):
        """Tests the _extract_bdm method."""
        self.assertEqual([], self.drv._extract_bdm(None))
        self.assertEqual([], self.drv._extract_bdm({'fake': 'val'}))

        fake_bdi = {'block_device_mapping': ['content']}
        self.assertListEqual(['content'], self.drv._extract_bdm(fake_bdi))

    def test_inst_dict(self):
        """Tests the _inst_dict method."""
        class_name = 'nova_powervm.tests.virt.powervm.test_driver.FakeClass'
        inst_dict = driver._inst_dict({'test': class_name})

        self.assertEqual(1, len(inst_dict.keys()))
        self.assertIsInstance(inst_dict['test'], FakeClass)

    def test_get_host_ip_addr(self):
        self.assertEqual(self.drv.get_host_ip_addr(), CONF.my_ip)

    @mock.patch('nova_powervm.virt.powervm.driver.LOG.warn')
    @mock.patch('nova.compute.utils.get_machine_ips')
    def test_get_host_ip_addr_failure(self, mock_ips, mock_log):
        mock_ips.return_value = ['1.1.1.1']
        self.drv.get_host_ip_addr()
        mock_log.assert_called_once_with(u'my_ip address (%(my_ip)s) was '
                                         u'not found on any of the '
                                         u'interfaces: %(ifaces)s',
                                         {'ifaces': '1.1.1.1',
                                          'my_ip': mock.ANY})

    def _fake_bdms(self):
        block_device_info = {
            'block_device_mapping': [
                {
                    'connection_info': {
                        'driver_volume_type': 'fibre_channel',
                        'data': {
                            'volume_id': 'fake_vol_uuid',
                            'target_lun': 0
                        }
                    },
                    'mount_device': '/dev/vda'
                },
                {
                    'connection_info': {
                        'driver_volume_type': 'fibre_channel',
                        'data': {
                            'volume_id': 'fake_vol_uuid2',
                            'target_lun': 1
                        }
                    },
                    'mount_device': '/dev/vdb'
                }
            ]
        }
        return block_device_info
