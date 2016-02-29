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

import copy
from nova import exception as nova_exc
from nova import test
from pypowervm import const as pvm_const
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.tests.test_utils import pvmhttp
from pypowervm.wrappers import storage as pvm_stor
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.tests.virt import powervm
from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm.disk import driver as disk_dvr
from nova_powervm.virt.powervm.disk import localdisk as ld
from nova_powervm.virt.powervm import exception as npvmex


VOL_GRP_WITH_VIOS = 'fake_volume_group_with_vio_data.txt'
VIOS_WITH_VOL_GRP = 'fake_vios_with_volume_group_data.txt'


class TestLocalDisk(test.TestCase):
    """Unit Tests for the LocalDisk storage driver."""

    def setUp(self):
        super(TestLocalDisk, self).setUp()

        self.apt = self.useFixture(pvm_fx.AdapterFx()).adpt

        def resp(file_name):
            return pvmhttp.load_pvm_resp(
                file_name, adapter=self.apt).get_response()

        self.vg_to_vio = resp(VOL_GRP_WITH_VIOS)
        self.vio_to_vg = resp(VIOS_WITH_VOL_GRP)

        # Set up for the mocks for get_ls

        self.mock_vg_uuid_p = mock.patch('nova_powervm.virt.powervm.disk.'
                                         'localdisk.LocalStorage.'
                                         '_get_vg_uuid')
        self.mock_vg_uuid = self.mock_vg_uuid_p.start()
        vg_uuid = 'd5065c2c-ac43-3fa6-af32-ea84a3960291'
        self.mock_vg_uuid.return_value = ('vios_uuid', vg_uuid)

    def tearDown(self):
        test.TestCase.tearDown(self)

        # Tear down mocks
        self.mock_vg_uuid_p.stop()

    @staticmethod
    def get_ls(adpt):
        return ld.LocalStorage({'adapter': adpt, 'host_uuid': 'host_uuid',
                                'mp_uuid': 'mp_uuid'})

    @mock.patch('pypowervm.tasks.storage.upload_new_vdisk')
    @mock.patch('nova_powervm.virt.powervm.disk.driver.'
                'IterableToFileAdapter')
    @mock.patch('nova.image.API')
    def test_create_disk_from_image(self, mock_img_api, mock_file_adpt,
                                    mock_upload_vdisk):
        mock_upload_vdisk.return_value = ('vdisk', None)
        inst = mock.Mock()
        inst.name = 'Inst Name'
        inst.uuid = 'd5065c2c-ac43-3fa6-af32-ea84a3960291'

        vdisk = self.get_ls(self.apt).create_disk_from_image(
            None, inst, powervm.TEST_IMAGE1, 20)
        mock_upload_vdisk.assert_called_with(mock.ANY, mock.ANY, mock.ANY,
                                             mock.ANY, 'b_Inst_Nam_d506',
                                             powervm.TEST_IMAGE1.size,
                                             d_size=21474836480)
        self.assertEqual('vdisk', vdisk)

    @mock.patch('pypowervm.wrappers.storage.VG')
    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage.'
                '_get_vg')
    def test_capacity(self, mock_get_vg, mock_vg):
        """Tests the capacity methods."""

        # Set up the mock data.  This will simulate our vg wrapper
        mock_vg_wrap = mock.MagicMock(name='vg_wrapper')
        type(mock_vg_wrap).capacity = mock.PropertyMock(return_value='5120')
        type(mock_vg_wrap).available_size = mock.PropertyMock(
            return_value='2048')

        mock_vg.wrap.return_value = mock_vg_wrap
        local = self.get_ls(self.apt)

        self.assertEqual(5120.0, local.capacity)
        self.assertEqual(3072.0, local.capacity_used)

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps')
    @mock.patch('nova_powervm.virt.powervm.vios.get_active_vioses')
    def test_disconnect_image_disk(self, mock_active_vioses, mock_rm_maps):
        # vio_to_vg is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTaskFx and FeedTask.
        feed = [pvm_vios.VIOS.wrap(self.vio_to_vg)]
        mock_active_vioses.return_value = feed
        ft_fx = pvm_fx.FeedTaskFx(feed)
        self.useFixture(ft_fx)

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
        self.assertEqual(1, ft_fx.patchers['update'].mock.call_count)
        mock_rm_maps.assert_called_once_with(feed[0], fx.FAKE_INST_UUID_PVM,
                                             match_func=mock.ANY)

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps')
    @mock.patch('nova_powervm.virt.powervm.vios.get_active_vioses')
    def test_disconnect_image_disk_no_update(self, mock_active_vioses,
                                             mock_rm_maps):
        # vio_to_vg is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTaskFx and FeedTask.
        feed = [pvm_vios.VIOS.wrap(self.vio_to_vg)]
        mock_active_vioses.return_value = feed
        ft_fx = pvm_fx.FeedTaskFx(feed)
        self.useFixture(ft_fx)

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
        self.assertEqual(0, ft_fx.patchers['update'].mock.call_count)
        mock_rm_maps.assert_called_once_with(feed[0], fx.FAKE_INST_UUID_PVM,
                                             match_func=mock.ANY)

    @mock.patch('pypowervm.tasks.scsi_mapper.gen_match_func')
    @mock.patch('pypowervm.tasks.scsi_mapper.find_maps')
    def test_disconnect_image_disk_disktype(self, mock_find_maps,
                                            mock_match_func):
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
        mock_find_maps.assert_called_once_with(
            mock.ANY, client_lpar_id=fx.FAKE_INST_UUID_PVM, match_func='test')

        # Make sure the matching function is generated with the right disk type
        mock_match_func.assert_called_once_with(
            pvm_stor.VDisk, prefixes=[disk_dvr.DiskType.BOOT])

    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_map')
    @mock.patch('nova_powervm.virt.powervm.vios.get_active_vioses')
    def test_connect_image_disk(self, mock_active_vioses, mock_add_map,
                                mock_build_map):
        # vio_to_vg is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTaskFx and FeedTask.
        feed = [pvm_vios.VIOS.wrap(self.vio_to_vg)]
        mock_active_vioses.return_value = feed
        ft_fx = pvm_fx.FeedTaskFx(feed)
        self.useFixture(ft_fx)

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
        self.assertEqual(1, ft_fx.patchers['update'].mock.call_count)

    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_map')
    @mock.patch('nova_powervm.virt.powervm.vios.get_active_vioses')
    def test_connect_image_disk_no_update(self, mock_active_vioses,
                                          mock_add_map, mock_build_map):
        # vio_to_vg is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTaskFx and FeedTask.
        feed = [pvm_vios.VIOS.wrap(self.vio_to_vg)]
        mock_active_vioses.return_value = feed
        ft_fx = pvm_fx.FeedTaskFx(feed)
        self.useFixture(ft_fx)

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
        self.assertEqual(0, ft_fx.patchers['update'].mock.call_count)

    @mock.patch('pypowervm.wrappers.storage.VG.update')
    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage.'
                '_get_vg_wrap')
    def test_delete_disks(self, mock_vg, mock_update):
        # Mocks
        self.apt.side_effect = [self.vg_to_vio]

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

    @mock.patch('pypowervm.wrappers.storage.VG')
    def test_extend_disk_not_found(self, mock_vg):
        local = self.get_ls(self.apt)

        inst = mock.Mock()
        inst.name = 'Name Of Instance'
        inst.uuid = 'd5065c2c-ac43-3fa6-af32-ea84a3960291'

        vdisk = mock.Mock(name='vdisk')
        vdisk.name = 'NO_MATCH'

        resp = mock.Mock(name='response')
        resp.virtual_disks = [vdisk]
        mock_vg.wrap.return_value = resp

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
        vios1 = pvm_vios.VIOS.wrap(self.vio_to_vg)
        vios2 = copy.deepcopy(vios1)
        vios1.scsi_mappings[0].backing_storage.name = 'b_Name_Of__d506'
        return inst, lpar_wrap, vios1, vios2

    @mock.patch('pypowervm.wrappers.storage.VG')
    def test_instance_disk_iter(self, mock_vg):
        def assert_read_calls(num):
            self.assertEqual(num, self.apt.read.call_count)
            self.apt.read.assert_has_calls(
                [mock.call(pvm_vios.VIOS.schema_type, root_id='vios_uuid',
                           xag=[pvm_const.XAG.VIO_SMAP])
                 for i in range(num)])
        local = self.get_ls(self.apt)
        inst, lpar_wrap, vios1, vios2 = self._bld_mocks_for_instance_disk()

        # Good path
        self.apt.read.return_value = vios1.entry
        for vdisk, vios in local.instance_disk_iter(inst, lpar_wrap=lpar_wrap):
            self.assertEqual('0300025d4a00007a000000014b36d9deaf.1',
                             vdisk.udid)
            self.assertEqual('3443DB77-AED1-47ED-9AA5-3DB9C6CF7089', vios.uuid)
        assert_read_calls(1)

        # Not found because no storage of that name
        self.apt.reset_mock()
        self.apt.read.return_value = vios2.entry
        for vdisk, vios in local.instance_disk_iter(inst, lpar_wrap=lpar_wrap):
            self.fail()
        assert_read_calls(1)

        # Not found because LPAR ID doesn't match
        self.apt.reset_mock()
        self.apt.read.return_value = vios1.entry
        lpar_wrap.id = 3
        for vdisk, vios in local.instance_disk_iter(inst, lpar_wrap=lpar_wrap):
            self.fail()
        assert_read_calls(1)

    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_vscsi_mapping')
    def test_connect_instance_disk_to_mgmt_partition(self, mock_add, mock_lw):
        local = self.get_ls(self.apt)
        inst, lpar_wrap, vios1, vios2 = self._bld_mocks_for_instance_disk()
        mock_lw.return_value = lpar_wrap

        # Good path
        self.apt.read.return_value = vios1.entry
        vdisk, vios = local.connect_instance_disk_to_mgmt(inst)
        self.assertEqual('0300025d4a00007a000000014b36d9deaf.1', vdisk.udid)
        self.assertIs(vios1.entry, vios.entry)
        self.assertEqual(1, mock_add.call_count)
        mock_add.assert_called_with('host_uuid', vios, 'mp_uuid', vdisk)

        # Not found
        mock_add.reset_mock()
        self.apt.read.return_value = vios2.entry
        self.assertRaises(npvmex.InstanceDiskMappingFailed,
                          local.connect_instance_disk_to_mgmt, inst)
        self.assertEqual(0, mock_add.call_count)

        # add_vscsi_mapping raises.  Show-stopper since only one VIOS.
        mock_add.reset_mock()
        self.apt.read.return_value = vios1.entry
        mock_add.side_effect = Exception("mapping failed")
        self.assertRaises(npvmex.InstanceDiskMappingFailed,
                          local.connect_instance_disk_to_mgmt, inst)
        self.assertEqual(1, mock_add.call_count)

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

        def resp(file_name):
            return pvmhttp.load_pvm_resp(
                file_name, adapter=self.apt).get_response()

        self.vg_to_vio = resp(VOL_GRP_WITH_VIOS)
        self.vio_to_vg = resp(VIOS_WITH_VOL_GRP)

        self.mock_vios_feed = [pvm_vios.VIOS.wrap(self.vio_to_vg)]
        self.mock_vg_feed = [pvm_stor.VG.wrap(self.vg_to_vio)]

    @mock.patch('pypowervm.wrappers.storage.VG.wrap')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.wrap')
    def test_get_vg_uuid(self, mock_vio_wrap, mock_vg_wrap):
        # The read is first the VIOS, then the Volume Group.  The reads
        # aren't really used as the wrap function is what we use to pass
        # back the proper data (as we're simulating feeds).
        self.apt.read.side_effect = [self.vio_to_vg, self.vg_to_vio]
        mock_vio_wrap.return_value = self.mock_vios_feed
        mock_vg_wrap.return_value = self.mock_vg_feed
        self.flags(volume_group_name='rootvg', group='powervm')

        storage = ld.LocalStorage({'adapter': self.apt,
                                   'host_uuid': 'host_uuid',
                                   'mp_uuid': 'mp_uuid'})

        # Make sure the uuids match
        self.assertEqual('d5065c2c-ac43-3fa6-af32-ea84a3960291',
                         storage.vg_uuid)

    @mock.patch('pypowervm.wrappers.storage.VG.wrap')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.search')
    def test_get_vg_uuid_on_vios(self, mock_vio_search, mock_vg_wrap):
        # Return no VIOSes.
        mock_vio_search.return_value = []

        # Similar to test_get_vg_uuid, the read isn't what is useful.  The
        # wrap is used to simulate a feed.
        self.apt.read.return_value = self.vg_to_vio
        mock_vg_wrap.return_value = self.mock_vg_feed

        # Override that we need a specific VIOS...that won't be found.
        self.flags(volume_group_name='rootvg',
                   volume_group_vios_name='invalid_vios', group='powervm')

        self.assertRaises(npvmex.VGNotFound, ld.LocalStorage,
                          {'adapter': self.apt, 'host_uuid': 'host_uuid',
                           'mp_uuid': 'mp_uuid'})
