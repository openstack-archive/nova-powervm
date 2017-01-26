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
from pypowervm import const as pvm_const
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.wrappers import storage as pvm_stg

from nova_powervm.tests.virt.powervm.volume import test_driver as test_vol
from nova_powervm.virt.powervm.volume import fileio as v_drv


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

        self.vol_drv = FakeFileIOVolAdapter(self.adpt, 'host_uuid', mock_inst,
                                            None)

        self.mock_vio_task = mock.MagicMock()
        self.mock_stg_ftsk = mock.MagicMock(
            wrapper_tasks={'vios_uuid': self.mock_vio_task})
        self.vol_drv.stg_ftsk = self.mock_stg_ftsk

    def test_min_xags(self):
        """Ensures xag's only returns SCSI Mappings."""
        self.assertEqual([pvm_const.XAG.VIO_SMAP], self.vol_drv.min_xags())

    @mock.patch('pypowervm.tasks.partition.get_mgmt_partition')
    @mock.patch('pypowervm.wrappers.storage.FileIO.bld')
    def test_connect_volume(self, mock_file_bld, mock_get_mgmt_partition):
        # Mockups
        mock_file = mock.Mock()
        mock_file_bld.return_value = mock_file
        mock_slot_mgr = mock.MagicMock()

        mock_vios = mock.Mock(uuid='vios_uuid')
        mock_get_mgmt_partition.return_value = mock_vios

        # Invoke
        self.vol_drv._connect_volume(mock_slot_mgr)

        # Validate
        mock_file_bld.assert_called_once_with(self.adpt, 'fake_path')

    @mock.patch('pypowervm.tasks.partition.get_mgmt_partition')
    @mock.patch('pypowervm.tasks.scsi_mapper.gen_match_func')
    @mock.patch('pypowervm.tasks.scsi_mapper.find_maps')
    def test_disconnect_volume(self, mock_find_maps, mock_gen_match_func,
                               mock_get_mgmt_partition):
        # Mockups
        mock_slot_mgr = mock.MagicMock()

        mock_vios = mock.Mock(uuid='vios_uuid')
        mock_get_mgmt_partition.return_value = mock_vios

        mock_match_func = mock.Mock()
        mock_gen_match_func.return_value = mock_match_func

        # Invoke
        self.vol_drv._disconnect_volume(mock_slot_mgr)

        # Validate
        mock_gen_match_func.assert_called_once_with(
            pvm_stg.FileIO, names=['fake_path'])
        mock_find_maps.assert_called_once_with(
            mock_vios.scsi_mappings, client_lpar_id='2BC123',
            match_func=mock_match_func)
