# Copyright 2015, 2016 IBM Corp.
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

from nova_powervm import conf as cfg
from nova_powervm.tests.virt.powervm.volume import test_driver as test_vol
from nova_powervm.virt.powervm import exception as p_exc
from nova_powervm.virt.powervm.volume import vscsi

from pypowervm import const as pvm_const
from pypowervm.tasks import hdisk
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.tests.test_utils import pvmhttp
from pypowervm.wrappers import storage as pvm_stor
from pypowervm.wrappers import virtual_io_server as pvm_vios

CONF = cfg.CONF

VIOS_FEED = 'fake_vios_feed2.txt'
VIOS_FEED_MULTI = 'fake_vios_feed_multi.txt'

I_WWPN_1 = '21000024FF649104'
I_WWPN_2 = '21000024FF649105'

I2_WWPN_1 = '10000090FA5371F2'
I2_WWPN_2 = '10000090FA53720A'


class BaseVSCSITest(test_vol.TestVolumeAdapter):
    """Basic test case for the VSCSI Volume Connector."""

    def setUp(self, vios_feed_file, p_wwpn1, p_wwpn2):
        super(BaseVSCSITest, self).setUp()
        self.adpt = self.useFixture(pvm_fx.AdapterFx()).adpt

        def resp(file_name):
            return pvmhttp.load_pvm_resp(
                file_name, adapter=self.adpt).get_response()
        self.vios_feed_resp = resp(vios_feed_file)

        self.feed = pvm_vios.VIOS.wrap(self.vios_feed_resp)
        self.ft_fx = pvm_fx.FeedTaskFx(self.feed)
        self.useFixture(self.ft_fx)

        self.adpt.read.return_value = self.vios_feed_resp

        @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.getter')
        @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
        def init_vol_adpt(mock_pvm_uuid, mock_getter):
            con_info = {
                'data': {
                    'initiator_target_map': {
                        p_wwpn1: ['t1'],
                        p_wwpn2: ['t2', 't3']
                    },
                    'target_lun': '1',
                    'volume_id': 'id'
                },
            }
            mock_inst = mock.MagicMock()
            mock_pvm_uuid.return_value = '1234'

            # The getter can just return the VIOS values (to remove a read
            # that would otherwise need to be mocked).
            mock_getter.return_value = self.feed

            return vscsi.VscsiVolumeAdapter(self.adpt, 'host_uuid', mock_inst,
                                            con_info)
        self.vol_drv = init_vol_adpt()


class TestVSCSIAdapter(BaseVSCSITest):
    """Tests the vSCSI Volume Connector Adapter.  Single VIOS tests"""

    def setUp(self):
        super(TestVSCSIAdapter, self).setUp(
            VIOS_FEED, I_WWPN_1, I_WWPN_2)

        # setup system_metadata tests
        self.volume_id = 'f042c68a-c5a5-476a-ba34-2f6d43f4226c'
        self.vios_uuid = '3443DB77-AED1-47ED-9AA5-3DB9C6CF7089'
        self.udid = (
            '01M0lCTTIxNDUxMjQ2MDA1MDc2ODAyODI4NjFEODgwMDAwMDAwMDAwMDA1Rg==')
        self.slot_mgr = mock.Mock()
        self.slot_mgr.build_map.get_vscsi_slot.return_value = 62, 'the_lua'

    @mock.patch('pypowervm.tasks.hdisk.lua_recovery')
    @mock.patch('pypowervm.tasks.storage.find_stale_lpars')
    def test_pre_live_migration(self, mock_fsl, mock_discover):
        # The mock return values
        mock_fsl.return_value = []
        mock_discover.return_value = (
            hdisk.LUAStatus.DEVICE_AVAILABLE, 'devname', 'udid')

        # Run the method
        self.vol_drv.pre_live_migration_on_destination({})

        # Test exception path
        mock_discover.return_value = (
            hdisk.LUAStatus.ITL_NOT_RELIABLE, 'devname', 'udid')

        # Run the method
        self.assertRaises(p_exc.VolumePreMigrationFailed,
                          self.vol_drv.pre_live_migration_on_destination, {})

    @mock.patch('pypowervm.tasks.hdisk.remove_hdisk')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.hdisk_from_uuid')
    def test_cleanup_volume(self, mock_hdisk_from_uuid, mock_remove_hdisk):
        mock_hdisk_from_uuid.return_value = 'device_name'

        # Bad path.  udid not found
        # Run the method - this should produce a warning
        with self.assertLogs(vscsi.__name__, 'WARNING'):
            self.vol_drv._cleanup_volume(None)

        # Good path
        self.vol_drv._cleanup_volume('udid1')
        # We don't update the feed, we run remove hdisk instead
        self.assertEqual(0, self.ft_fx.patchers['update'].mock.call_count)
        mock_remove_hdisk.assert_called_once_with(
            self.adpt, mock.ANY, 'device_name', self.vios_uuid)

    def test_post_live_migr_source(self):

        # Bad path.  volume id not found
        bad_data = {'vscsi-BAD': 'udid1'}
        # good path.
        good_data = {'vscsi-id': 'udid1'}

        with mock.patch.object(self.vol_drv, '_cleanup_volume') as mock_cln:
            self.vol_drv.post_live_migration_at_source(bad_data)
            mock_cln.assert_called_once_with(None)

            mock_cln.reset_mock()
            self.vol_drv.post_live_migration_at_source(good_data)
            mock_cln.assert_called_once_with('udid1')

    def test_cleanup_at_dest(self):

        # Bad path.  volume id not found
        bad_data = {'vscsi-BAD': 'udid1'}
        # good path.
        good_data = {'vscsi-id': 'udid1'}

        with mock.patch.object(self.vol_drv, '_cleanup_volume') as mock_cln:
            self.vol_drv.cleanup_volume_at_destination(bad_data)
            mock_cln.assert_called_once_with(None)

            mock_cln.reset_mock()
            self.vol_drv.cleanup_volume_at_destination(good_data)
            mock_cln.assert_called_once_with('udid1')

    @mock.patch('pypowervm.tasks.scsi_mapper.add_map')
    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('pypowervm.tasks.hdisk.lua_recovery')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_connect_volume(self, mock_get_vm_id, mock_lua_recovery,
                            mock_build_map, mock_add_map):
        # The mock return values
        mock_lua_recovery.return_value = (
            hdisk.LUAStatus.DEVICE_AVAILABLE, 'devname', 'udid')
        mock_get_vm_id.return_value = 'partition_id'

        def build_map_func(host_uuid, vios_w, lpar_uuid, pv,
                           lpar_slot_num=None, lua=None):
            self.assertEqual('host_uuid', host_uuid)
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertEqual('1234', lpar_uuid)
            self.assertIsInstance(pv, pvm_stor.PV)
            self.assertEqual(62, lpar_slot_num)
            self.assertEqual('the_lua', lua)
            return 'fake_map'

        mock_build_map.side_effect = build_map_func

        # Run the method
        self.vol_drv.connect_volume(self.slot_mgr)

        # As initialized above, remove_maps returns True to trigger update.
        self.assertEqual(1, mock_add_map.call_count)
        self.assertEqual(1, self.ft_fx.patchers['update'].mock.call_count)
        self.assertEqual(1, mock_build_map.call_count)

    @mock.patch('pypowervm.tasks.scsi_mapper.add_map')
    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('pypowervm.tasks.hdisk.discover_hdisk')
    @mock.patch('nova_powervm.virt.powervm.volume.vscsi.VscsiVolumeAdapter.'
                '_validate_vios_on_connection')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_connect_volume_no_update(
        self, mock_get_vm_id, mock_validate_vioses, mock_disc_hdisk,
        mock_build_map, mock_add_map):
        """Make sure we don't do an actual update of the VIOS if not needed."""
        # The mock return values
        mock_build_map.return_value = 'fake_map'
        mock_add_map.return_value = None
        mock_get_vm_id.return_value = 'partition_id'
        mock_disc_hdisk.return_value = (hdisk.LUAStatus.DEVICE_AVAILABLE,
                                        'devname', 'udid')

        # Run the method
        self.vol_drv.connect_volume(self.slot_mgr)

        # As initialized above, remove_maps returns True to trigger update.
        mock_validate_vioses.assert_called_with(1)
        self.assertEqual(1, mock_add_map.call_count)
        self.assertEqual(0, self.ft_fx.patchers['update'].mock.call_count)
        self.assertEqual(1, mock_disc_hdisk.call_count)

    @mock.patch('pypowervm.tasks.hdisk.build_itls')
    @mock.patch('pypowervm.tasks.hdisk.lua_recovery')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_vscsi_mapping')
    @mock.patch('nova_powervm.virt.powervm.volume.vscsi.VscsiVolumeAdapter.'
                '_validate_vios_on_connection')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_connect_volume_to_initiators(
        self, mock_get_vm_id, mock_validate_vioses, mock_add_vscsi_mapping,
        mock_lua_recovery, mock_build_itls):
        """Tests that the connect w/out initiators throws errors."""
        mock_lua_recovery.return_value = (
            hdisk.LUAStatus.DEVICE_AVAILABLE, 'devname', 'udid')
        mock_get_vm_id.return_value = 'partition_id'

        mock_instance = mock.Mock()
        mock_instance.system_metadata = {}

        mock_validate_vioses.side_effect = p_exc.VolumeAttachFailed(
            volume_id='1', reason='message', instance_name='inst')

        mock_build_itls.return_value = []
        self.assertRaises(p_exc.VolumeAttachFailed,
                          self.vol_drv.connect_volume, self.slot_mgr)

        # Validate that the validate was called with no vioses.
        mock_validate_vioses.assert_called_with(0)

    def test_validate_vios_on_connection(self):
        # Happy path!
        self.vol_drv._validate_vios_on_connection(1)

        # Raise if no VIOSes are found
        self.assertRaises(p_exc.VolumeAttachFailed,
                          self.vol_drv._validate_vios_on_connection, 0)

        # Multi VIOS required happy path.
        self.flags(vscsi_vios_connections_required=2, group='powervm')
        self.vol_drv._validate_vios_on_connection(2)

        # Raise if multiple VIOSes required
        self.assertRaises(p_exc.VolumeAttachFailed,
                          self.vol_drv._validate_vios_on_connection, 1)

    @mock.patch('pypowervm.tasks.hdisk.remove_hdisk')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.hdisk_from_uuid')
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_disconnect_volume(self, mock_get_vm_id, mock_remove_maps,
                               mock_hdisk_from_uuid, mock_remove_hdisk):
        # The mock return values
        mock_hdisk_from_uuid.return_value = 'device_name'
        mock_get_vm_id.return_value = 'partition_id'
        self.vol_drv._set_udid('UDIDIT!')

        def validate_remove_maps(vios_w, vm_uuid, match_func):
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertEqual('partition_id', vm_uuid)
            return 'removed'
        mock_remove_maps.side_effect = validate_remove_maps

        # Run the method
        self.vol_drv.disconnect_volume(self.slot_mgr)

        # As initialized above, remove_maps returns True to trigger update.
        self.assertEqual(1, mock_remove_maps.call_count)
        self.assertEqual(1, self.ft_fx.patchers['update'].mock.call_count)
        mock_remove_hdisk.assert_called_once_with(
            self.adpt, mock.ANY, 'device_name', self.vios_uuid)

    @mock.patch('pypowervm.tasks.scsi_mapper.find_maps')
    @mock.patch('pypowervm.tasks.hdisk.remove_hdisk')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.hdisk_from_uuid')
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_disconnect_volume_shared(self, mock_get_vm_id, mock_remove_maps,
                                      mock_hdisk_from_uuid, mock_remove_hdisk,
                                      mock_find_maps):
        # The mock return values
        mock_hdisk_from_uuid.return_value = 'device_name'
        mock_get_vm_id.return_value = 'partition_id'
        # Consider there are multiple attachments
        mock_find_maps.return_value = [mock.MagicMock(), mock.MagicMock()]
        self.vol_drv._set_udid('UDIDIT!')

        def validate_remove_maps(vios_w, vm_uuid, match_func):
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertEqual('partition_id', vm_uuid)
            return 'removed'
        mock_remove_maps.side_effect = validate_remove_maps

        # Run the method
        self.vol_drv.disconnect_volume(self.slot_mgr)

        # As initialized above, remove_maps returns True to trigger update.
        self.assertEqual(1, mock_remove_maps.call_count)
        self.assertEqual(1, self.ft_fx.patchers['update'].mock.call_count)
        # Since device has multiple mappings remove disk should not get called
        self.assertEqual(0, mock_remove_hdisk.call_count)

    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.hdisk_from_uuid')
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_disconnect_volume_no_update(
            self, mock_get_vm_id, mock_remove_maps, mock_hdisk_from_uuid):
        """Validate that if no maps removed, the VIOS update is not called."""
        # The mock return values
        mock_remove_maps.return_value = []
        mock_hdisk_from_uuid.return_value = 'device_name'
        mock_get_vm_id.return_value = 'partition_id'
        self.vol_drv._set_udid('UDIDIT!')

        # Run the method
        self.vol_drv.disconnect_volume(self.slot_mgr)

        # As initialized above, remove_maps returns True to trigger update.
        self.assertEqual(1, mock_remove_maps.call_count)
        self.assertEqual(0, self.ft_fx.patchers['update'].mock.call_count)

    @mock.patch('pypowervm.tasks.hdisk.good_discovery')
    @mock.patch('pypowervm.tasks.hdisk.remove_hdisk')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.hdisk_from_uuid')
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_disconnect_volume_no_udid(
            self, mock_get_vm_id, mock_remove_maps, mock_hdisk_from_uuid,
            mock_remove_hdisk, mock_good_discover):

        # The mock return values
        mock_hdisk_from_uuid.return_value = 'device_name'
        mock_get_vm_id.return_value = 'partition_id'
        mock_good_discover.return_value = True

        def validate_remove_maps(vios_w, vm_uuid, match_func):
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertEqual('partition_id', vm_uuid)
            return 'removed'
        mock_remove_maps.side_effect = validate_remove_maps

        with mock.patch.object(
            self.vol_drv, '_discover_volume_on_vios',
            return_value=('status', 'dev_name', 'udidit')):

            # Run the method
            self.vol_drv.disconnect_volume(self.slot_mgr)

        # As initialized above, remove_maps returns True to trigger update.
        self.assertEqual(1, mock_remove_maps.call_count)
        self.assertEqual(1, self.ft_fx.patchers['update'].mock.call_count)
        mock_remove_hdisk.assert_called_once_with(
            self.adpt, mock.ANY, 'dev_name', self.vios_uuid)

    @mock.patch('pypowervm.tasks.hdisk.good_discovery')
    @mock.patch('pypowervm.tasks.hdisk.remove_hdisk')
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps')
    def test_disconnect_volume_no_udid_on_discover(
            self, mock_remove_maps, mock_remove_hdisk, mock_good_discover):
        """Ensures that if the UDID can not be found, no disconnect."""
        mock_good_discover.return_value = False
        with mock.patch.object(
            self.vol_drv, '_discover_volume_on_vios',
            return_value=('status', 'dev_name', None)):

            # Run the method
            self.vol_drv.disconnect_volume(self.slot_mgr)

        # As initialized above, remove_maps returns True to trigger update.
        self.assertEqual(0, mock_remove_maps.call_count)
        self.assertEqual(0, self.ft_fx.patchers['update'].mock.call_count)
        self.assertEqual(0, mock_remove_hdisk.call_count)

    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.hdisk_from_uuid')
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps')
    def test_disconnect_volume_no_valid_vio(self, mock_remove_maps,
                                            mock_hdisk_from_uuid):
        """Validate that if all VIOSes are invalid, the vio updates are 0."""
        # The mock return values
        mock_remove_maps.return_value = None
        mock_hdisk_from_uuid.return_value = None

        # Run the method.  No disconnects should yield a LOG.warning.
        with self.assertLogs(vscsi.__name__, 'WARNING'):
            self.vol_drv.disconnect_volume(self.slot_mgr)

        # As initialized above, remove_maps returns True to trigger update.
        self.assertEqual(0, mock_remove_maps.call_count)
        self.assertEqual(0, self.ft_fx.patchers['update'].mock.call_count)

    @mock.patch('nova_powervm.virt.powervm.vios.get_physical_wwpns')
    def test_wwpns(self, mock_vio_wwpns):
        mock_vio_wwpns.return_value = ['aa', 'bb']

        wwpns = self.vol_drv.wwpns()

        self.assertListEqual(['aa', 'bb'], wwpns)

    def test_min_xags(self):
        xags = self.vol_drv.min_xags()
        self.assertEqual(1, len(xags))
        self.assertIn(pvm_const.XAG.VIO_SMAP, xags)

    def test_vol_type(self):
        self.assertEqual('vscsi', self.vol_drv.vol_type())

    def test_set_udid(self):

        # Mock connection info
        self.vol_drv.connection_info['data'][vscsi.UDID_KEY] = None

        # Set the UDID
        self.vol_drv._set_udid(self.udid)

        # Verify
        self.assertEqual(self.udid,
                         self.vol_drv.connection_info['data'][vscsi.UDID_KEY])

    def test_get_udid(self):

        # Set the value to retrieve
        self.vol_drv.connection_info['data'][vscsi.UDID_KEY] = self.udid
        retrieved_udid = self.vol_drv._get_udid()
        # Check key found
        self.assertEqual(self.udid, retrieved_udid)

        # Check key not found
        self.vol_drv.connection_info['data'].pop(vscsi.UDID_KEY)
        retrieved_udid = self.vol_drv._get_udid()
        # Check key not found
        self.assertIsNone(retrieved_udid)

    def test_get_hdisk_itls(self):
        """Validates the _get_hdisk_itls method."""

        mock_vios = mock.MagicMock()
        mock_vios.get_active_pfc_wwpns.return_value = [I_WWPN_1]

        i_wwpn, t_wwpns, lun = self.vol_drv._get_hdisk_itls(mock_vios)
        self.assertListEqual([I_WWPN_1], i_wwpn)
        self.assertListEqual(['t1'], t_wwpns)
        self.assertEqual('1', lun)

        mock_vios.get_active_pfc_wwpns.return_value = [I_WWPN_2]
        i_wwpn, t_wwpns, lun = self.vol_drv._get_hdisk_itls(mock_vios)
        self.assertListEqual([I_WWPN_2], i_wwpn)
        self.assertListEqual(['t2', 't3'], t_wwpns)

        mock_vios.get_active_pfc_wwpns.return_value = ['12345']
        i_wwpn, t_wwpns, lun = self.vol_drv._get_hdisk_itls(mock_vios)
        self.assertListEqual([], i_wwpn)

    @mock.patch('pypowervm.tasks.scsi_mapper.find_maps')
    def test_check_host_mappings(self, mock_find):
        mock_vios = mock.MagicMock()
        mock_vios.uuid = self.vios_uuid
        # Test when multiple matching entries found
        mock_find.return_value = [mock.MagicMock(), mock.MagicMock()]
        mapping = self.vol_drv._check_host_mappings(mock_vios, self.volume_id)
        self.assertTrue(mapping)
        # Test when single entry found check host mapping should return False
        mock_find.return_value = [mock.MagicMock()]
        mapping = self.vol_drv._check_host_mappings(mock_vios, self.volume_id)
        self.assertFalse(mapping)
        # Test when no entry found check host mapping should return False
        mock_find.return_value = []
        mapping = self.vol_drv._check_host_mappings(mock_vios, self.volume_id)
        self.assertFalse(mapping)


class TestVSCSIAdapterMultiVIOS(BaseVSCSITest):
    """Tests the vSCSI Volume Connector Adapter against multiple VIOSes."""

    def setUp(self):
        super(TestVSCSIAdapterMultiVIOS, self).setUp(
            VIOS_FEED_MULTI, I2_WWPN_1, I2_WWPN_2)
        self.slot_mgr = mock.Mock()
        self.slot_mgr.build_map.get_vscsi_slot.return_value = 62, 'the_lua'

    @mock.patch('pypowervm.tasks.scsi_mapper.add_map')
    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('pypowervm.tasks.hdisk.discover_hdisk')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_connect_volume_multi_vio(self, mock_vm_id, mock_discover_hdisk,
                                      mock_build_map, mock_add_map):
        # The mock return values
        mock_discover_hdisk.return_value = (
            hdisk.LUAStatus.DEVICE_AVAILABLE, 'devname', 'udid')
        mock_vm_id.return_value = 'partition_id'

        def build_map_func(host_uuid, vios_w, lpar_uuid, pv,
                           lpar_slot_num=None, lua=None):
            self.assertEqual('host_uuid', host_uuid)
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertEqual('1234', lpar_uuid)
            self.assertIsInstance(pv, pvm_stor.PV)
            self.assertEqual(62, lpar_slot_num)
            self.assertEqual('the_lua', lua)
            return 'fake_map'

        mock_build_map.side_effect = build_map_func

        # Run the method
        self.vol_drv.connect_volume(self.slot_mgr)

        # As initialized above, remove_maps returns True to trigger update.
        self.assertEqual(2, mock_add_map.call_count)
        self.assertEqual(2, mock_build_map.call_count)

        # Two of the calls are for the slots, two are for the add mappings
        self.assertEqual(2, self.ft_fx.patchers['update'].mock.call_count)
