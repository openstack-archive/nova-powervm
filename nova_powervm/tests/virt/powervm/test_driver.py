# Copyright 2014, 2016 IBM Corp.
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

from __future__ import absolute_import

import fixtures
import logging
import mock
from oslo_serialization import jsonutils

from nova import block_device as nova_block_device
from nova.compute import task_states
from nova import exception as exc
from nova import objects
from nova.objects import base as obj_base
from nova.objects import block_device as bdmobj
from nova import test
from nova.tests.unit import fake_instance
from nova.virt import block_device as nova_virt_bdm
from nova.virt import driver as virt_driver
from nova.virt import fake
from pypowervm import adapter as pvm_adp
from pypowervm import const as pvm_const
from pypowervm import exceptions as pvm_exc
from pypowervm.helpers import log_helper as log_hlp
from pypowervm.helpers import vios_busy as vio_hlp
from pypowervm.utils import transaction as pvm_tx
from pypowervm.wrappers import base_partition as pvm_bp
from pypowervm.wrappers import logical_partition as pvm_lpar
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.tests.virt import powervm
from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm import driver
from nova_powervm.virt.powervm import exception as p_exc
from nova_powervm.virt.powervm import live_migration as lpm
from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)
logging.basicConfig()


class TestPowerVMDriverInit(test.TestCase):
    """A test class specifically for the driver setup.

    Handles testing the configuration of the agent with the backing REST API.
    """

    def setUp(self):
        super(TestPowerVMDriverInit, self).setUp()

        self.flags(disk_driver='localdisk', group='powervm')
        self.flags(host='host1', my_ip='127.0.0.1')

    @mock.patch('nova_powervm.virt.powervm.driver.NovaEventHandler')
    @mock.patch('pypowervm.adapter.Adapter')
    @mock.patch('pypowervm.adapter.Session')
    def test_get_adapter(self, mock_session, mock_adapter, mock_evt_handler):
        # Set up the mocks.
        mock_evt_listener = (mock_session.return_value.get_event_listener.
                             return_value)
        mock_evt_handler.return_value = 'evt_hdlr'

        # Setup and invoke
        drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        drv._get_adapter()

        # Assert the appropriate calls
        mock_session.assert_called_once_with(conn_tries=300)
        mock_adapter.assert_called_once_with(
            mock_session.return_value,
            helpers=[log_hlp.log_helper, vio_hlp.vios_busy_retry_helper])
        mock_evt_listener.subscribe.assert_called_once_with('evt_hdlr')


class TestPowerVMDriver(test.TestCase):
    def setUp(self):
        super(TestPowerVMDriver, self).setUp()

        self.flags(disk_driver='localdisk', group='powervm')
        self.flags(host='host1', my_ip='127.0.0.1')
        self.drv_fix = self.useFixture(fx.PowerVMComputeDriver())
        self.drv = self.drv_fix.drv
        self.apt = self.drv.adapter

        self._setup_lpm()

        self.disk_dvr = self.drv.disk_dvr
        self.vol_fix = self.useFixture(fx.VolumeAdapter())
        self.vol_drv = self.vol_fix.drv

        self.crt_lpar = self.useFixture(fixtures.MockPatch(
            'nova_powervm.virt.powervm.vm.crt_lpar')).mock

        self.get_inst_wrap = self.useFixture(fixtures.MockPatch(
            'nova_powervm.virt.powervm.vm.get_instance_wrapper')).mock

        self.build_tx_feed = self.useFixture(fixtures.MockPatch(
            'nova_powervm.virt.powervm.vios.build_tx_feed_task')).mock

        self.stg_ftsk = pvm_tx.FeedTask('fake', pvm_vios.VIOS.getter(self.apt))
        self.build_tx_feed.return_value = self.stg_ftsk

        self.scrub_stg = self.useFixture(fixtures.MockPatch(
            'pypowervm.tasks.storage.add_lpar_storage_scrub_tasks')).mock

        self.san_lpar_name = self.useFixture(fixtures.MockPatch(
            'pypowervm.util.sanitize_partition_name_for_api')).mock
        self.san_lpar_name.side_effect = lambda name: name

        # Create an instance to test with
        self.inst = objects.Instance(**powervm.TEST_INST_SPAWNING)
        self.inst_ibmi = objects.Instance(**powervm.TEST_INST_SPAWNING)
        self.inst_ibmi.system_metadata = {'image_os_distro': 'ibmi'}

    def _setup_lpm(self):
        """Setup the lpm environment.

        This may have to be called directly by tests since the lpm code
        cleans up the dict entry on the last expected lpm method.
        """
        self.lpm = mock.Mock()
        self.lpm_inst = mock.Mock()
        self.lpm_inst.uuid = 'inst1'
        self.drv.live_migrations = {'inst1': self.lpm}

    def test_driver_create(self):
        """Validates that a driver of the PowerVM type can be initialized."""
        test_drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        self.assertIsNotNone(test_drv)

    def test_cleanup_host(self):
        self.drv.cleanup_host('fake_host')
        self.assertTrue(
            self.drv.session.get_event_listener.return_value.shutdown.called)

    def test_get_volume_connector(self):
        """Tests that a volume connector can be built."""
        vol_connector = self.drv.get_volume_connector(mock.Mock())
        self.assertIsNotNone(vol_connector['wwpns'])
        self.assertIsNotNone(vol_connector['host'])

    def test_get_disk_adapter(self):
        # Ensure we can handle upper case option and we instantiate the class
        self.flags(disk_driver='LoCaLDisK', group='powervm')
        self.drv.disk_dvr = None
        self.drv._get_disk_adapter()
        # The local disk driver has been mocked, so we just compare the name
        self.assertIn('LocalStorage()', str(self.drv.disk_dvr))

    @mock.patch('nova_powervm.virt.powervm.nvram.manager.NvramManager')
    @mock.patch('oslo_utils.importutils.import_object')
    @mock.patch('nova.utils.spawn')
    def test_setup_rebuild_store(self, mock_spawn, mock_import, mock_mgr):
        self.flags(nvram_store='NoNe', group='powervm')
        self.drv._setup_rebuild_store()
        self.assertFalse(mock_import.called)
        self.assertFalse(mock_mgr.called)
        self.assertFalse(mock_spawn.called)

        self.flags(nvram_store='swift', group='powervm')
        self.drv._setup_rebuild_store()
        self.assertTrue(mock_import.called)
        self.assertTrue(mock_mgr.called)
        self.assertTrue(mock_spawn.called)
        self.assertIsNotNone(self.drv.store_api)

    @mock.patch.object(vm, 'get_lpars')
    @mock.patch.object(vm, 'get_instance')
    def test_nvram_host_startup(self, mock_get_inst, mock_get_lpars):

        mock_lpar_wrapper = mock.Mock()
        mock_lpar_wrapper.uuid = 'uuid_value'
        mock_get_lpars.return_value = [mock_lpar_wrapper,
                                       mock_lpar_wrapper,
                                       mock_lpar_wrapper]
        mock_get_inst.side_effect = [powervm.TEST_INST1,
                                     None,
                                     powervm.TEST_INST2]

        self.drv.nvram_mgr = mock.Mock()
        self.drv._nvram_host_startup()
        self.drv.nvram_mgr.store.assert_has_calls(
            [mock.call(powervm.TEST_INST1), mock.call(powervm.TEST_INST2)])

    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('nova.context.get_admin_context')
    def test_driver_ops(self, mock_get_ctx, mock_getuuid):
        """Validates the PowerVM driver operations."""
        # get_info()
        inst = fake_instance.fake_instance_obj(mock.sentinel.ctx)
        mock_getuuid.return_value = '1234'
        info = self.drv.get_info(inst)
        self.assertEqual(info.id, '1234')

        # list_instances()
        tgt_mock = 'nova_powervm.virt.powervm.vm.get_lpar_names'
        with mock.patch(tgt_mock) as mock_get_list:
            fake_lpar_list = ['1', '2']
            mock_get_list.return_value = fake_lpar_list
            inst_list = self.drv.list_instances()
            self.assertEqual(fake_lpar_list, inst_list)

        # instance_exists()
        tgt_mock = 'nova_powervm.virt.powervm.vm.instance_exists'
        with mock.patch(tgt_mock) as mock_inst_exists:
            mock_inst_exists.side_effect = [True, False]
            self.assertTrue(self.drv.instance_exists(mock.Mock()))
            self.assertFalse(self.drv.instance_exists(mock.Mock()))

    def test_instance_on_disk(self):
        """Validates the instance_on_disk method."""

        @mock.patch.object(self.drv, '_is_booted_from_volume')
        @mock.patch.object(self.drv, '_get_block_device_info')
        @mock.patch.object(self.disk_dvr, 'capabilities')
        @mock.patch.object(self.disk_dvr, 'get_disk_ref')
        def inst_on_disk(mock_disk_ref, mock_capb, mock_block, mock_boot):
            # Test boot from volume.
            mock_boot.return_value = True
            self.assertTrue(self.drv.instance_on_disk(self.inst))

            mock_boot.return_value = False
            # Disk driver is shared storage and can find the disk
            mock_capb['shared_storage'] = True
            mock_disk_ref.return_value = 'disk_reference'
            self.assertTrue(self.drv.instance_on_disk(self.inst))

            # Disk driver can't find it
            mock_disk_ref.return_value = None
            self.assertFalse(self.drv.instance_on_disk(self.inst))

            # Disk driver exception
            mock_disk_ref.side_effect = ValueError('Bad disk')
            self.assertFalse(self.drv.instance_on_disk(self.inst))
            mock_disk_ref.side_effect = None

            # Not on shared storage
            mock_capb['shared_storage'] = False
            self.assertFalse(self.drv.instance_on_disk(self.inst))

        inst_on_disk()

    @mock.patch('nova_powervm.virt.powervm.tasks.storage.'
                'CreateAndConnectCfgDrive.execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.storage.ConnectVolume'
                '.execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.storage.CreateDiskForImg'
                '.execute')
    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver.'
                '_is_booted_from_volume')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugMgmtVif.execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugVifs.execute')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('pypowervm.tasks.power.power_on')
    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver._vol_drv_iter')
    def test_spawn_ops(
        self, mock_vdi, mock_pwron, mock_get_flv, mock_cfg_drv, mock_plug_vifs,
        mock_plug_mgmt_vif, mock_boot_from_vol, mock_crt_disk_img,
        mock_conn_vol, mock_crt_cfg_drv):
        """Validates the 'typical' spawn flow of the spawn of an instance.

        Uses a basic disk image, attaching networks and powering on.
        """
        # Set up the mocks to the tasks.
        mock_get_flv.return_value = self.inst.get_flavor()
        mock_cfg_drv.return_value = False
        mock_boot_from_vol.return_value = False
        # Invoke the method.
        self.drv.spawn('context', self.inst, powervm.IMAGE1,
                       'injected_files', 'admin_password')

        # _vol_drv_iter not called from spawn because not recreate; but still
        # called from _add_volume_connection_tasks.
        mock_vdi.assert_has_calls([mock.call(
            'context', self.inst, bdms=[],
            stg_ftsk=self.build_tx_feed.return_value)])
        # Assert the correct tasks were called
        self.assertTrue(mock_plug_vifs.called)
        self.assertTrue(mock_plug_mgmt_vif.called)
        self.assertTrue(mock_crt_disk_img.called)
        self.crt_lpar.assert_called_with(
            self.apt, self.drv.host_wrapper, self.inst, self.inst.get_flavor(),
            nvram=None)
        self.assertTrue(mock_pwron.called)
        self.assertFalse(mock_pwron.call_args[1]['synchronous'])
        # Assert that tasks that are not supposed to be called are not called
        self.assertFalse(mock_conn_vol.called)
        self.assertFalse(mock_crt_cfg_drv.called)
        self.scrub_stg.assert_called_with(mock.ANY, self.stg_ftsk,
                                          lpars_exist=True)

    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugMgmtVif.execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugVifs.execute')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                'create_cfg_drv_vopt')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('pypowervm.tasks.power.power_on')
    def test_spawn_with_cfg(
        self, mock_pwron, mock_get_flv, mock_cfg_drv, mock_val_vopt,
        mock_cfg_vopt, mock_plug_vifs, mock_plug_mgmt_vif):
        """Validates the PowerVM spawn w/ config drive operations."""
        # Set up the mocks to the tasks.
        mock_get_flv.return_value = self.inst.get_flavor()
        mock_cfg_drv.return_value = True

        # Invoke the method.
        self.drv.spawn('context', self.inst, powervm.IMAGE1,
                       'injected_files', 'admin_password')

        # Create LPAR was called
        self.crt_lpar.assert_called_with(self.apt, self.drv.host_wrapper,
                                         self.inst, self.inst.get_flavor(),
                                         nvram=None)
        # Config drive was called
        self.assertTrue(mock_val_vopt.called)
        self.assertTrue(mock_cfg_vopt.called)

        # Power on was called
        self.assertTrue(mock_pwron.called)
        self.assertFalse(mock_pwron.call_args[1]['synchronous'])
        self.scrub_stg.assert_called_with(mock.ANY, self.stg_ftsk,
                                          lpars_exist=True)

    @mock.patch('nova.virt.block_device.DriverVolumeBlockDevice.save')
    @mock.patch('nova_powervm.virt.powervm.tasks.storage.CreateDiskForImg'
                '.execute')
    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver.'
                '_is_booted_from_volume')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugMgmtVif.execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugVifs.execute')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('pypowervm.tasks.power.power_on')
    def test_spawn_with_bdms(
        self, mock_pwron, mock_get_flv, mock_cfg_drv, mock_plug_vifs,
        mock_plug_mgmt_vif, mock_boot_from_vol, mock_crt_img, mock_save):
        """Validates the PowerVM spawn.

        Specific Test: spawn of an image that has a disk image and block device
        mappings are passed into spawn which originated from either the image
        metadata itself or the create server request. In particular, test when
        the BDMs passed in do not have the root device for the instance.
        """
        # Set up the mocks to the tasks.
        mock_get_flv.return_value = self.inst.get_flavor()
        mock_cfg_drv.return_value = False
        mock_boot_from_vol.return_value = False

        # Create some fake BDMs
        block_device_info = self._fake_bdms()

        # Invoke the method.
        self.drv.spawn('context', self.inst, powervm.IMAGE1,
                       'injected_files', 'admin_password',
                       block_device_info=block_device_info)

        self.assertTrue(mock_boot_from_vol.called)
        # Since the root device is not in the BDMs we expect the image disk to
        # be created.
        self.assertTrue(mock_crt_img.called)

        # Create LPAR was called
        self.crt_lpar.assert_called_with(self.apt, self.drv.host_wrapper,
                                         self.inst, self.inst.get_flavor(),
                                         nvram=None)
        # Power on was called
        self.assertTrue(mock_pwron.called)
        self.assertFalse(mock_pwron.call_args[1]['synchronous'])

        # Check that the connect volume was called
        self.assertEqual(2, self.vol_drv.connect_volume.call_count)

        # Make sure the save was invoked
        self.assertEqual(2, mock_save.call_count)

        self.scrub_stg.assert_called_with(mock.ANY, self.stg_ftsk,
                                          lpars_exist=True)

    @mock.patch('nova.virt.block_device.DriverVolumeBlockDevice.save')
    @mock.patch('nova_powervm.virt.powervm.tasks.storage.CreateDiskForImg'
                '.execute')
    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver.'
                '_is_booted_from_volume')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugMgmtVif.execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugVifs.execute')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('pypowervm.tasks.power.power_on')
    def test_spawn_with_image_meta_root_bdm(
        self, mock_pwron, mock_get_flv, mock_cfg_drv, mock_plug_vifs,
        mock_plug_mgmt_vif, mock_boot_from_vol, mock_crt_img, mock_save):

        """Validates the PowerVM spawn.

        Specific Test: spawn of an image that does not have a disk image but
        rather the block device mappings are passed into spawn.  These
        originated from either the image metadata itself or the create server
        request.  In particular, test when the BDMs passed in have the root
        device for the instance and image metadata from an image is also
        passed.

        Note this tests the ability to spawn an image that does not
        contain a disk image but rather contains block device mappings
        containing the root BDM. The
        nova.compute.api.API.snapshot_volume_backed flow produces such images.
        """
        # Set up the mocks to the tasks.
        mock_get_flv.return_value = self.inst.get_flavor()
        mock_cfg_drv.return_value = False
        mock_boot_from_vol.return_value = True

        # Create some fake BDMs
        block_device_info = self._fake_bdms()
        # Invoke the method.
        self.drv.spawn('context', self.inst, powervm.IMAGE1,
                       'injected_files', 'admin_password',
                       block_device_info=block_device_info)

        self.assertTrue(mock_boot_from_vol.called)
        # Since the root device is in the BDMs we do not expect the image disk
        # to be created.
        self.assertFalse(mock_crt_img.called)

        # Create LPAR was called
        self.crt_lpar.assert_called_with(self.apt, self.drv.host_wrapper,
                                         self.inst, self.inst.get_flavor(),
                                         nvram=None)
        # Power on was called
        self.assertTrue(mock_pwron.called)
        self.assertFalse(mock_pwron.call_args[1]['synchronous'])

        # Check that the connect volume was called
        self.assertEqual(2, self.vol_drv.connect_volume.call_count)

        self.scrub_stg.assert_called_with(mock.ANY, self.stg_ftsk,
                                          lpars_exist=True)

    @mock.patch('nova.virt.block_device.DriverVolumeBlockDevice.save')
    @mock.patch('nova_powervm.virt.powervm.tasks.storage.CreateDiskForImg'
                '.execute')
    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver.'
                '_is_booted_from_volume')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugMgmtVif.execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugVifs.execute')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('pypowervm.tasks.power.power_on')
    def test_spawn_with_root_bdm(
        self, mock_pwron, mock_get_flv, mock_cfg_drv, mock_plug_vifs,
        mock_plug_mgmt_vif, mock_boot_from_vol, mock_crt_img, mock_save):
        """Validates the PowerVM spawn.

        Specific test: when no image is given and only block device mappings
        are given on the create server request.
        """
        # Set up the mocks to the tasks.
        mock_get_flv.return_value = self.inst.get_flavor()
        mock_cfg_drv.return_value = False
        mock_boot_from_vol.return_value = True

        # Create some fake BDMs
        block_device_info = self._fake_bdms()
        # Invoke the method.
        self.drv.spawn('context', self.inst, powervm.IMAGE1,
                       'injected_files', 'admin_password',
                       block_device_info=block_device_info)

        self.assertTrue(mock_boot_from_vol.called)
        # Since the root device is in the BDMs we do not expect the image disk
        # to be created.
        self.assertFalse(mock_crt_img.called)

        # Create LPAR was called
        self.crt_lpar.assert_called_with(self.apt, self.drv.host_wrapper,
                                         self.inst, self.inst.get_flavor(),
                                         nvram=None)
        # Power on was called
        self.assertTrue(mock_pwron.called)
        self.assertFalse(mock_pwron.call_args[1]['synchronous'])

        # Check that the connect volume was called
        self.assertEqual(2, self.vol_drv.connect_volume.call_count)

        # Make sure the BDM save was invoked twice.
        self.assertEqual(2, mock_save.call_count)

        self.scrub_stg.assert_called_with(mock.ANY, self.stg_ftsk,
                                          lpars_exist=True)

    @mock.patch('nova_powervm.virt.powervm.tasks.storage.'
                'CreateAndConnectCfgDrive')
    @mock.patch('nova_powervm.virt.powervm.tasks.storage.ConnectVolume')
    @mock.patch('nova_powervm.virt.powervm.tasks.storage.ConnectDisk')
    @mock.patch('nova_powervm.virt.powervm.tasks.storage.FindDisk')
    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver.'
                '_is_booted_from_volume')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugMgmtVif')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugVifs')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('nova_powervm.virt.powervm.tasks.vm.PowerOn')
    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver._vol_drv_iter')
    @mock.patch('nova_powervm.virt.powervm.slot.build_slot_mgr')
    @mock.patch('taskflow.patterns.linear_flow.Flow')
    @mock.patch('taskflow.engines.run')
    def test_spawn_recreate(
        self, mock_tf_run, mock_flow, mock_build_slot_mgr, mock_vol_drv_iter,
        mock_pwron, mock_get_flv, mock_cfg_drv, mock_plug_vifs,
        mock_plug_mgmt_vif, mock_boot_from_vol, mock_find_disk, mock_conn_disk,
        mock_conn_vol, mock_crt_cfg_drv):
        """Validates the 'recreate' spawn flow.

        Uses a basic disk image, attaching networks and powering on.
        """
        # Set up the mocks to the tasks.
        self.drv.nvram_mgr = mock.Mock()
        self.drv.nvram_mgr.fetch.return_value = 'nvram data'
        mock_get_flv.return_value = self.inst.get_flavor()
        mock_cfg_drv.return_value = False
        mock_boot_from_vol.return_value = False
        # Some tasks are mocked; some are not.  Have Flow.add "execute" them so
        # we can verify the code thereunder.
        mock_flow.return_value.add.side_effect = lambda task: task.execute()
        self.inst.task_state = task_states.REBUILD_SPAWNING
        # Invoke the method.
        self.drv.spawn('context', self.inst, powervm.EMPTY_IMAGE,
                       'injected_files', 'admin_password')

        # Recreate uses all XAGs.
        self.build_tx_feed.assert_called_once_with(
            self.drv.adapter, self.drv.host_uuid, xag={pvm_const.XAG.VIO_FMAP,
                                                       pvm_const.XAG.VIO_STOR,
                                                       pvm_const.XAG.VIO_SMAP})
        # _vol_drv_iter gets called once in spawn itself, and once under
        # _add_volume_connection_tasks.
        # TODO(IBM): Find a way to make the call just once.  Unless it's cheap.
        mock_vol_drv_iter.assert_has_calls([mock.call(
            'context', self.inst, bdms=[],
            stg_ftsk=self.build_tx_feed.return_value)] * 2)
        mock_build_slot_mgr.assert_called_once_with(
            self.inst, self.drv.store_api, adapter=self.drv.adapter,
            vol_drv_iter=mock_vol_drv_iter.return_value)
        # Assert the correct tasks were called
        mock_plug_vifs.assert_called_once_with(
            self.drv.virtapi, self.drv.adapter, self.inst, None,
            self.drv.host_uuid, mock_build_slot_mgr.return_value)
        mock_plug_mgmt_vif.assert_called_once_with(
            self.drv.adapter, self.inst, self.drv.host_uuid,
            mock_build_slot_mgr.return_value)
        self.assertTrue(mock_plug_mgmt_vif.called)
        self.assertTrue(mock_find_disk.called)
        self.crt_lpar.assert_called_with(
            self.apt, self.drv.host_wrapper, self.inst, self.inst.get_flavor(),
            nvram='nvram data')
        # SaveSlotStore.execute
        mock_build_slot_mgr.return_value.save.assert_called_once_with()
        self.assertTrue(mock_pwron.called)
        # Assert that tasks that are not supposed to be called are not called
        self.assertFalse(mock_conn_vol.called)
        self.assertFalse(mock_crt_cfg_drv.called)
        self.scrub_stg.assert_called_with(mock.ANY, self.stg_ftsk,
                                          lpars_exist=True)

    @mock.patch('nova.virt.block_device.DriverVolumeBlockDevice.save')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugMgmtVif.execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugVifs.execute')
    @mock.patch('nova_powervm.virt.powervm.vm.dlt_lpar')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('pypowervm.tasks.power.power_on')
    @mock.patch('pypowervm.tasks.power.power_off')
    def test_spawn_ops_rollback(
        self, mock_pwroff, mock_pwron, mock_get_flv, mock_cfg_drv, mock_dlt,
        mock_plug_vifs, mock_plug_mgmt_vifs, mock_save):
        """Validates the PowerVM driver operations.  Will do a rollback."""
        # Set up the mocks to the tasks.
        mock_get_flv.return_value = self.inst.get_flavor()
        mock_cfg_drv.return_value = False
        block_device_info = self._fake_bdms()

        # Make sure power on fails.
        mock_pwron.side_effect = exc.Forbidden()

        # Invoke the method.
        self.assertRaises(exc.Forbidden, self.drv.spawn, 'context', self.inst,
                          powervm.IMAGE1, 'injected_files', 'admin_password',
                          block_device_info=block_device_info)

        # Create LPAR was called
        self.crt_lpar.assert_called_with(self.apt, self.drv.host_wrapper,
                                         self.inst, self.inst.get_flavor(),
                                         nvram=None)
        self.assertEqual(2, self.vol_drv.connect_volume.call_count)

        # Power on was called
        self.assertTrue(mock_pwron.called)
        self.assertFalse(mock_pwron.call_args[1]['synchronous'])

        # Validate the rollbacks were called
        self.assertEqual(2, self.vol_drv.disconnect_volume.call_count)

    @mock.patch('nova.virt.block_device.DriverVolumeBlockDevice.save')
    @mock.patch('nova_powervm.virt.powervm.tasks.storage.CreateDiskForImg'
                '.execute')
    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver.'
                '_is_booted_from_volume')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugMgmtVif.execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugVifs.execute')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('nova_powervm.virt.powervm.tasks.vm.UpdateIBMiSettings'
                '.execute')
    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver.'
                '_get_boot_connectivity_type')
    @mock.patch('pypowervm.tasks.power.power_on')
    def test_spawn_ibmi(
        self, mock_pwron, mock_boot_conn_type,
        mock_update_lod_src, mock_get_flv, mock_cfg_drv,
        mock_plug_vifs, mock_plug_mgmt_vif, mock_boot_from_vol,
        mock_crt_img, mock_save):
        """Validates the PowerVM spawn to create an IBMi server."""
        # Set up the mocks to the tasks.
        mock_get_flv.return_value = self.inst_ibmi.get_flavor()
        mock_cfg_drv.return_value = False
        mock_boot_from_vol.return_value = True
        mock_boot_conn_type.return_value = 'vscsi'
        # Create some fake BDMs
        block_device_info = self._fake_bdms()
        # Invoke the method.
        self.drv.spawn('context', self.inst_ibmi, powervm.IMAGE1,
                       'injected_files', 'admin_password',
                       block_device_info=block_device_info)

        self.assertTrue(mock_boot_from_vol.called)
        # Since the root device is in the BDMs we do not expect the image disk
        # to be created.
        self.assertFalse(mock_crt_img.called)

        # Create LPAR was called
        self.crt_lpar.assert_called_with(self.apt, self.drv.host_wrapper,
                                         self.inst_ibmi,
                                         self.inst_ibmi.get_flavor(),
                                         nvram=None)

        self.assertTrue(mock_boot_conn_type.called)
        self.assertTrue(mock_update_lod_src.called)

        # Power on was called
        self.assertTrue(mock_pwron.called)
        self.assertFalse(mock_pwron.call_args[1]['synchronous'])

        # Check that the connect volume was called
        self.assertEqual(2, self.vol_drv.connect_volume.call_count)

        # Make sure the BDM save was invoked twice.
        self.assertEqual(2, mock_save.call_count)

    @mock.patch('nova_powervm.virt.powervm.tasks.storage.'
                'CreateAndConnectCfgDrive.execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.storage.ConnectVolume'
                '.execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.storage.CreateDiskForImg'
                '.execute')
    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver.'
                '_is_booted_from_volume')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugMgmtVif.execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugVifs.execute')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('nova_powervm.virt.powervm.tasks.vm.UpdateIBMiSettings'
                '.execute')
    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver.'
                '_get_boot_connectivity_type')
    @mock.patch('pypowervm.tasks.power.power_on')
    def test_spawn_ibmi_without_bdms(
        self, mock_pwron, mock_boot_conn_type, mock_update_lod_src,
        mock_get_flv, mock_cfg_drv, mock_plug_vifs,
        mock_plug_mgmt_vif, mock_boot_from_vol, mock_crt_disk_img,
        mock_conn_vol, mock_crt_cfg_drv):
        """Validates the 'typical' spawn flow for IBMi

        Perform an UT using an image with local disk, attaching networks
        and powering on.
        """
        # Set up the mocks to the tasks.
        mock_get_flv.return_value = self.inst_ibmi.get_flavor()
        mock_cfg_drv.return_value = False
        mock_boot_from_vol.return_value = False
        mock_boot_conn_type.return_value = 'vscsi'
        # Invoke the method.
        self.drv.spawn('context', self.inst_ibmi, powervm.IMAGE1,
                       'injected_files', 'admin_password')

        # Assert the correct tasks were called
        self.assertTrue(mock_plug_vifs.called)
        self.assertTrue(mock_plug_mgmt_vif.called)
        self.assertTrue(mock_crt_disk_img.called)
        self.crt_lpar.assert_called_with(
            self.apt, self.drv.host_wrapper, self.inst_ibmi,
            self.inst_ibmi.get_flavor(), nvram=None)
        self.assertTrue(mock_update_lod_src.called)
        self.assertTrue(mock_pwron.called)
        self.assertFalse(mock_pwron.call_args[1]['synchronous'])
        # Assert that tasks that are not supposed to be called are not called
        self.assertFalse(mock_conn_vol.called)
        self.assertFalse(mock_crt_cfg_drv.called)

    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage.'
                'delete_disks')
    @mock.patch('nova_powervm.virt.powervm.tasks.storage.CreateDiskForImg.'
                'execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugMgmtVif.execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugVifs.execute')
    @mock.patch('nova_powervm.virt.powervm.vm.dlt_lpar')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    def test_spawn_ops_rollback_disk(
        self, mock_get_flv, mock_cfg_drv, mock_dlt, mock_plug_vifs,
        mock_plug_mgmt_vifs, mock_crt_disk, mock_delete_disks):
        """Validates the rollback if failure occurs on disk create."""
        # Set up the mocks to the tasks.
        mock_get_flv.return_value = self.inst.get_flavor()
        mock_cfg_drv.return_value = False

        # Make sure power on fails.
        mock_crt_disk.side_effect = exc.Forbidden()

        # Invoke the method.
        self.assertRaises(exc.Forbidden, self.drv.spawn, 'context', self.inst,
                          powervm.IMAGE1, 'injected_files', 'admin_password',
                          block_device_info=None)

        # Create LPAR was called
        self.crt_lpar.assert_called_with(self.apt, self.drv.host_wrapper,
                                         self.inst, self.inst.get_flavor(),
                                         nvram=None)

        # Since the create disks method failed, the delete disks should not
        # have been called
        self.assertFalse(mock_delete_disks.called)

    @mock.patch('nova.virt.block_device.DriverVolumeBlockDevice.save')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugMgmtVif.execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.network.PlugVifs.execute')
    @mock.patch('nova_powervm.virt.powervm.vm.dlt_lpar')
    @mock.patch('nova.virt.configdrive.required_by')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('pypowervm.tasks.power.power_on')
    @mock.patch('pypowervm.tasks.power.power_off')
    def test_spawn_ops_rollback_on_vol_connect(
        self, mock_pwroff, mock_pwron, mock_get_flv, mock_cfg_drv, mock_dlt,
        mock_plug_vifs, mock_plug_mgmt_vifs, mock_save):
        """Validates the rollbacks on a volume connect failure."""
        # Set up the mocks to the tasks.
        mock_get_flv.return_value = self.inst.get_flavor()
        mock_cfg_drv.return_value = False
        block_device_info = self._fake_bdms()

        # Have the connect fail.  Also fail the disconnect on revert.  Should
        # not block the rollback.
        self.vol_drv.connect_volume.side_effect = exc.Forbidden()
        self.vol_drv.disconnect_volume.side_effect = p_exc.VolumeDetachFailed(
            volume_id='1', instance_name=self.inst.name, reason='Test Case')

        # Invoke the method.
        self.assertRaises(exc.Forbidden, self.drv.spawn, 'context', self.inst,
                          powervm.IMAGE1, 'injected_files', 'admin_password',
                          block_device_info=block_device_info)

        # Create LPAR was called
        self.crt_lpar.assert_called_with(self.apt, self.drv.host_wrapper,
                                         self.inst, self.inst.get_flavor(),
                                         nvram=None)
        self.assertEqual(1, self.vol_drv.connect_volume.call_count)

        # Power on should not be called.  Shouldn't get that far in flow.
        self.assertFalse(mock_pwron.called)

        # Disconnect should, as it may need to remove from one of the VIOSes
        # (but maybe failed on another).
        self.assertTrue(self.vol_drv.disconnect_volume.called)

    @mock.patch('nova.block_device.get_root_bdm')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping')
    def test_is_booted_from_volume(self, mock_get_mapping, mock_get_root_bdm):
        block_device_info = self._fake_bdms()
        ret = self.drv._is_booted_from_volume(block_device_info)
        mock_get_root_bdm.assert_called_once_with(
            mock_get_mapping.return_value)
        self.assertTrue(ret)
        self.assertEqual(1, mock_get_mapping.call_count)

        mock_get_mapping.reset_mock()
        mock_get_root_bdm.return_value = None
        ret = self.drv._is_booted_from_volume(block_device_info)
        self.assertFalse(ret)
        self.assertEqual(1, mock_get_mapping.call_count)

        # Test if block_device_info is None
        ret = self.drv._is_booted_from_volume(None)
        self.assertFalse(ret)

    def test_get_inst_xag(self):
        # No volumes - should be just the SCSI mapping
        xag = self.drv._get_inst_xag(mock.Mock(), None)
        self.assertEqual([pvm_const.XAG.VIO_SMAP], xag)

        # The vSCSI Volume attach - only needs the SCSI mapping.
        self.flags(fc_attach_strategy='vscsi', group='powervm')
        xag = self.drv._get_inst_xag(mock.Mock(), [mock.Mock()])
        self.assertEqual([pvm_const.XAG.VIO_SMAP], xag)

        # The NPIV volume attach - requires SCSI, Storage and FC Mapping
        self.flags(fc_attach_strategy='npiv', group='powervm')
        xag = self.drv._get_inst_xag(mock.Mock(), [mock.Mock()])
        self.assertEqual({pvm_const.XAG.VIO_STOR,
                          pvm_const.XAG.VIO_SMAP,
                          pvm_const.XAG.VIO_FMAP}, set(xag))

        # The vSCSI Volume attach - Ensure case insensitive.
        self.flags(fc_attach_strategy='VSCSI', group='powervm')
        xag = self.drv._get_inst_xag(mock.Mock(), [mock.Mock()])
        self.assertEqual([pvm_const.XAG.VIO_SMAP], xag)

        # If a recreate, all should be returned
        xag = self.drv._get_inst_xag(mock.Mock(), [mock.Mock()], recreate=True)
        self.assertEqual({pvm_const.XAG.VIO_STOR,
                          pvm_const.XAG.VIO_SMAP,
                          pvm_const.XAG.VIO_FMAP}, set(xag))

    @mock.patch('nova_powervm.virt.powervm.tasks.storage.ConnectVolume')
    @mock.patch('nova_powervm.virt.powervm.tasks.storage.SaveBDM')
    def test_add_vol_conn_task(self, mock_save_bdm, mock_conn_vol):
        bdm1, bdm2, vol_drv1, vol_drv2 = [mock.Mock()] * 4
        flow = mock.Mock()
        mock_save_bdm.side_effect = 'save_bdm1', 'save_bdm2'
        mock_conn_vol.side_effect = 'conn_vol1', 'conn_vol2'
        vals = [(bdm1, vol_drv1), (bdm2, vol_drv2)]
        with mock.patch.object(self.drv, '_vol_drv_iter',
                               return_value=vals) as mock_vdi:
            self.drv._add_volume_connection_tasks(
                'context', 'instance', 'bdms', flow, 'stg_ftsk', 'slot_mgr')
        mock_vdi.assert_called_once_with('context', 'instance', bdms='bdms',
                                         stg_ftsk='stg_ftsk')
        mock_conn_vol.assert_has_calls([mock.call(vol_drv1, 'slot_mgr'),
                                        mock.call(vol_drv2, 'slot_mgr')])
        mock_save_bdm.assert_has_calls([mock.call(bdm1, 'instance'),
                                        mock.call(bdm2, 'instance')])
        flow.add.assert_has_calls([
            mock.call('conn_vol1'), mock.call('save_bdm1'),
            mock.call('conn_vol2'), mock.call('save_bdm2')])

    @mock.patch('nova_powervm.virt.powervm.tasks.storage.DisconnectVolume')
    def test_add_vol_disconn_task(self, mock_disconn_vol):
        vol_drv1, vol_drv2 = [mock.Mock()] * 2
        flow = mock.Mock()
        mock_disconn_vol.side_effect = 'disconn_vol1', 'disconn_vol2'
        vals = [('bdm', vol_drv1), ('bdm', vol_drv2)]
        with mock.patch.object(self.drv, '_vol_drv_iter',
                               return_value=vals) as mock_vdi:
            self.drv._add_volume_disconnection_tasks(
                'context', 'instance', 'bdms', flow, 'stg_ftsk', 'slot_mgr')
        mock_vdi.assert_called_once_with('context', 'instance', bdms='bdms',
                                         stg_ftsk='stg_ftsk')
        mock_disconn_vol.assert_has_calls([mock.call(vol_drv1, 'slot_mgr'),
                                           mock.call(vol_drv2, 'slot_mgr')])
        flow.add.assert_has_calls([mock.call('disconn_vol1'),
                                   mock.call('disconn_vol2')])

    @mock.patch('nova_powervm.virt.powervm.tasks.network.UnplugVifs.execute')
    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver.'
                '_is_booted_from_volume')
    @mock.patch('nova_powervm.virt.powervm.vm.dlt_lpar')
    @mock.patch('nova_powervm.virt.powervm.vm.power_off')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                'dlt_vopt')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('nova_powervm.virt.powervm.slot.build_slot_mgr')
    def test_destroy_internal(
        self, mock_bld_slot_mgr, mock_get_flv, mock_pvmuuid,
        mock_val_vopt, mock_dlt_vopt, mock_pwroff, mock_dlt,
        mock_boot_from_vol, mock_unplug_vifs):
        """Validates the basic PowerVM destroy."""
        # NVRAM Manager
        self.drv.nvram_mgr = mock.Mock()

        # BDMs
        mock_bdms = self._fake_bdms()
        mock_boot_from_vol.return_value = False
        # Invoke the method.
        self.drv.destroy('context', self.inst, ['net'],
                         block_device_info=mock_bdms)

        # Power off was called
        mock_pwroff.assert_called_with(self.drv.adapter, self.inst,
                                       self.drv.host_uuid,
                                       force_immediate=True)

        mock_bld_slot_mgr.assert_called_once_with(self.inst,
                                                  self.drv.store_api)
        # Unplug should have been called
        # TODO(IBM): Find a way to verify UnplugVifs(..., slot_mgr)
        self.assertTrue(mock_unplug_vifs.called)

        # Validate that the vopt delete was called
        self.assertTrue(mock_dlt_vopt.called)

        # Validate that the volume detach was called
        self.vol_drv.disconnect_volume.assert_has_calls(
            [mock.call(mock_bld_slot_mgr.return_value)] * 2)
        # Delete LPAR was called
        mock_dlt.assert_called_with(self.apt, mock.ANY)

        # Validate root device in bdm was checked.
        mock_boot_from_vol.assert_called_with(mock_bdms)

        # Validate disk driver detach and delete disk methods were called.
        self.assertTrue(self.drv.disk_dvr.delete_disks.called)
        self.assertTrue(self.drv.disk_dvr.disconnect_image_disk.called)

        # NVRAM was deleted
        self.drv.nvram_mgr.remove.assert_called_once_with(self.inst)
        # Slot store was deleted
        mock_bld_slot_mgr.return_value.delete.assert_called_once_with()

        def reset_mocks():
            # Reset the mocks
            for mk in [mock_pwroff, mock_dlt, mock_dlt_vopt,
                       self.vol_drv, mock_dlt,
                       mock_boot_from_vol]:
                mk.reset_mock()

        def assert_not_called():
            # Power off was not called
            self.assertFalse(mock_pwroff.called)

            # Validate that the vopt delete was not called
            self.assertFalse(mock_dlt_vopt.called)

            # Validate that the volume detach was not called
            self.assertFalse(self.vol_drv.disconnect_volume.called)

            # Delete LPAR was not called
            self.assertFalse(mock_dlt.called)

        # Test when the VM's root device is a BDM.
        reset_mocks()
        mock_boot_from_vol.return_value = True
        self.drv.disk_dvr.delete_disks.reset_mock()
        self.drv.disk_dvr.disconnect_image_disk.reset_mock()

        # Invoke the method.
        self.drv.destroy('context', self.inst, None,
                         block_device_info=mock_bdms)

        # Validate root device in bdm was checked.
        mock_boot_from_vol.assert_called_with(mock_bdms)

        # Validate disk driver detach and delete disk methods were called.
        self.assertFalse(self.drv.disk_dvr.delete_disks.called)
        self.assertFalse(self.drv.disk_dvr.disconnect_image_disk.called)

        # Test when destroy_disks set to False.
        reset_mocks()
        mock_boot_from_vol.return_value = True
        self.drv.disk_dvr.delete_disks.reset_mock()
        self.drv.disk_dvr.disconnect_image_disk.reset_mock()

        # Invoke the method.
        self.drv.destroy('context', self.inst, None,
                         block_device_info=mock_bdms, destroy_disks=False)

        mock_pwroff.assert_called_with(self.drv.adapter, self.inst,
                                       self.drv.host_uuid,
                                       force_immediate=False)

        # Start negative tests
        reset_mocks()
        # Pretend we didn't find the VM on the system
        mock_pvmuuid.side_effect = exc.InstanceNotFound(
            instance_id=self.inst.name)

        # Invoke the method.
        self.drv.destroy('context', self.inst, None,
                         block_device_info=mock_bdms)
        assert_not_called()

        mock_resp = mock.Mock()
        mock_resp.status = 404
        mock_resp.reqpath = (
            '/rest/api/uom/ManagedSystem/c5d782c7-44e4-3086-ad15-'
            'b16fb039d63b/LogicalPartition/1B5FB633-16D1-4E10-A14'
            '5-E6FB905161A3?group=None')
        mock_pvmuuid.side_effect = pvm_exc.HttpError(mock_resp)

        # Invoke the method.
        self.drv.destroy('context', self.inst, [],
                         block_device_info=mock_bdms)
        assert_not_called()

        # Ensure the exception is raised with non-matching path
        reset_mocks()
        mock_resp.reqpath = (
            '/rest/api/uom/ManagedSystem/c5d782c7-44e4-3086-ad15-'
            'b16fb039d63b/SomeResource/1B5FB633-16D1-4E10-A14'
            '5-E6FB905161A3?group=None')
        # Invoke the method.
        self.assertRaises(exc.InstanceTerminationFailure,
                          self.drv.destroy, 'context', self.inst,
                          [], block_device_info=mock_bdms)
        assert_not_called()

        # Test generic exception
        mock_pvmuuid.side_effect = ValueError('Some error')
        # Invoke the method.
        self.assertRaises(exc.InstanceTerminationFailure,
                          self.drv.destroy, 'context', self.inst,
                          [], block_device_info=mock_bdms)
        assert_not_called()

    @mock.patch('nova_powervm.virt.powervm.tasks.network.UnplugVifs.execute')
    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver.'
                '_is_booted_from_volume')
    @mock.patch('nova_powervm.virt.powervm.vm.dlt_lpar')
    @mock.patch('nova_powervm.virt.powervm.vm.power_off')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                'dlt_vopt')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('nova_powervm.virt.powervm.slot.build_slot_mgr')
    def test_destroy_internal_no_nvram_cleanup(
        self, mock_bld_slot_mgr, mock_get_flv, mock_pvmuuid,
        mock_val_vopt, mock_dlt_vopt, mock_pwroff, mock_dlt,
        mock_boot_from_vol, mock_unplug_vifs):
        """Validates the basic PowerVM destroy, without NVRAM cleanup.

        Used to validate the behavior when destroying evacuated instances.
        It should not clean up NVRAM as the instance is still on another host.
        """
        # NVRAM Manager
        self.drv.nvram_mgr = mock.Mock()
        self.inst.host = 'other'

        # BDMs
        mock_bdms = self._fake_bdms()
        mock_boot_from_vol.return_value = False

        # Invoke the method.
        self.drv.destroy('context', self.inst, ['net'],
                         block_device_info=mock_bdms)

        # Power off was called
        mock_pwroff.assert_called_with(self.drv.adapter, self.inst,
                                       self.drv.host_uuid,
                                       force_immediate=True)

        mock_bld_slot_mgr.assert_called_once_with(self.inst,
                                                  self.drv.store_api)
        # Unplug should have been called
        # TODO(IBM): Find a way to verify UnplugVifs(..., slot_mgr)
        self.assertTrue(mock_unplug_vifs.called)

        # Validate that the vopt delete was called
        self.assertTrue(mock_dlt_vopt.called)

        # Validate that the volume detach was called
        self.vol_drv.disconnect_volume.assert_has_calls(
            [mock.call(mock_bld_slot_mgr.return_value)] * 2)
        # Delete LPAR was called
        mock_dlt.assert_called_with(self.apt, mock.ANY)

        # Validate root device in bdm was checked.
        mock_boot_from_vol.assert_called_with(mock_bdms)

        # Validate disk driver detach and delete disk methods were called.
        self.assertTrue(self.drv.disk_dvr.delete_disks.called)
        self.assertTrue(self.drv.disk_dvr.disconnect_image_disk.called)

        # NVRAM was NOT deleted
        self.assertFalse(self.drv.nvram_mgr.remove.called)
        self.assertFalse(mock_bld_slot_mgr.return_value.delete.called)

    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_qp')
    def test_destroy(self, mock_getqp, mock_getuuid):
        """Validates the basic PowerVM destroy."""
        # BDMs
        mock_bdms = self._fake_bdms()

        with mock.patch.object(self.drv, '_destroy') as mock_dst_int:
            # Invoke the method.
            self.drv.destroy('context', self.inst, [],
                             block_device_info=mock_bdms)
        mock_dst_int.assert_called_with(
            'context', self.inst, block_device_info=mock_bdms,
            destroy_disks=True, shutdown=True, network_info=[])
        self.san_lpar_name.assert_not_called()

        # Test delete during migrate / resize
        self.inst.task_state = task_states.RESIZE_REVERTING
        mock_getqp.return_value = 'resize_' + self.inst.name
        with mock.patch.object(self.drv, '_destroy') as mock_dst_int:
            # Invoke the method.
            self.drv.destroy('context', self.inst, [],
                             block_device_info=mock_bdms)
        # We shouldn't delete our resize_ instances
        mock_dst_int.assert_not_called()
        self.san_lpar_name.assert_called_with('resize_' + self.inst.name)
        self.san_lpar_name.reset_mock()

        # Now test migrating...
        mock_getqp.return_value = 'migrate_' + self.inst.name
        with mock.patch.object(self.drv, '_destroy') as mock_dst_int:
            # Invoke the method.
            self.drv.destroy('context', self.inst, [],
                             block_device_info=mock_bdms)
        # If it is a migrated instance, it should be deleted.
        mock_dst_int.assert_called_with(
            'context', self.inst, block_device_info=mock_bdms,
            destroy_disks=True, shutdown=True, network_info=[])

    @mock.patch('nova_powervm.virt.powervm.slot.build_slot_mgr')
    def test_attach_volume(self, mock_bld_slot_mgr):
        """Validates the basic PowerVM attach volume."""
        # BDMs
        mock_bdm = self._fake_bdms()['block_device_mapping'][0]

        with mock.patch.object(self.inst, 'save') as mock_save:
            # Invoke the method.
            self.drv.attach_volume('context', mock_bdm.get('connection_info'),
                                   self.inst, mock.Mock())

        mock_bld_slot_mgr.assert_called_once_with(self.inst,
                                                  self.drv.store_api)
        # Verify the connect volume was invoked
        self.vol_drv.connect_volume.assert_called_once_with(
            mock_bld_slot_mgr.return_value)
        mock_bld_slot_mgr.return_value.save.assert_called_once_with()
        self.assertTrue(mock_save.called)

    @mock.patch('nova_powervm.virt.powervm.vm.instance_exists')
    @mock.patch('nova_powervm.virt.powervm.slot.build_slot_mgr')
    def test_detach_volume(self, mock_bld_slot_mgr, mock_inst_exists):
        """Validates the basic PowerVM detach volume."""
        # Mock that the instance exists for the first test, then not.
        mock_inst_exists.side_effect = [True, False, False]

        # BDMs
        mock_bdm = self._fake_bdms()['block_device_mapping'][0]
        # Invoke the method, good path test.
        self.drv.detach_volume(mock_bdm.get('connection_info'), self.inst,
                               mock.Mock())

        mock_bld_slot_mgr.assert_called_once_with(self.inst,
                                                  self.drv.store_api)
        # Verify the disconnect volume was invoked
        self.vol_drv.disconnect_volume.assert_called_once_with(
            mock_bld_slot_mgr.return_value)
        mock_bld_slot_mgr.return_value.save.assert_called_once_with()

        # Invoke the method, instance doesn't exist, no migration
        self.vol_drv.disconnect_volume.reset_mock()
        self.drv.detach_volume(mock_bdm.get('connection_info'), self.inst,
                               mock.Mock())
        # Verify the disconnect volume was not invoked
        self.assertEqual(0, self.vol_drv.disconnect_volume.call_count)

        # Test instance doesn't exist, migration cleanup
        self.vol_drv.disconnect_volume.reset_mock()
        mig = lpm.LiveMigrationDest(self.drv, self.inst)
        self.drv.live_migrations[self.inst.uuid] = mig
        with mock.patch.object(mig, 'cleanup_volume') as mock_clnup:
            self.drv.detach_volume(mock_bdm.get('connection_info'), self.inst,
                                   mock.Mock())
        # The cleanup should have been called since there was a migration
        self.assertEqual(1, mock_clnup.call_count)
        # Verify the disconnect volume was not invoked
        self.assertEqual(0, self.vol_drv.disconnect_volume.call_count)

    @mock.patch('nova_powervm.virt.powervm.tasks.network.UnplugVifs.execute')
    @mock.patch('nova_powervm.virt.powervm.vm.dlt_lpar')
    @mock.patch('nova_powervm.virt.powervm.vm.power_off')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                'dlt_vopt')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    def test_destroy_rollback(
        self, mock_get_flv, mock_val_vopt, mock_dlt_vopt,
        mock_pwroff, mock_dlt, mock_unplug_vifs):
        """Validates the basic PowerVM destroy rollback mechanism works."""
        # Set up the mocks to the tasks.
        mock_get_flv.return_value = self.inst.get_flavor()

        # BDMs
        mock_bdms = self._fake_bdms()

        # Fire a failure in the power off.
        mock_dlt.side_effect = exc.Forbidden()

        # Have the connect volume fail on the rollback.  Should not block the
        # full rollback.
        self.vol_drv.connect_volume.side_effect = p_exc.VolumeAttachFailed(
            volume_id='1', instance_name=self.inst.name, reason='Test Case')

        # Invoke the method.
        self.assertRaises(exc.InstanceTerminationFailure, self.drv.destroy,
                          'context', self.inst, [],
                          block_device_info=mock_bdms)

        # Validate that the vopt delete was called
        self.assertTrue(mock_dlt_vopt.called)
        self.assertTrue(mock_unplug_vifs.called)

        # Validate that the volume detach was called
        self.assertEqual(2, self.vol_drv.disconnect_volume.call_count)

        # Delete LPAR was called
        mock_dlt.assert_called_with(self.apt, mock.ANY)

        # Validate the rollbacks were called.
        self.assertEqual(2, self.vol_drv.connect_volume.call_count)

    @mock.patch('nova_powervm.virt.powervm.slot.build_slot_mgr')
    def test_migrate_disk_and_power_off(self, mock_bld_slot_mgr):
        """Validates the PowerVM driver migrate / resize operation."""
        # Set up the mocks to the migrate / resize operation.
        host = self.drv.get_host_ip_addr()
        resp = pvm_adp.Response('method', 'path', 'status', 'reason', {})
        resp.entry = pvm_lpar.LPAR._bld(None).entry
        self.apt.read.return_value = resp

        # BDMs
        mock_bdms = self._fake_bdms()

        # Catch root disk resize smaller.
        small_root = objects.Flavor(vcpus=1, memory_mb=2048, root_gb=9)
        self.assertRaises(
            exc.InstanceFaultRollback, self.drv.migrate_disk_and_power_off,
            'context', self.inst, 'dest', small_root, 'network_info',
            mock_bdms)

        # Boot disk resize
        boot_flav = objects.Flavor(vcpus=1, memory_mb=2048, root_gb=12)
        # Tasks expected to be added for migrate
        expected = [
            'pwr_off_lpar',
            'store_nvram',
            'extend_disk_boot',
            'disconnect_vol_*',
            'disconnect_vol_*',
            'fake',
            'rename_lpar_migrate_instance-00000001',
        ]
        dest_host = host + '1'
        with fx.DriverTaskFlow() as taskflow_fix:
            self.drv.migrate_disk_and_power_off(
                'context', self.inst, dest_host, boot_flav, 'network_info',
                mock_bdms)
            taskflow_fix.assert_tasks_added(self, expected)
            mock_bld_slot_mgr.assert_called_once_with(
                self.inst, self.drv.store_api, adapter=self.drv.adapter,
                vol_drv_iter=mock.ANY)
            # Check the size set in the resize task
            extend_task = taskflow_fix.tasks_added[
                expected.index('extend_disk_boot')]
            self.assertEqual(extend_task.size, 12)
            # Ensure slot manager was passed to disconnect
            self.assertEqual(mock_bld_slot_mgr.return_value,
                             taskflow_fix.tasks_added[3].slot_mgr)
            self.assertEqual(mock_bld_slot_mgr.return_value,
                             taskflow_fix.tasks_added[4].slot_mgr)
        self.san_lpar_name.assert_called_with('migrate_' + self.inst.name)

    @mock.patch('nova.objects.flavor.Flavor.get_by_id')
    @mock.patch('nova_powervm.virt.powervm.slot.build_slot_mgr')
    def test_finish_migration(self, mock_bld_slot_mgr, mock_get_flv):
        mock_bdms = self._fake_bdms()
        mig = objects.Migration(**powervm.TEST_MIGRATION)
        mig_same_host = objects.Migration(**powervm.TEST_MIGRATION_SAME_HOST)
        disk_info = {}

        # The first test is different hosts but local storage, should fail
        self.assertRaises(exc.InstanceFaultRollback,
                          self.drv.finish_migration,
                          'context', mig, self.inst, disk_info, 'network_info',
                          powervm.IMAGE1, 'resize_instance', mock_bdms)

        # The rest of the test need to pass the shared disk test
        self.disk_dvr.validate.return_value = None

        # Tasks expected to be added for migration to different host
        expected = [
            'crt_lpar',
            'plug_vifs',
            'plug_mgmt_vif',
            'find_disk',
            'connect_disk',
            'connect_vol_*',
            'save_bdm_fake_vol1',
            'connect_vol_*',
            'save_bdm_fake_vol2',
            'fake',
            'get_lpar',
            'pwr_lpar',
        ]
        with fx.DriverTaskFlow() as taskflow_fix:
            self.drv.finish_migration(
                'context', mig, self.inst, disk_info, 'network_info',
                powervm.IMAGE1, 'resize_instance', block_device_info=mock_bdms)
            mock_bld_slot_mgr.assert_called_once_with(
                self.inst, self.drv.store_api, adapter=self.drv.adapter,
                vol_drv_iter=mock.ANY)
            taskflow_fix.assert_tasks_added(self, expected)
            # Slot manager was passed to PlugVifs, PlugMgmtVif, and
            # connect_volume (twice)
            for idx in (1, 2, 5, 7):
                self.assertEqual(mock_bld_slot_mgr.return_value,
                                 taskflow_fix.tasks_added[idx].slot_mgr)
        self.san_lpar_name.assert_not_called()

        mock_bld_slot_mgr.reset_mock()

        # Tasks expected to be added for resize to the same host
        expected = [
            'resize_lpar',
            'connect_vol_*',
            'save_bdm_fake_vol1',
            'connect_vol_*',
            'save_bdm_fake_vol2',
            'fake',
            'get_lpar',
            'pwr_lpar',
        ]
        with fx.DriverTaskFlow() as taskflow_fix:
            self.drv.finish_migration(
                'context', mig_same_host, self.inst, disk_info, 'network_info',
                powervm.IMAGE1, 'resize_instance', block_device_info=mock_bdms)
            taskflow_fix.assert_tasks_added(self, expected)
            mock_bld_slot_mgr.assert_called_once_with(
                self.inst, self.drv.store_api, adapter=self.drv.adapter,
                vol_drv_iter=mock.ANY)
            # Slot manager was passed to connect_volume (twice)
            for idx in (1, 3):
                self.assertEqual(mock_bld_slot_mgr.return_value,
                                 taskflow_fix.tasks_added[idx].slot_mgr)
        self.san_lpar_name.assert_called_with('resize_' + self.inst.name)
        self.san_lpar_name.reset_mock()

        mock_bld_slot_mgr.reset_mock()

        # Tasks expected to be added for resize to the same host, no BDMS,
        # and no power_on
        expected = [
            'resize_lpar',
        ]
        with fx.DriverTaskFlow() as taskflow_fix:
            self.drv.finish_migration(
                'context', mig_same_host, self.inst, disk_info, 'network_info',
                powervm.IMAGE1, 'resize_instance', power_on=False)
            taskflow_fix.assert_tasks_added(self, expected)
        # Don't need the slot manager on a pure resize (no BDMs and same host)
        mock_bld_slot_mgr.assert_not_called()
        self.san_lpar_name.assert_called_with('resize_' + self.inst.name)

    @mock.patch('nova_powervm.virt.powervm.vm.power_on')
    @mock.patch('nova_powervm.virt.powervm.vm.update')
    @mock.patch('nova_powervm.virt.powervm.vm.power_off')
    def test_finish_revert_migration(self, mock_off, mock_update, mock_on):
        """Validates that the finish revert migration works."""
        mock_flavor = mock.Mock()
        mock_instance = mock.Mock(flavor=mock_flavor)

        # Validate with a default power on
        self.drv.finish_revert_migration('context', mock_instance, None)

        # Asserts
        mock_off.assert_called_once_with(
            self.apt, mock_instance, self.drv.host_uuid)
        mock_update.assert_called_once_with(
            self.apt, self.drv.host_wrapper, mock_instance, mock_flavor)
        mock_on.assert_called_once_with(
            self.apt, mock_instance, self.drv.host_uuid)

    @mock.patch('nova_powervm.virt.powervm.vm.power_on')
    @mock.patch('nova_powervm.virt.powervm.vm.update')
    @mock.patch('nova_powervm.virt.powervm.vm.power_off')
    def test_finish_revert_migration_no_power_on(self, mock_off, mock_update,
                                                 mock_on):
        """Validates that the finish revert migration works, no power_on."""
        mock_flavor = mock.Mock()
        mock_instance = mock.Mock(flavor=mock_flavor)

        # Validate with power_on set to false
        self.drv.finish_revert_migration(
            'context', mock_instance, None, power_on=False)

        # Asserts
        mock_off.assert_called_once_with(
            self.apt, mock_instance, self.drv.host_uuid)
        mock_update.assert_called_once_with(
            self.apt, self.drv.host_wrapper, mock_instance, mock_flavor)
        self.assertFalse(mock_on.called)

    @mock.patch('nova_powervm.virt.powervm.vm')
    @mock.patch('nova_powervm.virt.powervm.tasks.vm.vm')
    @mock.patch('nova_powervm.virt.powervm.tasks.vm.power')
    def test_rescue(self, mock_task_pwr, mock_task_vm, mock_dvr_vm):
        """Validates the PowerVM driver rescue operation."""
        with mock.patch.object(self.drv, 'disk_dvr') as mock_disk_dvr:
            # Invoke the method.
            self.drv.rescue('context', self.inst, mock.MagicMock(),
                            powervm.TEST_IMAGE1, 'rescue_psswd')

        self.assertTrue(mock_task_vm.power_off.called)
        self.assertTrue(mock_disk_dvr.create_disk_from_image.called)
        self.assertTrue(mock_disk_dvr.connect_disk.called)
        self.assertTrue(mock_task_pwr.power_on.called)
        self.assertFalse(mock_task_pwr.power_on.call_args[1]['synchronous'])

    @mock.patch('nova_powervm.virt.powervm.driver.vm')
    @mock.patch('nova_powervm.virt.powervm.tasks.vm.vm')
    @mock.patch('nova_powervm.virt.powervm.tasks.vm.power')
    def test_unrescue(self, mock_task_pwr, mock_task_vm, mock_dvr_vm):
        """Validates the PowerVM driver rescue operation."""
        with mock.patch.object(self.drv, 'disk_dvr') as mock_disk_dvr:
            # Invoke the method.
            self.drv.unrescue(self.inst, 'network_info')

        self.assertTrue(mock_task_vm.power_off.called)
        self.assertTrue(mock_disk_dvr.disconnect_image_disk.called)
        self.assertTrue(mock_disk_dvr.delete_disks.called)
        self.assertTrue(mock_task_pwr.power_on.called)
        self.assertFalse(mock_task_pwr.power_on.call_args[1]['synchronous'])

    @mock.patch.object(driver, 'LOG')
    def test_log_op(self, mock_log):
        """Validates the log_operations."""
        self.drv._log_operation('fake_op', self.inst)
        entry = (r'Operation: %(op)s. Virtual machine display '
                 'name: %(display_name)s, name: %(name)s, '
                 'UUID: %(uuid)s')
        msg_dict = {'uuid': 'b3c04455-a435-499d-ac81-371d2a2d334f',
                    'display_name': u'Fake Instance',
                    'name': 'instance-00000001',
                    'op': 'fake_op'}
        mock_log.info.assert_called_with(entry, msg_dict)

    def test_host_resources(self):
        # Mock methods not currently under test
        with mock.patch.object(self.apt, 'read') as mock_read:
            mock_read.return_value = None
            # Run the actual test
            stats = self.drv.get_available_resource('nodename')
            self.assertIsNotNone(stats)

        # Check for the presence of fields added to host stats
        fields = ('local_gb', 'local_gb_used')

        for fld in fields:
            value = stats.get(fld, None)
            self.assertIsNotNone(value)

    @mock.patch('nova_powervm.virt.powervm.vif.plug_secure_rmc_vif')
    @mock.patch('nova_powervm.virt.powervm.vif.get_secure_rmc_vswitch')
    @mock.patch('nova_powervm.virt.powervm.vif.plug')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper')
    @mock.patch('nova_powervm.virt.powervm.slot.build_slot_mgr')
    def test_plug_vifs(
        self, mock_bld_slot_mgr, mock_wrap, mock_vm_get, mock_plug_vif,
        mock_get_rmc_vswitch, mock_plug_rmc_vif):
        # Mock up the CNA response
        cnas = [mock.MagicMock(), mock.MagicMock()]
        cnas[0].mac = 'AABBCCDDEEFF'
        cnas[0].vswitch_uri = 'fake_uri'
        cnas[1].mac = 'AABBCCDDEE11'
        cnas[1].vswitch_uri = 'fake_mgmt_uri'
        mock_vm_get.return_value = cnas

        mock_lpar_wrapper = mock.MagicMock()
        mock_lpar_wrapper.can_modify_io = mock.MagicMock(
            return_value=(True, None))
        mock_wrap.return_value = mock_lpar_wrapper

        # Mock up the network info.  They get sanitized to upper case.
        net_info = [
            {'address': 'aa:bb:cc:dd:ee:ff'},
            {'address': 'aa:bb:cc:dd:ee:22'}
        ]

        # Mock up the rmc vswitch
        vswitch_w = mock.MagicMock()
        vswitch_w.href = 'fake_mgmt_uri'
        mock_get_rmc_vswitch.return_value = vswitch_w

        # Run method
        self.drv.plug_vifs(self.inst, net_info)

        mock_bld_slot_mgr.assert_called_once_with(self.inst,
                                                  self.drv.store_api)

        # The create should have only been called once.  The other was already
        # existing.
        mock_plug_vif.assert_called_once_with(
            self.drv.adapter, self.drv.host_uuid, self.inst, net_info[1],
            mock_bld_slot_mgr.return_value)
        mock_bld_slot_mgr.return_value.save.assert_called_once_with()
        self.assertEqual(0, mock_plug_rmc_vif.call_count)

    @mock.patch('nova_powervm.virt.powervm.tasks.vm.Get')
    def test_plug_vif_failures(self, mock_vm):
        # Test instance not found handling
        mock_vm.execute.side_effect = exc.InstanceNotFound(
            instance_id=self.inst)

        # Run method
        self.assertRaises(exc.VirtualInterfacePlugException,
                          self.drv.plug_vifs, self.inst, {})

        # Test a random Exception
        mock_vm.execute.side_effect = ValueError()

        # Run method
        self.assertRaises(exc.VirtualInterfacePlugException,
                          self.drv.plug_vifs, self.inst, {})

    @mock.patch('nova_powervm.virt.powervm.vif.unplug')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper')
    @mock.patch('nova_powervm.virt.powervm.slot.build_slot_mgr')
    def test_unplug_vifs(self, mock_bld_slot_mgr, mock_wrap, mock_vm_get,
                         mock_unplug_vif):
        # Mock up the CNA response
        cnas = [mock.MagicMock(), mock.MagicMock()]
        cnas[0].mac = 'AABBCCDDEEFF'
        cnas[0].vswitch_uri = 'fake_uri'
        cnas[1].mac = 'AABBCCDDEE11'
        cnas[1].vswitch_uri = 'fake_mgmt_uri'
        mock_vm_get.return_value = cnas

        mock_lpar_wrapper = mock.MagicMock()
        mock_lpar_wrapper.can_modify_io = mock.MagicMock(
            return_value=(True, None))
        mock_wrap.return_value = mock_lpar_wrapper

        # Mock up the network info.  They get sanitized to upper case.
        net_info = [
            {'address': 'aa:bb:cc:dd:ee:ff'},
            {'address': 'aa:bb:cc:dd:ee:22'}
        ]

        # Run method
        self.drv.unplug_vifs(self.inst, net_info)

        mock_bld_slot_mgr.assert_called_once_with(self.inst,
                                                  self.drv.store_api)

        # The create should have only been called once.  The other was already
        # existing.
        mock_unplug_vif.assert_has_calls(
            [mock.call(self.drv.adapter, self.drv.host_uuid, self.inst,
                       net_inf, mock_bld_slot_mgr.return_value,
                       cna_w_list=cnas) for net_inf in net_info])
        mock_bld_slot_mgr.return_value.save.assert_called_once_with()

    @mock.patch('nova_powervm.virt.powervm.tasks.vm.Get.execute')
    def test_unplug_vif_failures(self, mock_vm):
        # Test instance not found handling
        mock_vm.side_effect = exc.InstanceNotFound(
            instance_id=self.inst)

        # Run method
        self.drv.unplug_vifs(self.inst, {})
        self.assertEqual(1, mock_vm.call_count)

    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper')
    def test_unplug_vif_failures_httperror(self, mock_wrap):
        # Test instance not found handling
        mock_wrap.side_effect = exc.InstanceNotFound(
            instance_id=self.inst.name)

        # Backing API: Instance does not exist
        # Nova Response: No exceptions should be raised.
        self.drv.unplug_vifs(self.inst, {})
        self.assertEqual(1, mock_wrap.call_count)

    def test_extract_bdm(self):
        """Tests the _extract_bdm method."""
        self.assertEqual([], self.drv._extract_bdm(None))
        self.assertEqual([], self.drv._extract_bdm({'fake': 'val'}))

        fake_bdi = {'block_device_mapping': ['content']}
        self.assertListEqual(['content'], self.drv._extract_bdm(fake_bdi))

    def test_get_host_ip_addr(self):
        self.assertEqual(self.drv.get_host_ip_addr(), '127.0.0.1')

    @mock.patch('nova_powervm.virt.powervm.driver.LOG.warning')
    @mock.patch('nova.compute.utils.get_machine_ips')
    def test_get_host_ip_addr_failure(self, mock_ips, mock_log):
        mock_ips.return_value = ['1.1.1.1']
        self.drv.get_host_ip_addr()
        mock_log.assert_called_once_with(u'my_ip address (%(my_ip)s) was '
                                         u'not found on any of the '
                                         u'interfaces: %(ifaces)s',
                                         {'ifaces': '1.1.1.1',
                                          'my_ip': mock.ANY})

    def test_shared_stg_calls(self):
        data = self.drv.check_instance_shared_storage_local('context', 'inst')
        self.assertTrue(
            self.drv.disk_dvr.check_instance_shared_storage_local.called)

        self.drv.check_instance_shared_storage_remote('context', data)
        self.assertTrue(
            self.drv.disk_dvr.check_instance_shared_storage_remote.called)

        self.drv.check_instance_shared_storage_cleanup('context', data)
        self.assertTrue(
            self.drv.disk_dvr.check_instance_shared_storage_cleanup.called)

    @mock.patch('pypowervm.tasks.power.power_on')
    @mock.patch('pypowervm.tasks.power.power_off')
    def test_reboot(self, mock_pwroff, mock_pwron):
        entry = mock.Mock()
        self.get_inst_wrap.return_value = entry

        # VM is in 'not activated' state
        # Validate SOFT vs HARD and power_on called with each.
        entry.state = pvm_bp.LPARState.NOT_ACTIVATED
        self.assertTrue(self.drv.reboot('context', self.inst, None, 'SOFT'))
        # Make sure power off is not called
        self.assertEqual(0, mock_pwroff.call_count)
        mock_pwron.assert_called_with(entry, self.drv.host_uuid)
        self.assertTrue(self.drv.reboot('context', self.inst, None, 'HARD'))
        # Make sure power off is not called
        self.assertEqual(0, mock_pwroff.call_count)
        self.assertEqual(2, mock_pwron.call_count)
        mock_pwron.assert_called_with(entry, self.drv.host_uuid)

        # VM is not in 'not activated' state
        # reset mock_pwron
        mock_pwron.reset_mock()
        entry.state = 'whatever'
        self.assertTrue(self.drv.reboot('context', self.inst, None, 'SOFT'))
        mock_pwroff.assert_called_with(entry, self.drv.host_uuid,
                                       restart=True,
                                       force_immediate=False)
        self.assertEqual(0, mock_pwron.call_count)
        self.assertTrue(self.drv.reboot('context', self.inst, None, 'HARD'))
        mock_pwroff.assert_called_with(entry, self.drv.host_uuid,
                                       restart=True,
                                       force_immediate=True)
        self.assertEqual(0, mock_pwron.call_count)

        # If power_off raises an exception, power_on is not called, and the
        # exception percolates up.
        entry.state = 'whatever'
        mock_pwroff.side_effect = pvm_exc.VMPowerOffFailure(lpar_nm='lpar',
                                                            reason='reason')
        self.assertRaises(pvm_exc.VMPowerOffFailure, self.drv.reboot,
                          'context', self.inst, None, 'HARD')

        # If power_on raises an exception, it percolates up.
        entry.state = pvm_bp.LPARState.NOT_ACTIVATED
        mock_pwron.side_effect = pvm_exc.VMPowerOnFailure(lpar_nm='lpar',
                                                          reason='reason')
        self.assertRaises(pvm_exc.VMPowerOnFailure, self.drv.reboot, 'context',
                          self.inst, None, 'SOFT')

    @mock.patch('pypowervm.tasks.vterm.open_remotable_vnc_vterm')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    def test_get_vnc_console(self, mock_uuid, mock_vterm):
        # Mock response
        mock_vterm.return_value = '10'
        mock_uuid.return_value = 'uuid'

        # Invoke
        resp = self.drv.get_vnc_console(mock.ANY, self.inst)

        # Validate
        self.assertEqual('127.0.0.1', resp.host)
        self.assertEqual('10', resp.port)
        self.assertEqual('uuid', resp.internal_access_path)

        mock_vterm.assert_called_once_with(
            mock.ANY, 'uuid', mock.ANY, vnc_path='uuid')

    @staticmethod
    def _fake_bdms():
        def _fake_bdm(volume_id, target_lun):
            connection_info = {'driver_volume_type': 'fibre_channel',
                               'data': {'volume_id': volume_id,
                                        'target_lun': target_lun,
                                        'initiator_target_map':
                                        {'21000024F5': ['50050768']}}}
            mapping_dict = {'source_type': 'volume', 'volume_id': volume_id,
                            'destination_type': 'volume',
                            'connection_info':
                                jsonutils.dumps(connection_info),
                            }
            bdm_dict = nova_block_device.BlockDeviceDict(mapping_dict)
            bdm_obj = bdmobj.BlockDeviceMapping(**bdm_dict)

            return nova_virt_bdm.DriverVolumeBlockDevice(bdm_obj)

        bdm_list = [_fake_bdm('fake_vol1', 0), _fake_bdm('fake_vol2', 1)]
        block_device_info = {'block_device_mapping': bdm_list}

        return block_device_info

    @mock.patch('nova_powervm.virt.powervm.tasks.image.UpdateTaskState.'
                'execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.storage.InstanceDiskToMgmt.'
                'execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.image.StreamToGlance.execute')
    @mock.patch('nova_powervm.virt.powervm.tasks.storage.'
                'RemoveInstanceDiskFromMgmt.execute')
    def test_snapshot(self, mock_rm, mock_stream, mock_conn, mock_update):
        mock_conn.return_value = 'stg_elem', 'vios_wrap', 'disk_path'
        self.drv.snapshot('context', self.inst, 'image_id',
                          'update_task_state')
        self.assertEqual(2, mock_update.call_count)
        self.assertEqual(1, mock_conn.call_count)
        mock_stream.assert_called_with(disk_path='disk_path')
        mock_rm.assert_called_with(stg_elem='stg_elem', vios_wrap='vios_wrap',
                                   disk_path='disk_path')

    @mock.patch('nova_powervm.virt.powervm.live_migration.LiveMigrationDest')
    def test_can_migrate_dest(self, mock_lpm):
        mock_lpm.return_value.check_destination.return_value = 'dest_data'
        dest_data = self.drv.check_can_live_migrate_destination(
            'context', mock.Mock(), 'src_compute_info', 'dst_compute_info')
        self.assertEqual('dest_data', dest_data)

    def test_can_live_mig_dest_clnup(self):
        self.drv.check_can_live_migrate_destination_cleanup(
            'context', 'dest_data')

    @mock.patch('nova_powervm.virt.powervm.live_migration.LiveMigrationSrc')
    def test_can_live_mig_src(self, mock_lpm):
        mock_lpm.return_value.check_source.return_value = (
            'src_data')
        src_data = self.drv.check_can_live_migrate_source(
            'context', mock.Mock(), 'dest_check_data')
        self.assertEqual('src_data', src_data)

    def test_pre_live_migr(self):
        block_device_info = self._fake_bdms()
        resp = self.drv.pre_live_migration(
            'context', self.lpm_inst, block_device_info, 'network_info',
            'disk_info', migrate_data='migrate_data')
        self.assertIsNotNone(resp)

    def test_live_migration(self):
        mock_post_meth = mock.Mock()
        mock_rec_meth = mock.Mock()

        # Good path
        self.drv.live_migration(
            'context', self.lpm_inst, 'dest', mock_post_meth, mock_rec_meth,
            'block_mig', 'migrate_data')

        mock_post_meth.assert_called_once_with(
            'context', self.lpm_inst, 'dest', mock.ANY, mock.ANY)
        self.assertEqual(0, mock_rec_meth.call_count)

        # Abort invocation path
        self._setup_lpm()
        mock_post_meth.reset_mock()
        mock_kwargs = {'operation_name': 'op', 'seconds': 10}
        self.lpm.live_migration.side_effect = (
            pvm_exc.JobRequestTimedOut(**mock_kwargs))
        self.assertRaises(
            lpm.LiveMigrationFailed, self.drv.live_migration,
            'context', self.lpm_inst, 'dest', mock_post_meth, mock_rec_meth,
            'block_mig', 'migrate_data')
        self.lpm.migration_abort.assert_called_once_with()
        mock_rec_meth.assert_called_once_with(
            'context', self.lpm_inst, 'dest', mock.ANY, mock.ANY)
        self.lpm.rollback_live_migration.assert_called_once_with('context')
        self.assertEqual(0, mock_post_meth.call_count)

        # Exception path
        self._setup_lpm()
        mock_post_meth.reset_mock()
        mock_rec_meth.reset_mock()
        self.lpm.live_migration.side_effect = ValueError()
        self.assertRaises(
            lpm.LiveMigrationFailed, self.drv.live_migration,
            'context', self.lpm_inst, 'dest', mock_post_meth, mock_rec_meth,
            'block_mig', 'migrate_data')
        mock_rec_meth.assert_called_once_with(
            'context', self.lpm_inst, 'dest', mock.ANY, mock.ANY)
        self.lpm.rollback_live_migration.assert_called_once_with('context')
        self.assertEqual(0, mock_post_meth.call_count)

        # Ensure we get LiveMigrationFailed even if recovery fails.
        self._setup_lpm()
        mock_post_meth.reset_mock()
        mock_rec_meth.reset_mock()
        self.lpm.live_migration.side_effect = ValueError()
        # Cause the recovery method to fail with an exception.
        mock_rec_meth.side_effect = ValueError()
        self.assertRaises(
            lpm.LiveMigrationFailed, self.drv.live_migration,
            'context', self.lpm_inst, 'dest', mock_post_meth, mock_rec_meth,
            'block_mig', 'migrate_data')
        mock_rec_meth.assert_called_once_with(
            'context', self.lpm_inst, 'dest', mock.ANY, mock.ANY)
        self.lpm.rollback_live_migration.assert_called_once_with('context')
        self.assertEqual(0, mock_post_meth.call_count)

    def test_rollbk_lpm_dest(self):
        self.drv.rollback_live_migration_at_destination(
            'context', self.lpm_inst, 'network_info', 'block_device_info')
        self.assertRaises(
            KeyError, lambda: self.drv.live_migrations[self.lpm_inst.uuid])

    def test_post_live_mig(self):
        self.drv.post_live_migration('context', self.lpm_inst, None)
        self.lpm.post_live_migration.assert_called_once_with([], None)

    def test_post_live_mig_src(self):
        self.drv.post_live_migration_at_source('context', self.lpm_inst,
                                               'network_info')
        self.lpm.post_live_migration_at_source.assert_called_once_with(
            'network_info')

    def test_post_live_mig_dest(self):
        self.drv.post_live_migration_at_destination(
            'context', self.lpm_inst, 'network_info')
        self.lpm.post_live_migration_at_destination.assert_called_once_with(
            'network_info', [])

    @mock.patch('pypowervm.tasks.memory.calculate_memory_overhead_on_host')
    def test_estimate_instance_overhead(self, mock_calc_over):
        mock_calc_over.return_value = ('2048', '96')

        inst_info = self.inst.get_flavor()
        inst_info.extra_specs = {}
        overhead = self.drv.estimate_instance_overhead(inst_info)
        self.assertEqual({'memory_mb': '2048'}, overhead)

        # Make sure the cache works
        mock_calc_over.reset_mock()
        overhead = self.drv.estimate_instance_overhead(inst_info)
        self.assertEqual({'memory_mb': '2048'}, overhead)
        mock_calc_over.assert_not_called()

        # Reset the cache every time from now on
        self.drv._inst_overhead_cache = {}

        # Flavor having extra_specs
        inst_info.extra_specs = {'powervm:max_mem': 4096}
        overhead = self.drv.estimate_instance_overhead(inst_info)
        mock_calc_over.assert_called_with(self.apt, self.drv.host_uuid,
                                          {'max_mem': 4096})
        self.assertEqual({'memory_mb': '2048'}, overhead)

        self.drv._inst_overhead_cache = {}

        # Test when instance passed is dict
        inst_info = obj_base.obj_to_primitive(inst_info)
        overhead = self.drv.estimate_instance_overhead(inst_info)
        self.assertEqual({'memory_mb': '2048'}, overhead)

        self.drv._inst_overhead_cache = {}

        # When instance_info is None
        overhead = self.drv.estimate_instance_overhead(None)
        self.assertEqual({'memory_mb': 0}, overhead)

        self.drv._inst_overhead_cache = {}

        # Test when instance Object is passed
        overhead = self.drv.estimate_instance_overhead(self.inst)
        self.assertEqual({'memory_mb': '2048'}, overhead)

    def test_vol_drv_iter(self):
        block_device_info = self._fake_bdms()
        vol_adpt = mock.Mock()

        def _get_results(block_device_info=None, bdms=None):
            # Patch so we get the same mock back each time.
            with mock.patch.object(self.drv, '_get_inst_vol_adpt',
                                   return_value=vol_adpt):
                return [
                    (bdm, vol_drv) for bdm, vol_drv in self.drv._vol_drv_iter(
                        'context', self.inst,
                        block_device_info=block_device_info, bdms=bdms)]

        def validate(results):
            # For each good call, we should get back two bdms / vol_adpt
            self.assertEqual(
                'fake_vol1',
                results[0][0]['connection_info']['data']['volume_id'])
            self.assertEqual(vol_adpt, results[0][1])
            self.assertEqual(
                'fake_vol2',
                results[1][0]['connection_info']['data']['volume_id'])
            self.assertEqual(vol_adpt, results[1][1])

        # Send block device info
        results = _get_results(block_device_info=block_device_info)
        validate(results)
        # Same results with bdms
        results = _get_results(bdms=self.drv._extract_bdm(block_device_info))
        validate(results)
        # Empty bdms
        self.assertEqual([], _get_results(bdms=[]))

    def test_build_vol_drivers(self):
        # This utility just returns a list of drivers from the _vol_drv_iter()
        # iterator so mock it and ensure the drivers are returned.
        vals = [('bdm0', 'drv0'), ('bdm1', 'drv1')]
        with mock.patch.object(self.drv, '_vol_drv_iter', return_value=vals):
            drivers = self.drv._build_vol_drivers('context', 'instance')

        self.assertEqual(['drv0', 'drv1'], drivers)

    @mock.patch.object(objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(virt_driver, 'get_block_device_info')
    def test_get_block_device_info(self, mock_bk_dev, mock_bdml):
        mock_bk_dev.return_value = 'info'
        self.assertEqual('info',
                         self.drv._get_block_device_info('ctx', self.inst))


class TestNovaEventHandler(test.TestCase):
    def setUp(self):
        super(TestNovaEventHandler, self).setUp()
        self.mock_driver = mock.Mock()
        self.handler = driver.NovaEventHandler(self.mock_driver)

    @mock.patch.object(vm, 'get_instance')
    @mock.patch.object(vm, 'get_vm_qp')
    def test_events(self, mock_qprops, mock_get_inst):
        # Test events
        event_data = [
            {
                'EventType': 'NEW_CLIENT',
                'EventData': '',
                'EventID': '1452692619554',
                'EventDetail': '',
            },
            {
                'EventType': 'MODIFY_URI',
                'EventData': 'http://localhost:12080/rest/api/uom/Managed'
                             'System/c889bf0d-9996-33ac-84c5-d16727083a77',
                'EventID': '1452692619555',
                'EventDetail': 'Other',
            },
            {
                'EventType': 'MODIFY_URI',
                'EventData': 'http://localhost:12080/rest/api/uom/Managed'
                             'System/c889bf0d-9996-33ac-84c5-d16727083a77/'
                             'LogicalPartition/794654F5-B6E9-4A51-BEC2-'
                             'A73E41EAA938',
                'EventID': '1452692619563',
                'EventDetail': 'ReferenceCode,Other',
            },
            {
                'EventType': 'MODIFY_URI',
                'EventData': 'http://localhost:12080/rest/api/uom/Managed'
                             'System/c889bf0d-9996-33ac-84c5-d16727083a77/'
                             'LogicalPartition/794654F5-B6E9-4A51-BEC2-'
                             'A73E41EAA938',
                'EventID': '1452692619566',
                'EventDetail': 'RMCState,PartitionState,Other',
            },
            {
                'EventType': 'MODIFY_URI',
                'EventData': 'http://localhost:12080/rest/api/uom/Managed'
                             'System/c889bf0d-9996-33ac-84c5-d16727083a77/'
                             'LogicalPartition/794654F5-B6E9-4A51-BEC2-'
                             'A73E41EAA938',
                'EventID': '1452692619566',
                'EventDetail': 'NVRAM',
            },
        ]

        mock_qprops.return_value = pvm_bp.LPARState.RUNNING
        mock_get_inst.return_value = powervm.TEST_INST1

        self.handler.process(event_data)
        self.assertTrue(self.mock_driver.emit_event.called)
        self.assertTrue(self.mock_driver.nvram_mgr.store.called)
