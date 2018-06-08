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
from nova_powervm.virt.powervm.volume import rbd as v_drv
from pypowervm import const as pvm_const
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.wrappers import base_partition as pvm_bp
from pypowervm.wrappers import storage as pvm_stg
from pypowervm.wrappers import virtual_io_server as pvm_vios


class FakeRBDVolAdapter(v_drv.RBDVolumeAdapter):
    """Subclass for RBDVolumeAdapter, since it is abstract."""

    def __init__(self, adapter, host_uuid, instance, connection_info,
                 stg_ftsk=None):
        super(FakeRBDVolAdapter, self).__init__(
            adapter, host_uuid, instance, connection_info, stg_ftsk=stg_ftsk)


class TestRBDVolumeAdapter(test_vol.TestVolumeAdapter):
    """Tests the RBDVolumeAdapter.  NovaLink is a I/O host."""

    def setUp(self):
        super(TestRBDVolumeAdapter, self).setUp()

        # Needed for the volume adapter
        self.adpt = self.useFixture(pvm_fx.AdapterFx()).adpt
        mock_inst = mock.MagicMock(uuid='2BC123')

        self.vol_drv = FakeRBDVolAdapter(
            self.adpt, 'host_uuid', mock_inst,
            {'data': {'name': 'pool/image', 'volume_id': 'a_vol_id'},
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

    @mock.patch('pypowervm.tasks.scsi_mapper.add_map', autospec=True)
    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping',
                autospec=True)
    @mock.patch('pypowervm.entities.Entry.uuid',
                new_callable=mock.PropertyMock)
    @mock.patch('pypowervm.tasks.slot_map.SlotMapStore.register_vscsi_mapping',
                autospec=True)
    @mock.patch('pypowervm.tasks.client_storage.udid_to_scsi_mapping',
                autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    @mock.patch('pypowervm.tasks.partition.get_mgmt_partition', autospec=True)
    @mock.patch('pypowervm.wrappers.storage.RBD.bld_ref')
    def test_connect_volume(self, mock_rbd_bld_ref, mock_get_mgmt_partition,
                            mock_get_vm_id, mock_udid_to_map, mock_reg_map,
                            mock_get_vios_uuid, mock_build_map, mock_add_map):
        # Mockups
        mock_rbd = mock.Mock()
        mock_rbd_bld_ref.return_value = mock_rbd
        mock_slot_mgr = mock.MagicMock()
        mock_slot_mgr.build_map.get_vscsi_slot.return_value = 4, 'fake_path'

        mock_vios = mock.Mock(uuid='uuid1')
        mock_get_mgmt_partition.return_value = mock_vios
        mock_get_vios_uuid.return_value = 'uuid1'
        mock_get_vm_id.return_value = 'partition_id'

        mock_udid_to_map.return_value = mock.Mock()
        mock_add_map.return_value = None

        # Set user
        v_drv.CONF.powervm.rbd_user = 'tester'
        # Invoke
        self.vol_drv.connect_volume(mock_slot_mgr)
        # Validate
        mock_rbd_bld_ref.assert_called_once_with(
            self.adpt, 'pool/image', tag='a_vol_id', user='tester')
        self.assertEqual(1, mock_build_map.call_count)
        self.assertEqual(1, mock_udid_to_map.call_count)

    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping',
                autospec=True)
    @mock.patch('pypowervm.entities.Entry.uuid',
                new_callable=mock.PropertyMock)
    @mock.patch('pypowervm.tasks.slot_map.SlotMapStore.register_vscsi_mapping',
                autospec=True)
    @mock.patch('pypowervm.tasks.client_storage.udid_to_scsi_mapping',
                autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    @mock.patch('pypowervm.tasks.partition.get_mgmt_partition', autospec=True)
    @mock.patch('pypowervm.wrappers.storage.RBD.bld_ref')
    def test_connect_volume_rebuild_no_slot(
        self, mock_rbd_bld_ref, mock_get_mgmt_partition, mock_get_vm_id,
        mock_udid_to_map, mock_reg_map, mock_get_vios_uuid, mock_build_map):
        # Mockups
        mock_rbd = mock.Mock()
        mock_rbd_bld_ref.return_value = mock_rbd
        mock_slot_mgr = mock.MagicMock()
        mock_slot_mgr.is_rebuild = True
        mock_slot_mgr.build_map.get_vscsi_slot.return_value = None, None

        mock_vios = mock.Mock(uuid='uuid1')
        mock_get_mgmt_partition.return_value = mock_vios
        mock_get_vios_uuid.return_value = 'uuid1'
        # Invoke
        self.vol_drv.connect_volume(mock_slot_mgr)

        # Validate
        mock_rbd_bld_ref.assert_called_once_with(
            self.adpt, 'pool/image', tag='a_vol_id', user='')
        self.assertEqual(0, mock_build_map.call_count)

    @mock.patch('pypowervm.entities.Entry.uuid',
                new_callable=mock.PropertyMock)
    @mock.patch('pypowervm.tasks.partition.get_mgmt_partition', autospec=True)
    @mock.patch('pypowervm.tasks.scsi_mapper.gen_match_func', autospec=True)
    @mock.patch('pypowervm.tasks.scsi_mapper.find_maps', autospec=True)
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
            pvm_stg.VDisk, names=['pool/image'])
        mock_find_maps.assert_called_once_with(
            mock.ANY,
            client_lpar_id='2BC123', match_func=mock_match_func)

    @mock.patch('nova_powervm.virt.powervm.volume.rbd.RBDVolumeAdapter.'
                'vios_uuids', new_callable=mock.PropertyMock)
    @mock.patch('nova_powervm.virt.powervm.volume.rbd.RBDVolumeAdapter.'
                'is_volume_on_vios')
    def test_pre_live_migration_on_destination(self, mock_on_vios, mock_uuids):
        mock_uuids.return_value = ['uuid1']
        mock_on_vios.return_value = (True, 'pool/image')
        self.vol_drv.pre_live_migration_on_destination(mock.ANY)
        mock_on_vios.return_value = (False, None)
        self.assertRaises(
            p_exc.VolumePreMigrationFailed,
            self.vol_drv.pre_live_migration_on_destination, mock.ANY)

    @mock.patch('nova_powervm.virt.powervm.volume.rbd.RBDVolumeAdapter.'
                'vios_uuids', new_callable=mock.PropertyMock)
    @mock.patch('pypowervm.tasks.hdisk.rbd_exists', autospec=True)
    def test_is_volume_on_vios(self, mock_exists, mock_vios_uuids):
        mock_exists.return_value = True
        mock_vios_uuids.return_value = ['uuid1']
        vol_found, vol_name = self.vol_drv.is_volume_on_vios(
            mock.Mock(uuid='uuid2'))
        self.assertFalse(vol_found)
        self.assertIsNone(vol_name)
        vol_found, vol_name = self.vol_drv.is_volume_on_vios(
            mock.Mock(uuid='uuid1'))
        self.assertTrue(vol_found)
        self.assertEqual('pool/image', vol_name)
        mock_exists.return_value = False
        vol_found, vol_name = self.vol_drv.is_volume_on_vios(
            mock.Mock(uuid='uuid1'))
        self.assertFalse(vol_found)
        self.assertIsNone(vol_name)
