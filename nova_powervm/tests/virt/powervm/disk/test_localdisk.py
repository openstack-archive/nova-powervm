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

import copy

import mock
from oslo_config import cfg

from nova import exception as nova_exc
from nova import test
import os
from pypowervm.tests.wrappers.util import pvmhttp
from pypowervm.wrappers import storage as pvm_stor
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm.disk import driver as disk_dvr
from nova_powervm.virt.powervm.disk import localdisk as ld


VOL_GRP_WITH_VIOS = 'fake_volume_group_with_vio_data.txt'
VIOS_WITH_VOL_GRP = 'fake_vios_with_volume_group_data.txt'

CONF = cfg.CONF


class TestLocalDisk(test.TestCase):
    """Unit Tests for the LocalDisk storage driver."""

    def setUp(self):
        super(TestLocalDisk, self).setUp()

        # Find directory for response file(s)
        data_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(data_dir, "..", 'data')

        def resp(file_name):
            file_path = os.path.join(data_dir, file_name)
            return pvmhttp.load_pvm_resp(file_path).get_response()

        self.vg_to_vio = resp(VOL_GRP_WITH_VIOS)
        self.vio_to_vg = resp(VIOS_WITH_VOL_GRP)
        self.pypvm = self.useFixture(fx.PyPowerVM())
        self.apt = self.pypvm.apt

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
        return ld.LocalStorage({'adapter': adpt, 'host_uuid': 'host_uuid'})

    @mock.patch('pypowervm.tasks.storage.upload_new_vdisk')
    @mock.patch('nova_powervm.virt.powervm.disk.driver.'
                'IterableToFileAdapter')
    @mock.patch('nova.image.API')
    def test_create_disk_from_image(self, mock_img_api, mock_file_adpt,
                                    mock_upload_vdisk):
        mock_img = {'id': 'fake_id', 'size': 50}
        mock_upload_vdisk.return_value = ('vdisk', None)
        inst = mock.Mock()
        inst.name = 'Inst Name'
        inst.uuid = 'd5065c2c-ac43-3fa6-af32-ea84a3960291'

        vdisk = self.get_ls(self.apt).create_disk_from_image(
            None, inst, mock_img, 20)
        mock_upload_vdisk.assert_called_with(mock.ANY, mock.ANY, mock.ANY,
                                             mock.ANY, 'b_Inst_Nam_d506', 50,
                                             d_size=21474836480L)
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

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_vdisk_mapping')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_disconnect_image_disk(self, mock_get_vm_id, mock_remove):
        """Tests the disconnect_image_disk method."""
        # Set up the mock data.
        mock_get_vm_id.return_value = '2'
        mock_remove.return_value = ('fake_vios', [])

        local = self.get_ls(self.apt)
        local.disconnect_image_disk(mock.MagicMock(), mock.MagicMock(), '2')

        # Validate
        mock_remove.assert_called_once_with(mock.ANY, mock.ANY, '2',
                                            disk_prefixes=None)
        self.assertEqual(1, mock_remove.call_count)

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_vdisk_mapping')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_disconnect_image_disk_disktype(self, mock_get_vm_id, mock_remove):
        """Tests the disconnect_image_disk method."""
        # Set up the mock data.
        mock_get_vm_id.return_value = '2'
        mock_remove.return_value = ('fake_vios', [])

        # Invoke
        local = self.get_ls(self.apt)
        local.disconnect_image_disk(mock.MagicMock(), mock.MagicMock(), '2',
                                    disk_type=[disk_dvr.DiskType.BOOT])

        # Validate
        mock_remove.assert_called_once_with(mock.ANY, mock.ANY, '2',
                                            disk_prefixes=[
                                                disk_dvr.DiskType.BOOT])
        self.assertEqual(1, mock_remove.call_count)

    @mock.patch('pypowervm.tasks.scsi_mapper.add_vscsi_mapping')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VSCSIMapping.'
                '_client_lpar_href')
    def test_connect_disk(self, mock_lpar_href, mock_add_mapping):
        mock_lpar_href.return_value = 'client_lpar_href'

        mock_vdisk = mock.MagicMock()
        mock_vdisk.name = 'vdisk'

        ls = self.get_ls(self.apt)
        ls.connect_disk(mock.MagicMock(), mock.MagicMock(),
                        mock_vdisk, 'lpar_UUID')
        self.assertEqual(1, mock_add_mapping.call_count)

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
                           xag=[pvm_vios.VIOS.xags.SCSI_MAPPING])
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

    def _mp_wrap_mock(self):
        mp_wrap = mock.Mock()
        mp_wrap.name = 'ManagementPartition'
        mp_wrap.id = 'mp_id'
        mp_wrap.uuid = 'mp_uuid'
        return mp_wrap

    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_vscsi_mapping')
    def test_connect_instance_disk_to_mgmt_partition(self, mock_add, mock_lw):
        local = self.get_ls(self.apt)
        inst, lpar_wrap, vios1, vios2 = self._bld_mocks_for_instance_disk()
        mp_wrap = self._mp_wrap_mock()
        mock_lw.return_value = lpar_wrap

        # Good path
        self.apt.read.return_value = vios1.entry
        vdisk, vios, mpw = local.connect_instance_disk_to_mgmt(inst,
                                                               mp_wrap=mp_wrap)
        self.assertEqual('0300025d4a00007a000000014b36d9deaf.1', vdisk.udid)
        self.assertIs(mp_wrap, mpw)
        self.assertIs(vios1.entry, vios.entry)
        self.assertEqual(1, mock_add.call_count)
        mock_add.assert_called_with('host_uuid', vios1.uuid, 'mp_uuid', vdisk)

        # Not found
        mock_add.reset_mock()
        self.apt.read.return_value = vios2.entry
        self.assertRaises(
            disk_dvr.InstanceDiskMappingFailed,
            local.connect_instance_disk_to_mgmt, inst, mp_wrap=mp_wrap)
        self.assertEqual(0, mock_add.call_count)

        # add_vscsi_mapping raises.  Show-stopper since only one VIOS.
        mock_add.reset_mock()
        self.apt.read.return_value = vios1.entry
        mock_add.side_effect = Exception("mapping failed")
        self.assertRaises(
            disk_dvr.InstanceDiskMappingFailed,
            local.connect_instance_disk_to_mgmt, inst, mp_wrap=mp_wrap)
        self.assertEqual(1, mock_add.call_count)

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_vdisk_mapping')
    def test_disconnect_disk_from_mgmt_partition(self, mock_rm_vdisk_map):
        local = self.get_ls(self.apt)
        mp_wrap = self._mp_wrap_mock()
        local.disconnect_disk_from_mgmt('vios_uuid', 'disk_name', mp_wrap)
        mock_rm_vdisk_map.assert_called_with(local.adapter, 'vios_uuid',
                                             'mp_id', disk_names=['disk_name'])

    @mock.patch('nova_powervm.virt.powervm.mgmt.get_mgmt_partition')
    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage.'
                'instance_disk_iter')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_vscsi_mapping')
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_vdisk_mapping')
    def test_discover_mgmt_partition(self, mock_rm_vdisk, mock_add_map,
                                     mock_inst_disk_iter, mock_get_mgmt):
        """Ensure connect/disconnect will discover the mgmt partition."""
        local = self.get_ls(self.apt)
        mp_wrap = self._mp_wrap_mock()
        inst = mock.Mock()
        mock_inst_disk_iter.return_value = [(mock.Mock(), mock.Mock())]

        local.connect_instance_disk_to_mgmt(inst, mp_wrap=mp_wrap)
        self.assertFalse(mock_get_mgmt.called)
        local.connect_instance_disk_to_mgmt(inst)
        self.assertTrue(mock_get_mgmt.called)

        mock_get_mgmt.reset_mock()
        local.disconnect_disk_from_mgmt('vios_uuid', 'disk_name',
                                        mp_wrap=mp_wrap)
        self.assertFalse(mock_get_mgmt.called)
        local.disconnect_disk_from_mgmt('vios_uuid', 'disk_name')
        self.assertTrue(mock_get_mgmt.called)


class TestLocalDiskFindVG(test.TestCase):
    """Test in separate class for the static loading of the VG.

    This is abstracted in all other tests.  To keep the other test cases terse
    we put this one in a separate class that doesn't make use of the patchers.
    """

    def setUp(self):
        super(TestLocalDiskFindVG, self).setUp()

        # Find directory for response file(s)
        data_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(data_dir, "..", 'data')

        def resp(file_name):
            file_path = os.path.join(data_dir, file_name)
            return pvmhttp.load_pvm_resp(file_path).get_response()

        self.vg_to_vio = resp(VOL_GRP_WITH_VIOS)
        self.vio_to_vg = resp(VIOS_WITH_VOL_GRP)

        self.mock_vios_feed = [pvm_vios.VIOS.wrap(self.vio_to_vg)]
        self.mock_vg_feed = [pvm_stor.VG.wrap(self.vg_to_vio)]

        self.pypvm = self.useFixture(fx.PyPowerVM())
        self.apt = self.pypvm.apt

    @mock.patch('pypowervm.wrappers.storage.VG.wrap')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.wrap')
    def test_get_vg_uuid(self, mock_vio_wrap, mock_vg_wrap):
        # The read is first the VIOS, then the Volume Group.  The reads
        # aren't really used as the wrap function is what we use to pass
        # back the proper data (as we're simulating feeds).
        self.apt.read.side_effect = [self.vio_to_vg, self.vg_to_vio]
        mock_vio_wrap.return_value = self.mock_vios_feed
        mock_vg_wrap.return_value = self.mock_vg_feed
        CONF.volume_group_name = 'rootvg'

        storage = ld.LocalStorage({'adapter': self.apt,
                                   'host_uuid': 'host_uuid'})

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
        CONF.volume_group_name = 'rootvg'
        CONF.volume_group_vios_name = 'invalid_vios'

        self.assertRaises(ld.VGNotFound, ld.LocalStorage,
                          {'adapter': self.apt, 'host_uuid': 'host_uuid'})
