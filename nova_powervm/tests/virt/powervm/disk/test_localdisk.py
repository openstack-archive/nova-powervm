# Copyright 2015, 2017 IBM Corp.
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


class TestLocalDisk(test.TestCase):
    """Unit Tests for the LocalDisk storage driver."""

    def setUp(self):
        super(TestLocalDisk, self).setUp()

        self.apt = self.useFixture(pvm_fx.AdapterFx()).adpt

        # Set up mock for internal VIOS.get()s
        self.mock_vios_get = self.useFixture(fixtures.MockPatch(
            'pypowervm.wrappers.virtual_io_server.VIOS.get')).mock
        # The mock VIOS needs to have scsi_mappings as a list.  Internals are
        # set by individual test cases as needed.
        smaps = [mock.Mock()]
        self.vio_to_vg = mock.Mock(spec=pvm_vios.VIOS, scsi_mappings=smaps)
        # For our tests, we want find_maps to return the mocked list of scsi
        # mappings in our mocked VIOS.
        self.mock_find_maps = self.useFixture(fixtures.MockPatch(
            'pypowervm.tasks.scsi_mapper.find_maps')).mock
        self.mock_find_maps.return_value = smaps

        # Set up for the mocks for get_ls
        self.mock_vg_uuid = self.useFixture(fixtures.MockPatch(
            'nova_powervm.virt.powervm.disk.localdisk.LocalStorage.'
            '_get_vg_uuid')).mock
        self.vg_uuid = 'd5065c2c-ac43-3fa6-af32-ea84a3960291'
        self.mock_vg_uuid.return_value = ('vios_uuid', self.vg_uuid)

        # Return the mgmt uuid
        self.mgmt_uuid = self.useFixture(fixtures.MockPatch(
            'nova_powervm.virt.powervm.mgmt.mgmt_uuid')).mock
        self.mgmt_uuid.return_value = 'mp_uuid'

    @staticmethod
    def get_ls(adpt):
        return ld.LocalStorage(adpt, 'host_uuid')

    @mock.patch('pypowervm.tasks.storage.upload_new_vdisk')
    @mock.patch('nova.image.api.API.download')
    def test_create_disk_from_image(self, mock_img_api, mock_upload_vdisk):
        mock_upload_vdisk.return_value = ('vdisk', None)
        inst = mock.Mock()
        inst.configure_mock(name='Inst Name',
                            uuid='d5065c2c-ac43-3fa6-af32-ea84a3960291')

        def test_upload_new_vdisk(adpt, vio_uuid, vg_uuid, upload, vol_name,
                                  image_size, d_size=0, upload_type=None):
            self.assertEqual(self.apt, adpt)
            self.assertEqual('vios_uuid', vio_uuid)
            self.assertEqual(self.vg_uuid, vg_uuid)
            self.assertEqual('b_Inst_Nam_d506', vol_name)
            self.assertEqual(powervm.TEST_IMAGE1.size, image_size)
            self.assertEqual(21474836480, d_size)
            self.assertEqual(tsk_stor.UploadType.FUNC, upload_type)
            upload('test_path')
            return 'vdisk', None

        mock_upload_vdisk.side_effect = test_upload_new_vdisk

        vdisk = self.get_ls(self.apt).create_disk_from_image(
            None, inst, powervm.TEST_IMAGE1, 20)
        self.assertEqual('vdisk', vdisk)

        # Make sure the upload was invoked properly
        mock_img_api.assert_called_once_with(
            None, '3e865d14-8c1e-4615-b73f-f78eaecabfbd',
            dest_path='test_path')

        mock_img_api.reset_mock()
        exception = Exception
        mock_img_api.side_effect = exception
        self.assertRaises(exception,
                          self.get_ls(self.apt).create_disk_from_image,
                          None, inst, powervm.TEST_IMAGE1, 20)
        self.assertEqual(mock_img_api.call_count, 4)

    @mock.patch('pypowervm.wrappers.storage.VG.get')
    def test_capacity(self, mock_vg):
        """Tests the capacity methods."""

        # Set up the mock data.  This will simulate our vg wrapper
        mock_vg_wrap = mock.MagicMock(name='vg_wrapper')
        type(mock_vg_wrap).capacity = mock.PropertyMock(return_value='5120')
        type(mock_vg_wrap).available_size = mock.PropertyMock(
            return_value='2048')

        mock_vg.return_value = mock_vg_wrap
        local = self.get_ls(self.apt)

        self.assertEqual(5120.0, local.capacity)
        self.assertEqual(3072.0, local.capacity_used)

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps')
    @mock.patch('pypowervm.tasks.partition.get_active_vioses')
    def test_disconnect_image_disk(self, mock_active_vioses, mock_rm_maps):
        # vio_to_vg is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTaskFx and FeedTask.
        feed = [self.vio_to_vg]
        mock_active_vioses.return_value = feed

        # The mock return values
        mock_rm_maps.return_value = True

        # Need the driver to return the actual UUID of the VIOS in the feed,
        # to match the FeedTask.
        self.mock_vg_uuid.return_value = (feed[0].uuid, 'vg_uuid')

        # Create the feed task
        local = self.get_ls(self.apt)
        inst = mock.Mock(uuid=fx.FAKE_INST_UUID)

        # As initialized above, remove_maps returns True to trigger update.
        local.disconnect_image_disk(mock.MagicMock(), inst, stg_ftsk=None,
                                    disk_type=[disk_dvr.DiskType.BOOT])
        self.assertEqual(1, mock_rm_maps.call_count)
        self.assertEqual(1, self.vio_to_vg.update.call_count)
        mock_rm_maps.assert_called_once_with(feed[0], fx.FAKE_INST_UUID_PVM,
                                             match_func=mock.ANY)

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps')
    @mock.patch('pypowervm.tasks.partition.get_active_vioses')
    def test_disconnect_image_disk_no_update(self, mock_active_vioses,
                                             mock_rm_maps):
        # vio_to_vg is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTaskFx and FeedTask.
        feed = [self.vio_to_vg]
        mock_active_vioses.return_value = feed

        # The mock return values
        mock_rm_maps.return_value = False

        # Need the driver to return the actual UUID of the VIOS in the feed,
        # to match the FeedTask.
        self.mock_vg_uuid.return_value = (feed[0].uuid, 'vg_uuid')

        # Create the feed task
        local = self.get_ls(self.apt)
        inst = mock.Mock(uuid=fx.FAKE_INST_UUID)

        # As initialized above, remove_maps returns True to trigger update.
        local.disconnect_image_disk(mock.MagicMock(), inst, stg_ftsk=None,
                                    disk_type=[disk_dvr.DiskType.BOOT])
        self.assertEqual(1, mock_rm_maps.call_count)
        self.vio_to_vg.update.assert_not_called()
        mock_rm_maps.assert_called_once_with(feed[0], fx.FAKE_INST_UUID_PVM,
                                             match_func=mock.ANY)

    @mock.patch('pypowervm.tasks.scsi_mapper.gen_match_func')
    def test_disconnect_image_disk_disktype(self, mock_match_func):
        """Ensures that the match function passes in the right prefix."""
        # Set up the mock data.
        inst = mock.Mock(uuid=fx.FAKE_INST_UUID)
        mock_match_func.return_value = 'test'

        # Invoke
        local = self.get_ls(self.apt)
        local.disconnect_image_disk(mock.MagicMock(), inst,
                                    stg_ftsk=mock.MagicMock(),
                                    disk_type=[disk_dvr.DiskType.BOOT])

        # Make sure the find maps is invoked once.
        self.mock_find_maps.assert_called_once_with(
            mock.ANY, client_lpar_id=fx.FAKE_INST_UUID_PVM, match_func='test')

        # Make sure the matching function is generated with the right disk type
        mock_match_func.assert_called_once_with(
            pvm_stor.VDisk, prefixes=[disk_dvr.DiskType.BOOT])

    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_map')
    @mock.patch('pypowervm.tasks.partition.get_active_vioses')
    def test_connect_image_disk(self, mock_active_vioses, mock_add_map,
                                mock_build_map):
        # vio_to_vg is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTask.
        feed = [self.vio_to_vg]
        mock_active_vioses.return_value = feed

        # The mock return values
        mock_add_map.return_value = True
        self.mock_vg_uuid.return_value = (feed[0].uuid, 'vg_uuid')
        mock_build_map.return_value = 'fake_map'

        # Need the driver to return the actual UUID of the VIOS in the feed,
        # to match the FeedTask.
        local = self.get_ls(self.apt)
        inst = mock.Mock(uuid=fx.FAKE_INST_UUID)

        # As initialized above, remove_maps returns True to trigger update.
        local.connect_disk(mock.MagicMock(), inst, mock.MagicMock(),
                           stg_ftsk=None)
        self.assertEqual(1, mock_add_map.call_count)
        mock_add_map.assert_called_once_with(feed[0], 'fake_map')
        self.assertEqual(1, self.vio_to_vg.update.call_count)

    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_map')
    @mock.patch('pypowervm.tasks.partition.get_active_vioses')
    def test_connect_image_disk_no_update(self, mock_active_vioses,
                                          mock_add_map, mock_build_map):
        # vio_to_vg is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTask.
        feed = [self.vio_to_vg]
        mock_active_vioses.return_value = feed

        # The mock return values
        mock_add_map.return_value = False
        self.mock_vg_uuid.return_value = (feed[0].uuid, 'vg_uuid')
        mock_build_map.return_value = 'fake_map'

        # Need the driver to return the actual UUID of the VIOS in the feed,
        # to match the FeedTask.
        local = self.get_ls(self.apt)
        inst = mock.Mock(uuid=fx.FAKE_INST_UUID)

        # As initialized above, remove_maps returns True to trigger update.
        local.connect_disk(mock.MagicMock(), inst, mock.MagicMock(),
                           stg_ftsk=None)
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
        local.delete_disks(mock.MagicMock(), mock.MagicMock(),
                           [mock_remove])

        # Validate the call
        self.assertEqual(1, mock_wrapper.update.call_count)
        self.assertEqual(0, len(mock_wrapper.virtual_disks))

    @mock.patch('pypowervm.wrappers.storage.VG.get')
    def test_extend_disk_not_found(self, mock_vg):
        local = self.get_ls(self.apt)

        inst = mock.Mock()
        inst.name = 'Name Of Instance'
        inst.uuid = 'd5065c2c-ac43-3fa6-af32-ea84a3960291'

        vdisk = mock.Mock(name='vdisk')
        vdisk.name = 'NO_MATCH'

        resp = mock.Mock(name='response')
        resp.virtual_disks = [vdisk]
        mock_vg.return_value = resp

        self.assertRaises(nova_exc.DiskNotFound, local.extend_disk,
                          'context', inst, dict(type='boot'), 10)

        vdisk.name = 'b_Name_Of__d506'
        local.extend_disk('context', inst, dict(type='boot'), 1000)
        # Validate the call
        self.assertEqual(1, resp.update.call_count)
        self.assertEqual(vdisk.capacity, 1000)

    def _bld_mocks_for_instance_disk(self):
        inst = mock.Mock()
        inst.name = 'Name Of Instance'
        inst.uuid = 'd5065c2c-ac43-3fa6-af32-ea84a3960291'
        lpar_wrap = mock.Mock()
        lpar_wrap.id = 2
        vios1 = self.vio_to_vg
        vios1.scsi_mappings[0].backing_storage.name = 'b_Name_Of__d506'
        return inst, lpar_wrap, vios1

    def test_boot_disk_path_for_instance(self):
        local = self.get_ls(self.apt)
        inst = mock.Mock()
        inst.name = 'Name Of Instance'
        inst.uuid = 'f921620A-EE30-440E-8C2D-9F7BA123F298'
        vios1 = self.vio_to_vg
        vios1.scsi_mappings[0].server_adapter.backing_dev_name = 'boot_7f81628'
        vios1.scsi_mappings[0].backing_storage.name = 'b_Name_Of__f921'
        self.mock_vios_get.return_value = vios1
        dev_name = local.boot_disk_path_for_instance(inst, vios1.uuid)
        self.assertEqual('boot_7f81628', dev_name)

    @mock.patch('pypowervm.wrappers.storage.VG.get', new=mock.Mock())
    def test_instance_disk_iter(self):
        local = self.get_ls(self.apt)
        inst, lpar_wrap, vios1 = self._bld_mocks_for_instance_disk()

        # Good path
        self.mock_vios_get.return_value = vios1
        for vdisk, vios in local.instance_disk_iter(inst, lpar_wrap=lpar_wrap):
            self.assertEqual(vios1.scsi_mappings[0].backing_storage, vdisk)
            self.assertEqual(vios1.uuid, vios.uuid)
        self.mock_vios_get.assert_called_once_with(
            self.apt, uuid='vios_uuid', xag=[pvm_const.XAG.VIO_SMAP])

        # Not found because no storage of that name
        self.mock_vios_get.reset_mock()
        self.mock_find_maps.return_value = []
        for vdisk, vios in local.instance_disk_iter(inst, lpar_wrap=lpar_wrap):
            self.fail()
        self.mock_vios_get.assert_called_once_with(
            self.apt, uuid='vios_uuid', xag=[pvm_const.XAG.VIO_SMAP])

    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_vscsi_mapping')
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
        mock_add.assert_not_called()

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_vdisk_mapping')
    def test_disconnect_disk_from_mgmt_partition(self, mock_rm_vdisk_map):
        local = self.get_ls(self.apt)
        local.disconnect_disk_from_mgmt('vios_uuid', 'disk_name')
        mock_rm_vdisk_map.assert_called_with(
            local.adapter, 'vios_uuid', 'mp_uuid', disk_names=['disk_name'])


class TestLocalDiskFindVG(test.TestCase):
    """Test in separate class for the static loading of the VG.

    This is abstracted in all other tests.  To keep the other test cases terse
    we put this one in a separate class that doesn't make use of the patchers.
    """

    def setUp(self):
        super(TestLocalDiskFindVG, self).setUp()

        self.apt = self.useFixture(pvm_fx.AdapterFx()).adpt

        self.vg_to_vio = mock.Mock()
        self.vg_to_vio.configure_mock(name='rootvg')
        self.vio_to_vg = mock.Mock()

        self.mock_vios_feed = [self.vio_to_vg]
        self.mock_vg_feed = [self.vg_to_vio]

        # Return the mgmt uuid
        self.mgmt_uuid = self.useFixture(fixtures.MockPatch(
            'nova_powervm.virt.powervm.mgmt.mgmt_uuid')).mock
        self.mgmt_uuid.return_value = 'mp_uuid'

    @mock.patch('pypowervm.wrappers.storage.VG.get')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.get')
    def test_get_vg_uuid(self, mock_vio_get, mock_vg_get):
        mock_vio_get.return_value = self.mock_vios_feed
        mock_vg_get.return_value = self.mock_vg_feed
        self.flags(volume_group_name='rootvg', group='powervm')

        storage = ld.LocalStorage(self.apt, 'host_uuid')

        # Make sure the uuids match
        self.assertEqual(self.vg_to_vio.uuid, storage.vg_uuid)

    @mock.patch('pypowervm.wrappers.storage.VG.get')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.search')
    def test_get_vg_uuid_on_vios(self, mock_vio_search, mock_vg_get):
        # Return no VIOSes.
        mock_vio_search.return_value = []

        # Similar to test_get_vg_uuid, the read isn't what is useful.  The
        # wrap is used to simulate a feed.
        self.apt.read.return_value = self.vg_to_vio
        mock_vg_get.return_value = self.mock_vg_feed

        # Override that we need a specific VIOS...that won't be found.
        self.flags(volume_group_name='rootvg',
                   volume_group_vios_name='invalid_vios', group='powervm')

        self.assertRaises(npvmex.VGNotFound, ld.LocalStorage,
                          self.apt, 'host_uuid')
