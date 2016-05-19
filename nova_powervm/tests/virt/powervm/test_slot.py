# Copyright 2016 IBM Corp.
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


class TestNovaSlotManager(test.TestCase):

    def setUp(self):
        super(TestNovaSlotManager, self).setUp()
        self.store_api = mock.MagicMock()
        self.inst = mock.MagicMock(uuid='uuid1')

    def test_build_slot_mgr(self):
        # Test when NVRAM store exists
        # The Swift-backed implementation of PowerVM SlotMapStore is returned
        self.store_api.fetch_slot_map = mock.MagicMock(return_value=None)
        self.assertIsInstance(
            slot.build_slot_mgr(self.inst, self.store_api, adapter=None,
                                vol_drv_iter=None),
            slot.SwiftSlotManager)

        # Test when no NVRAM store is set up
        # The no-op implementation of PowerVM SlotMapStore is returned
        self.assertIsInstance(
            slot.build_slot_mgr(self.inst, None, adapter=None,
                                vol_drv_iter=None),
            slot.NoopSlotManager)


class TestSwiftSlotManager(test.TestCase):

    def setUp(self):
        super(TestSwiftSlotManager, self).setUp()
        self.store_api = mock.MagicMock()
        self.store_api.fetch_slot_map = mock.MagicMock(return_value=None)
        self.inst = mock.MagicMock(uuid='uuid1')
        self.slot_mgr = slot.SwiftSlotManager(self.store_api,
                                              instance=self.inst)

    def test_load(self):
        # Run load
        self.slot_mgr.load()

        # Validate the call
        self.store_api.fetch_slot_map.assert_called_with('uuid1_slot_map')

    def test_save(self):
        # Mock the call
        self.store_api.store_slot_map = mock.MagicMock()

        # Run save
        self.slot_mgr.save()

        # Validate the call
        self.store_api.store_slot_map.assert_called_once_with(
            'uuid1_slot_map', mock.ANY)

    def test_delete(self):
        # Mock the call
        self.store_api.delete_slot_map = mock.MagicMock()

        # Run delete
        self.slot_mgr.delete()

        # Validate the call
        self.store_api.delete_slot_map.assert_called_once_with(
            'uuid1_slot_map')

    @mock.patch('pypowervm.tasks.slot_map.RebuildSlotMap')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.get')
    def test_init_recreate_map(self, mock_vios_get, mock_rebuild_slot):
        vios1, vios2 = mock.Mock(uuid='uuid1'), mock.Mock(uuid='uuid2')
        mock_vios_get.return_value = [vios1, vios2]
        self.slot_mgr.init_recreate_map(mock.Mock(), self._vol_drv_iter())
        mock_rebuild_slot.assert_called_once_with(
            self.slot_mgr, mock.ANY, {'udid': ['uuid2']}, ['a', 'b'])

    @mock.patch('pypowervm.tasks.slot_map.RebuildSlotMap')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.get')
    def test_init_recreate_map_fails(self, mock_vios_get, mock_rebuild_slot):
        vios1, vios2 = mock.Mock(uuid='uuid1'), mock.Mock(uuid='uuid2')
        mock_vios_get.return_value = [vios1, vios2]
        mock_rebuild_slot.side_effect = (
            pvm_exc.InvalidHostForRebuildNotEnoughVIOS(udid='udid56'))
        self.assertRaises(
            p_exc.InvalidRebuild, self.slot_mgr.init_recreate_map, mock.Mock(),
            self._vol_drv_iter())

    def _vol_drv_iter(self):
        mock_scsi = mock.Mock()
        mock_scsi.vol_type.return_value = 'vscsi'
        mock_scsi.is_volume_on_vios.side_effect = ((False, None),
                                                   (True, 'udid'))

        mock_npiv1 = mock.Mock()
        mock_npiv1.vol_type.return_value = 'npiv'
        mock_npiv1._fabric_names.return_value = ['a', 'b']

        mock_npiv2 = mock.Mock()
        mock_npiv2.vol_type.return_value = 'npiv'
        mock_npiv2._fabric_names.return_value = ['a', 'b', 'c']

        vol_drv = [mock_scsi, mock_npiv1, mock_npiv2]
        for type in vol_drv:
            yield mock.Mock(), type
