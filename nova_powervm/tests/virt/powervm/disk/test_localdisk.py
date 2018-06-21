# Copyright IBM Corp. and contributors
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

import fixtures
import mock

from nova import exception as nova_exc
from nova import test
from nova.tests import uuidsentinel as uuids
from pypowervm import const as pvm_const
from pypowervm.tasks import storage as tsk_stor
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.wrappers import storage as pvm_stor
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.tests.virt import powervm
from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm.disk import driver as disk_dvr
from nova_powervm.virt.powervm.disk import localdisk as ld
from nova_powervm.virt.powervm import exception as npvmex
from nova_powervm.virt.powervm import vm


class TestLocalDisk(test.NoDBTestCase):
    """Unit Tests for the LocalDisk storage driver."""

    def setUp(self):
        super(TestLocalDisk, self).setUp()

        self.apt = self.useFixture(pvm_fx.AdapterFx()).adpt

        # Set up mock for internal VIOS.get()s
        self.mock_vios_get = self.useFixture(fixtures.MockPatch(
            'pypowervm.wrappers.virtual_io_server.VIOS',
            autospec=True)).mock.get
        # The mock VIOS needs to have scsi_mappings as a list.  Internals are
        # set by individual test cases as needed.
        smaps = [mock.Mock()]
        self.vio_to_vg = mock.Mock(spec=pvm_vios.VIOS, scsi_mappings=smaps,
                                   uuid='vios-uuid')
        # For our tests, we want find_maps to return the mocked list of scsi
        # mappings in our mocked VIOS.
        self.mock_find_maps = self.useFixture(fixtures.MockPatch(
            'pypowervm.tasks.scsi_mapper.find_maps', autospec=True)).mock
        self.mock_find_maps.return_value = smaps

        # Set up for the mocks for get_ls
        self.mock_find_vg = self.useFixture(fixtures.MockPatch(
            'pypowervm.tasks.storage.find_vg', autospec=True)).mock
        self.vg_uuid = uuids.vg_uuid
        self.vg = mock.Mock(spec=pvm_stor.VG, uuid=self.vg_uuid)
        self.mock_find_vg.return_value = (self.vio_to_vg, self.vg)

        # Return the mgmt uuid
        self.mgmt_uuid = self.useFixture(fixtures.MockPatch(
            'nova_powervm.virt.powervm.mgmt.mgmt_uuid')).mock
        self.mgmt_uuid.return_value = 'mp_uuid'

        self.flags(volume_group_name='fakevg', group='powervm')

    @staticmethod
    def get_ls(adpt):
        return ld.LocalStorage(adpt, 'host_uuid')

    def test_init(self):
        local = self.get_ls(self.apt)
        self.mock_find_vg.assert_called_once_with(self.apt, 'fakevg')
        self.assertEqual('vios-uuid', local._vios_uuid)
        self.assertEqual(self.vg_uuid, local.vg_uuid)
        self.assertEqual(self.apt, local.adapter)
        self.assertEqual('host_uuid', local.host_uuid)

    @mock.patch('pypowervm.tasks.storage.crt_copy_vdisk', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage.'
                '_get_or_upload_image')
    def test_create_disk_from_image(self, mock_get_image, mock_copy):
        mock_copy.return_value = 'vdisk'
        inst = mock.Mock()
        inst.configure_mock(name='Inst Name',
                            uuid='d5065c2c-ac43-3fa6-af32-ea84a3960291',
                            flavor=mock.Mock(root_gb=20))
        mock_image = mock.MagicMock()
        mock_image.name = 'cached_image'
        mock_get_image.return_value = mock_image

        vdisk = self.get_ls(self.apt).create_disk_from_image(
            None, inst, powervm.TEST_IMAGE1)
        self.assertEqual('vdisk', vdisk)

        mock_get_image.reset_mock()
        exception = Exception
        mock_get_image.side_effect = exception
        with mock.patch('time.sleep', autospec=True) as mock_sleep:
            self.assertRaises(exception,
                              self.get_ls(self.apt).create_disk_from_image,
                              None, inst, powervm.TEST_IMAGE1)
        self.assertEqual(mock_get_image.call_count, 4)
        self.assertEqual(3, mock_sleep.call_count)

    @mock.patch('pypowervm.tasks.storage.upload_new_vdisk', autospec=True)
    @mock.patch('nova.image.api.API.download')
    @mock.patch('nova_powervm.virt.powervm.disk.driver.IterableToFileAdapter')
    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage.'
                '_get_vg_wrap')
    def test_get_or_upload_image(self, mock_get_vg, mock_it2f, mock_img_api,
                                 mock_upload_vdisk):
        mock_wrapper = mock.Mock()
        mock_wrapper.configure_mock(name='vg_name', virtual_disks=[])
        mock_get_vg.return_value = mock_wrapper
        local = self.get_ls(self.apt)

        self.assertEqual(
            mock_upload_vdisk.return_value[0].udid,
            local._get_or_upload_image('ctx', powervm.TEST_IMAGE1))

        # Make sure the upload was invoked properly
        mock_upload_vdisk.assert_called_once_with(
            self.apt, 'vios-uuid', self.vg_uuid, mock_it2f.return_value,
            'i_3e865d14_8c1e', powervm.TEST_IMAGE1.size,
            d_size=powervm.TEST_IMAGE1.size,
            upload_type=tsk_stor.UploadType.IO_STREAM,
            file_format=powervm.TEST_IMAGE1.disk_format)
        mock_it2f.assert_called_once_with(mock_img_api.return_value)
        mock_img_api.assert_called_once_with('ctx', powervm.TEST_IMAGE1.id)

        mock_img_api.reset_mock()
        mock_upload_vdisk.reset_mock()

        # Now ensure upload_new_vdisk isn't called if the vdisk already exists.
        mock_image = mock.MagicMock()
        mock_image.configure_mock(name='i_3e865d14_8c1e', udid='udid')
        mock_instance = mock.MagicMock()
        mock_instance.configure_mock(name='b_Inst_Nam_d506')
        mock_wrapper.virtual_disks = [mock_instance, mock_image]
        mock_get_vg.return_value = mock_wrapper
        self.assertEqual(
            mock_image.udid,
            local._get_or_upload_image('ctx', powervm.TEST_IMAGE1))
        mock_img_api.assert_not_called()
        self.assertEqual(0, mock_upload_vdisk.call_count)

    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage.'
                '_get_vg_wrap')
    def test_capacity(self, mock_vg):
        """Tests the capacity methods."""
        local = self.get_ls(self.apt)
        mock_vg.return_value = mock.Mock(
            capacity='5120', available_size='2048')
        self.assertEqual(5120.0, local.capacity)
        self.assertEqual(3072.0, local.capacity_used)

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps', autospec=True)
    @mock.patch('pypowervm.tasks.partition.get_active_vioses', autospec=True)
    def test_disconnect_disk(self, mock_active_vioses, mock_rm_maps):
        # vio_to_vg is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTaskFx and FeedTask.
        feed = [self.vio_to_vg]
        mock_active_vioses.return_value = feed

        # The mock return values
        mock_rm_maps.return_value = True

        # Create the feed task
        local = self.get_ls(self.apt)
        inst = mock.Mock(uuid=fx.FAKE_INST_UUID)

        # As initialized above, remove_maps returns True to trigger update.
        local.disconnect_disk(inst, stg_ftsk=None,
                              disk_type=[disk_dvr.DiskType.BOOT])
        self.assertEqual(1, mock_rm_maps.call_count)
        self.assertEqual(1, self.vio_to_vg.update.call_count)
        mock_rm_maps.assert_called_once_with(feed[0], fx.FAKE_INST_UUID_PVM,
                                             match_func=mock.ANY)

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps', autospec=True)
    @mock.patch('pypowervm.tasks.partition.get_active_vioses', autospec=True)
    def test_disconnect_disk_no_update(self, mock_active_vioses, mock_rm_maps):
        # vio_to_vg is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTaskFx and FeedTask.
        feed = [self.vio_to_vg]
        mock_active_vioses.return_value = feed

        # The mock return values
        mock_rm_maps.return_value = False

        # Create the feed task
        local = self.get_ls(self.apt)
        inst = mock.Mock(uuid=fx.FAKE_INST_UUID)
        # As initialized above, remove_maps returns True to trigger update.
        local.disconnect_disk(inst, stg_ftsk=None,
                              disk_type=[disk_dvr.DiskType.BOOT])
        self.assertEqual(1, mock_rm_maps.call_count)
        self.vio_to_vg.update.assert_not_called()
        mock_rm_maps.assert_called_once_with(feed[0], fx.FAKE_INST_UUID_PVM,
                                             match_func=mock.ANY)

    @mock.patch('pypowervm.tasks.scsi_mapper.gen_match_func', autospec=True)
    def test_disconnect_disk_disktype(self, mock_match_func):
        """Ensures that the match function passes in the right prefix."""
        # Set up the mock data.
        inst = mock.Mock(uuid=fx.FAKE_INST_UUID)
        mock_match_func.return_value = 'test'

        # Invoke
        local = self.get_ls(self.apt)
        local.disconnect_disk(inst, stg_ftsk=mock.MagicMock(),
                              disk_type=[disk_dvr.DiskType.BOOT])

        # Make sure the find maps is invoked once.
        self.mock_find_maps.assert_called_once_with(
            mock.ANY, client_lpar_id=fx.FAKE_INST_UUID_PVM, match_func='test')

        # Make sure the matching function is generated with the right disk type
        mock_match_func.assert_called_once_with(
            pvm_stor.VDisk, prefixes=[disk_dvr.DiskType.BOOT])

    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping',
                autospec=True)
    @mock.patch('pypowervm.tasks.scsi_mapper.add_map', autospec=True)
    @mock.patch('pypowervm.tasks.partition.get_active_vioses', autospec=True)
    def test_connect_disk(self, mock_active_vioses, mock_add_map,
                          mock_build_map):
        # vio_to_vg is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTask.
        feed = [self.vio_to_vg]
        mock_active_vioses.return_value = feed

        # The mock return values
        mock_add_map.return_value = True
        mock_build_map.return_value = 'fake_map'

        # Need the driver to return the actual UUID of the VIOS in the feed,
        # to match the FeedTask.
        local = self.get_ls(self.apt)
        inst = mock.Mock(uuid=fx.FAKE_INST_UUID)
        lpar_uuid = vm.get_pvm_uuid(inst)
        mock_disk = mock.Mock()
        # As initialized above, remove_maps returns True to trigger update.
        local.connect_disk(inst, mock_disk, stg_ftsk=None)
        self.assertEqual(1, mock_add_map.call_count)
        mock_build_map.assert_called_once_with(
            'host_uuid', self.vio_to_vg, lpar_uuid, mock_disk)
        mock_add_map.assert_called_once_with(feed[0], 'fake_map')
        self.assertEqual(1, self.vio_to_vg.update.call_count)

    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping',
                autospec=True)
    @mock.patch('pypowervm.tasks.scsi_mapper.add_map', autospec=True)
    @mock.patch('pypowervm.tasks.partition.get_active_vioses', autospec=True)
    def test_connect_disk_no_update(self, mock_active_vioses, mock_add_map,
                                    mock_build_map):
        # vio_to_vg is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTask.
        feed = [self.vio_to_vg]
        mock_active_vioses.return_value = feed

        # The mock return values
        mock_add_map.return_value = False
        mock_build_map.return_value = 'fake_map'

        # Need the driver to return the actual UUID of the VIOS in the feed,
        # to match the FeedTask.
        local = self.get_ls(self.apt)
        inst = mock.Mock(uuid=fx.FAKE_INST_UUID)

        # As initialized above, remove_maps returns True to trigger update.
        local.connect_disk(inst, mock.Mock(), stg_ftsk=None)
        self.assertEqual(1, mock_add_map.call_count)
        mock_add_map.assert_called_once_with(feed[0], 'fake_map')
        self.vio_to_vg.update.assert_not_called()

    @mock.patch('pypowervm.wrappers.storage.VG.update', new=mock.Mock())
    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage.'
                '_get_vg_wrap')
    def test_delete_disks(self, mock_vg):
        # Mocks
        self.apt.side_effect = [mock.Mock()]

        mock_remove = mock.MagicMock()
        mock_remove.name = 'disk'

        mock_wrapper = mock.MagicMock()
        mock_wrapper.virtual_disks = [mock_remove]
        mock_vg.return_value = mock_wrapper

        # Invoke the call
        local = self.get_ls(self.apt)
        local.delete_disks([mock_remove])

        # Validate the call
        self.assertEqual(1, mock_wrapper.update.call_count)
        self.assertEqual(0, len(mock_wrapper.virtual_disks))

    @mock.patch('pypowervm.wrappers.storage.VG', autospec=True)
    def test_extend_disk_not_found(self, mock_vg):
        local = self.get_ls(self.apt)
        inst = mock.Mock()
        inst.name = 'Name Of Instance'
        inst.uuid = 'd5065c2c-ac43-3fa6-af32-ea84a3960291'

        vdisk = mock.Mock(name='vdisk')
        vdisk.name = 'NO_MATCH'

        resp = mock.Mock(name='response')
        resp.virtual_disks = [vdisk]
        mock_vg.get.return_value = resp

        self.assertRaises(nova_exc.DiskNotFound, local.extend_disk,
                          inst, dict(type='boot'), 10)

        vdisk.name = 'b_Name_Of__d506'
        local.extend_disk(inst, dict(type='boot'), 1000)
        # Validate the call
        self.assertEqual(1, resp.update.call_count)
        self.assertEqual(vdisk.capacity, 1000)

    @mock.patch('pypowervm.wrappers.storage.VG', autospec=True)
    def test_extend_disk_file_format(self, mock_vg):
        local = self.get_ls(self.apt)
        inst = mock.Mock()
        inst.name = 'Name Of Instance'
        inst.uuid = 'd5065c2c-ac43-3fa6-af32-ea84a3960291'

        vdisk = mock.Mock(name='vdisk')
        vdisk.configure_mock(name='/path/to/b_Name_Of__d506',
                             backstore_type=pvm_stor.BackStoreType.USER_QCOW,
                             file_format=pvm_stor.FileFormatType.QCOW2)
        resp = mock.Mock(name='response')
        resp.virtual_disks = [vdisk]
        mock_vg.get.return_value = resp
        self.assertRaises(nova_exc.ResizeError, local.extend_disk,
                          inst, dict(type='boot'), 10)
        vdisk.file_format = pvm_stor.FileFormatType.RAW
        self.assertRaises(nova_exc.ResizeError, local.extend_disk,
                          inst, dict(type='boot'), 10)

    def _bld_mocks_for_instance_disk(self):
        inst = mock.Mock()
        inst.name = 'Name Of Instance'
        inst.uuid = uuids.inst_uuid
        lpar_wrap = mock.Mock()
        lpar_wrap.id = 2
        vios1 = self.vio_to_vg
        back_stor_name = 'b_Name_Of__' + inst.uuid[:4]
        vios1.scsi_mappings[0].backing_storage.name = back_stor_name
        return inst, lpar_wrap, vios1

    def test_get_bootdisk_path(self):
        local = self.get_ls(self.apt)
        inst = mock.Mock()
        inst.name = 'Name Of Instance'
        inst.uuid = 'f921620A-EE30-440E-8C2D-9F7BA123F298'
        vios1 = self.vio_to_vg
        vios1.scsi_mappings[0].server_adapter.backing_dev_name = 'boot_7f81628'
        vios1.scsi_mappings[0].backing_storage.name = 'b_Name_Of__f921'
        self.mock_vios_get.return_value = vios1
        dev_name = local.get_bootdisk_path(inst, vios1.uuid)
        self.assertEqual('boot_7f81628', dev_name)

    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper',
                autospec=True)
    @mock.patch('pypowervm.wrappers.storage.VG.get', new=mock.Mock())
    def test_get_bootdisk_iter(self, mock_lpar_wrap):
        local = self.get_ls(self.apt)
        inst, lpar_wrap, vios1 = self._bld_mocks_for_instance_disk()
        mock_lpar_wrap.return_value = lpar_wrap

        # Good path
        self.mock_vios_get.return_value = vios1
        for vdisk, vios in local._get_bootdisk_iter(inst):
            self.assertEqual(vios1.scsi_mappings[0].backing_storage, vdisk)
            self.assertEqual(vios1.uuid, vios.uuid)
        self.mock_vios_get.assert_called_once_with(
            self.apt, uuid='vios-uuid', xag=[pvm_const.XAG.VIO_SMAP])

        # Not found because no storage of that name
        self.mock_vios_get.reset_mock()
        self.mock_find_maps.return_value = []
        for vdisk, vios in local._get_bootdisk_iter(inst):
            self.fail('Should not have found any storage elements.')
        self.mock_vios_get.assert_called_once_with(
            self.apt, uuid='vios-uuid', xag=[pvm_const.XAG.VIO_SMAP])

    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_vscsi_mapping', autospec=True)
    def test_connect_instance_disk_to_mgmt_partition(self, mock_add, mock_lw):
        local = self.get_ls(self.apt)
        inst, lpar_wrap, vios1 = self._bld_mocks_for_instance_disk()
        mock_lw.return_value = lpar_wrap

        # Good path
        self.mock_vios_get.return_value = vios1
        vdisk, vios = local.connect_instance_disk_to_mgmt(inst)
        self.assertEqual(vios1.scsi_mappings[0].backing_storage, vdisk)
        self.assertIs(vios1, vios)
        self.assertEqual(1, mock_add.call_count)
        mock_add.assert_called_with('host_uuid', vios, 'mp_uuid', vdisk)

        # add_vscsi_mapping raises.  Show-stopper since only one VIOS.
        mock_add.reset_mock()
        mock_add.side_effect = Exception("mapping failed")
        self.assertRaises(npvmex.InstanceDiskMappingFailed,
                          local.connect_instance_disk_to_mgmt, inst)
        self.assertEqual(1, mock_add.call_count)

        # Not found
        mock_add.reset_mock()
        self.mock_find_maps.return_value = []
        self.assertRaises(npvmex.InstanceDiskMappingFailed,
                          local.connect_instance_disk_to_mgmt, inst)
        self.assertFalse(mock_add.called)

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_vdisk_mapping',
                autospec=True)
    def test_disconnect_disk_from_mgmt_partition(self, mock_rm_vdisk_map):
        local = self.get_ls(self.apt)
        local.disconnect_disk_from_mgmt('vios-uuid', 'disk_name')
        mock_rm_vdisk_map.assert_called_with(
            local.adapter, 'vios-uuid', 'mp_uuid', disk_names=['disk_name'])

    def test_capabilities_non_mgmt_vios(self):
        local = self.get_ls(self.apt)
        self.assertFalse(local.capabilities.get('shared_storage'))
        self.assertTrue(local.capabilities.get('has_imagecache'))
        # With the default setup, the management partition isn't the VIOS.
        self.assertFalse(local.capabilities.get('snapshot'))

    def test_capabilities_mgmt_vios(self):
        # Make the management partition the VIOS.
        self.vio_to_vg.uuid = self.mgmt_uuid.return_value
        local = self.get_ls(self.apt)
        self.assertFalse(local.capabilities.get('shared_storage'))
        self.assertTrue(local.capabilities.get('has_imagecache'))
        self.assertTrue(local.capabilities.get('snapshot'))
