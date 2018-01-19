# Copyright 2016, 2017 IBM Corp.
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

import mock

from nova import test
from nova_powervm.virt.powervm import exception as p_exc
from nova_powervm.virt.powervm import slot

from pypowervm import exceptions as pvm_exc


class TestNovaSlotManager(test.NoDBTestCase):

    def setUp(self):
        super(TestNovaSlotManager, self).setUp()
        self.store_api = mock.MagicMock()
        self.inst = mock.MagicMock(uuid='uuid1')

    def test_build_slot_mgr(self):
        # Test when NVRAM store exists
        # The Swift-backed implementation of PowerVM SlotMapStore is returned
        self.store_api.fetch_slot_map = mock.MagicMock(return_value=None)
        slot_mgr = slot.build_slot_mgr(self.inst, self.store_api, adapter=None,
                                       vol_drv_iter=None)
        self.assertIsInstance(slot_mgr, slot.SwiftSlotManager)
        self.assertFalse(slot_mgr.is_rebuild)

        # Test when no NVRAM store is set up
        # The no-op implementation of PowerVM SlotMapStore is returned
        self.assertIsInstance(
            slot.build_slot_mgr(self.inst, None, adapter=None,
                                vol_drv_iter=None),
            slot.NoopSlotManager)

        # Test that the rebuild flag is set when it is flagged as a rebuild
        slot_mgr = slot.build_slot_mgr(
            self.inst, self.store_api, adapter='adpt', vol_drv_iter='test')
        self.assertTrue(slot_mgr.is_rebuild)


class TestSwiftSlotManager(test.NoDBTestCase):

    def setUp(self):
        super(TestSwiftSlotManager, self).setUp()
        self.store_api = mock.MagicMock()
        self.store_api.fetch_slot_map = mock.MagicMock(return_value=None)
        self.inst = mock.MagicMock(uuid='a2e71b38-160f-4650-bbdc-2a10cd507e2b')
        self.slot_mgr = slot.SwiftSlotManager(self.store_api,
                                              instance=self.inst)

    def test_load(self):
        # load() should have been called internally by __init__
        self.store_api.fetch_slot_map.assert_called_with(
            self.inst.uuid + '_slot_map')

    def test_save(self):
        # Mock the call
        self.store_api.store_slot_map = mock.MagicMock()

        # Run save
        self.slot_mgr.save()

        # Not called because nothing changed
        self.store_api.store_slot_map.assert_not_called()

        # Change something
        mock_vfcmap = mock.Mock(server_adapter=mock.Mock(lpar_slot_num=123))
        self.slot_mgr.register_vfc_mapping(mock_vfcmap, 'fabric')

        # Run save
        self.slot_mgr.save()

        # Validate the call
        self.store_api.store_slot_map.assert_called_once_with(
            self.inst.uuid + '_slot_map', mock.ANY)

    def test_delete(self):
        # Mock the call
        self.store_api.delete_slot_map = mock.MagicMock()

        # Run delete
        self.slot_mgr.delete()

        # Validate the call
        self.store_api.delete_slot_map.assert_called_once_with(
            self.inst.uuid + '_slot_map')

    @mock.patch('pypowervm.tasks.slot_map.RebuildSlotMap', autospec=True)
    @mock.patch('pypowervm.tasks.storage.ComprehensiveScrub', autospec=True)
    def test_init_recreate_map(self, mock_ftsk, mock_rebuild_slot):
        vios1, vios2 = mock.Mock(uuid='uuid1'), mock.Mock(uuid='uuid2')
        mock_ftsk.return_value.feed = [vios1, vios2]
        self.slot_mgr.init_recreate_map(mock.Mock(), self._vol_drv_iter())
        self.assertEqual(1, mock_ftsk.call_count)
        mock_rebuild_slot.assert_called_once_with(
            self.slot_mgr, mock.ANY, {'udid': ['uuid2'], 'iscsi': ['uuid1']},
            ['a', 'b'])

    @mock.patch('pypowervm.tasks.slot_map.RebuildSlotMap', autospec=True)
    @mock.patch('pypowervm.tasks.storage.ComprehensiveScrub', autospec=True)
    def test_init_recreate_map_fails(self, mock_ftsk, mock_rebuild_slot):
        vios1, vios2 = mock.Mock(uuid='uuid1'), mock.Mock(uuid='uuid2')
        mock_ftsk.return_value.feed = [vios1, vios2]
        mock_rebuild_slot.side_effect = (
            pvm_exc.InvalidHostForRebuildNotEnoughVIOS(udid='udid56'))
        self.assertRaises(
            p_exc.InvalidRebuild, self.slot_mgr.init_recreate_map, mock.Mock(),
            self._vol_drv_iter())

    @mock.patch('pypowervm.tasks.slot_map.RebuildSlotMap', autospec=True)
    @mock.patch('pypowervm.tasks.storage.ComprehensiveScrub', autospec=True)
    def test_init_recreate_map_fileio(self, mock_ftsk, mock_rebuild_slot):
        vios1, vios2 = mock.Mock(uuid='uuid1'), mock.Mock(uuid='uuid2')
        mock_ftsk.return_value.feed = [vios1, vios2]
        expected_vio_wrap = [vios1, vios2]
        self.slot_mgr.init_recreate_map(mock.Mock(), self._vol_drv_iter_2())
        self.assertEqual(1, mock_ftsk.call_count)
        mock_rebuild_slot.assert_called_once_with(
            self.slot_mgr, expected_vio_wrap,
            {'udidvscsi': ['uuid1'], 'udid': ['uuid1']}, [])

    def _vol_drv_iter_2(self):
        mock_fileio = mock.Mock()
        mock_fileio.vol_type.return_value = 'fileio'
        mock_fileio.is_volume_on_vios.side_effect = ((True, 'udid'),
                                                     (False, None))
        mock_scsi = mock.Mock()
        mock_scsi.vol_type.return_value = 'vscsi'
        mock_scsi.is_volume_on_vios.side_effect = ((True, 'udidvscsi'),
                                                   (False, None))

        vol_drv = [mock_fileio, mock_scsi]
        for type in vol_drv:
            yield mock.Mock(), type

    def _vol_drv_iter(self):
        mock_scsi = mock.Mock()
        mock_scsi.vol_type.return_value = 'vscsi'
        mock_scsi.is_volume_on_vios.side_effect = ((False, None),
                                                   (True, 'udid'))
        mock_iscsi = mock.Mock()
        mock_iscsi.vol_type.return_value = 'iscsi'
        mock_iscsi.is_volume_on_vios.side_effect = ((True, 'iscsi'),
                                                    (False, None))

        mock_npiv1 = mock.Mock()
        mock_npiv1.vol_type.return_value = 'npiv'
        mock_npiv1._fabric_names.return_value = ['a', 'b']

        mock_npiv2 = mock.Mock()
        mock_npiv2.vol_type.return_value = 'npiv'
        mock_npiv2._fabric_names.return_value = ['a', 'b', 'c']

        vol_drv = [mock_scsi, mock_npiv1, mock_npiv2, mock_iscsi]
        for type in vol_drv:
            yield mock.Mock(), type
