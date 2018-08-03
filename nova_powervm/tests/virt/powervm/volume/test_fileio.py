# Copyright 2017 IBM Corp.
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

from nova_powervm.tests.virt.powervm.volume import test_driver as test_vol
from nova_powervm.virt.powervm import exception as p_exc
from nova_powervm.virt.powervm.volume import fileio as v_drv
from pypowervm import const as pvm_const
from pypowervm import exceptions as pvm_exc
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.wrappers import base_partition as pvm_bp
from pypowervm.wrappers import storage as pvm_stg
from pypowervm.wrappers import virtual_io_server as pvm_vios


class FakeFileIOVolAdapter(v_drv.FileIOVolumeAdapter):
    """Subclass for FileIOVolumeAdapter, since it is abstract."""

    def __init__(self, adapter, host_uuid, instance, connection_info,
                 stg_ftsk=None):
        super(FakeFileIOVolAdapter, self).__init__(
            adapter, host_uuid, instance, connection_info, stg_ftsk=stg_ftsk)

    def _get_path(self):
        return "fake_path"


class TestFileIOVolumeAdapter(test_vol.TestVolumeAdapter):
    """Tests the FileIOVolumeAdapter.  NovaLink is a I/O host."""

    def setUp(self):
        super(TestFileIOVolumeAdapter, self).setUp()

        # Needed for the volume adapter
        self.adpt = self.useFixture(pvm_fx.AdapterFx()).adpt
        mock_inst = mock.MagicMock(uuid='2BC123')

        self.vol_drv = FakeFileIOVolAdapter(
            self.adpt, 'host_uuid', mock_inst,
            {'data': {'volume_id': 'a_vol_id'},
             'serial': 'volid1'})

        self.fake_vios = pvm_vios.VIOS.bld(
            self.adpt, 'vios1',
            pvm_bp.PartitionMemoryConfiguration.bld(self.adpt, 1024),
            pvm_bp.PartitionMemoryConfiguration.bld(self.adpt, 0.1, 1))
        self.feed = [pvm_vios.VIOS.wrap(self.fake_vios.entry)]
        ftskfx = pvm_fx.FeedTaskFx(self.feed)
        self.useFixture(ftskfx)

    def test_min_xags(self):
        """Ensures xag's only returns SCSI Mappings."""
        self.assertEqual([pvm_const.XAG.VIO_SMAP], self.vol_drv.min_xags())

    @mock.patch('pypowervm.tasks.scsi_mapper.add_map')
    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('pypowervm.entities.Entry.uuid',
                new_callable=mock.PropertyMock)
    @mock.patch('pypowervm.tasks.slot_map.SlotMapStore.register_vscsi_mapping')
    @mock.patch('pypowervm.tasks.client_storage.udid_to_scsi_mapping')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    @mock.patch('pypowervm.tasks.partition.get_mgmt_partition')
    @mock.patch('pypowervm.wrappers.storage.FileIO.bld')
    def test_connect_volume(self, mock_file_bld, mock_get_mgmt_partition,
                            mock_get_vm_id, mock_udid_to_map, mock_reg_map,
                            mock_get_vios_uuid, mock_build_map, mock_add_map):
        # Mockups
        mock_file = mock.Mock()
        mock_file_bld.return_value = mock_file
        mock_slot_mgr = mock.MagicMock()
        mock_slot_mgr.build_map.get_vscsi_slot.return_value = 4, 'fake_path'

        mock_vios = mock.Mock(uuid='uuid1')
        mock_get_mgmt_partition.return_value = mock_vios
        mock_get_vios_uuid.return_value = 'uuid1'
        mock_get_vm_id.return_value = 'partition_id'

        mock_udid_to_map.return_value = mock.Mock()
        mock_add_map.return_value = None

        # Invoke
        self.vol_drv.connect_volume(mock_slot_mgr)

        # Validate
        mock_file_bld.assert_called_once_with(
            self.adpt, 'fake_path',
            backstore_type=pvm_stg.BackStoreType.LOOP, tag='a_vol_id')
        self.assertEqual(1, mock_build_map.call_count)
        self.assertEqual(1, mock_udid_to_map.call_count)

    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('pypowervm.entities.Entry.uuid',
                new_callable=mock.PropertyMock)
    @mock.patch('pypowervm.tasks.slot_map.SlotMapStore.register_vscsi_mapping')
    @mock.patch('pypowervm.tasks.client_storage.udid_to_scsi_mapping')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    @mock.patch('pypowervm.tasks.partition.get_mgmt_partition')
    @mock.patch('pypowervm.wrappers.storage.FileIO.bld')
    def test_connect_volume_rebuild_no_slot(
        self, mock_file_bld, mock_get_mgmt_partition, mock_get_vm_id,
        mock_udid_to_map, mock_reg_map, mock_get_vios_uuid, mock_build_map):
        # Mockups
        mock_file = mock.Mock()
        mock_file_bld.return_value = mock_file
        mock_slot_mgr = mock.MagicMock()
        mock_slot_mgr.is_rebuild = True
        mock_slot_mgr.build_map.get_vscsi_slot.return_value = None, None

        mock_vios = mock.Mock(uuid='uuid1')
        mock_get_mgmt_partition.return_value = mock_vios
        mock_get_vios_uuid.return_value = 'uuid1'

        # Invoke
        self.vol_drv.connect_volume(mock_slot_mgr)

        # Validate
        mock_file_bld.assert_called_once_with(
            self.adpt, 'fake_path',
            backstore_type=pvm_stg.BackStoreType.LOOP, tag='a_vol_id')
        self.assertEqual(0, mock_build_map.call_count)

    @mock.patch('pypowervm.tasks.partition.get_mgmt_partition', autospec=True)
    @mock.patch('pypowervm.tasks.storage.rescan_vstor', autospec=True)
    def test_extend_volume(self, mock_rescan, mock_get_mgmt_partition):
        # FileIO driver can only have 1 uuid in vol_drv.vios_uuids
        mock_vios = mock.Mock(uuid='uuid1')
        mock_get_mgmt_partition.return_value = mock_vios
        self.vol_drv.extend_volume()
        mock_rescan.assert_called_once_with(self.vol_drv.vios_uuids[0],
                                            "fake_path", adapter=self.adpt)
        mock_rescan.side_effect = pvm_exc.JobRequestFailed(
            operation_name='RescanVirtualDisk', error='fake_err')
        self.assertRaises(p_exc.VolumeExtendFailed, self.vol_drv.extend_volume)
        mock_rescan.side_effect = pvm_exc.VstorNotFound(
            stor_udid='stor_udid', vios_uuid='uuid')
        self.assertRaises(p_exc.VolumeExtendFailed, self.vol_drv.extend_volume)

    @mock.patch('pypowervm.entities.Entry.uuid',
                new_callable=mock.PropertyMock)
    @mock.patch('pypowervm.tasks.partition.get_mgmt_partition')
    @mock.patch('pypowervm.tasks.scsi_mapper.gen_match_func')
    @mock.patch('pypowervm.tasks.scsi_mapper.find_maps')
    def test_disconnect_volume(self, mock_find_maps, mock_gen_match_func,
                               mock_get_mgmt_partition, mock_entry_uuid):
        # Mockups
        mock_slot_mgr = mock.MagicMock()

        mock_vios = mock.Mock(uuid='uuid1')
        mock_get_mgmt_partition.return_value = mock_vios

        mock_match_func = mock.Mock()
        mock_gen_match_func.return_value = mock_match_func
        mock_entry_uuid.return_value = 'uuid1'
        # Invoke
        self.vol_drv._disconnect_volume(mock_slot_mgr)

        # Validate
        mock_gen_match_func.assert_called_once_with(
            pvm_stg.VDisk, names=['fake_path'])
        mock_find_maps.assert_called_once_with(
            mock.ANY,
            client_lpar_id='2BC123', match_func=mock_match_func)

    @mock.patch('os.path.exists')
    def test_pre_live_migration_on_destination(self, mock_path_exists):
        mock_path_exists.return_value = False
        self.assertRaises(
            p_exc.VolumePreMigrationFailed,
            self.vol_drv.pre_live_migration_on_destination, mock.ANY)

    @mock.patch('nova_powervm.virt.powervm.volume.fileio.FileIOVolumeAdapter.'
                'vios_uuids', new_callable=mock.PropertyMock)
    def test_is_volume_on_vios(self, mock_vios_uuids):
        mock_vios_uuids.return_value = ['uuid1']
        vol_found, vol_path = self.vol_drv.is_volume_on_vios(
            mock.Mock(uuid='uuid2'))
        self.assertFalse(vol_found)
        self.assertIsNone(vol_path)
