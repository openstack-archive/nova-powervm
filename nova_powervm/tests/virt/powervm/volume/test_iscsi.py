# Copyright 2015, 2018 IBM Corp.
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

from nova import exception as nova_exc

from nova_powervm import conf as cfg
from nova_powervm.tests.virt.powervm.volume import test_driver as test_vol
from nova_powervm.virt.powervm import exception as p_exc
from nova_powervm.virt.powervm.volume import iscsi

from pypowervm import const as pvm_const
from pypowervm import exceptions as pvm_exc
from pypowervm.tasks import hdisk
from pypowervm.tests.tasks.util import load_file
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.wrappers import storage as pvm_stor
from pypowervm.wrappers import virtual_io_server as pvm_vios

CONF = cfg.CONF

VIOS_FEED = 'fake_vios_feed.txt'


class TestISCSIAdapter(test_vol.TestVolumeAdapter):
    """Tests the vSCSI Volume Connector Adapter.  Single VIOS tests"""

    def setUp(self):
        super(TestISCSIAdapter, self).setUp()
        self.adpt = self.useFixture(pvm_fx.AdapterFx()).adpt

        self.vios_feed_resp = load_file(VIOS_FEED)

        self.feed = pvm_vios.VIOS.wrap(self.vios_feed_resp)
        self.ft_fx = pvm_fx.FeedTaskFx(self.feed)
        self.useFixture(self.ft_fx)

        self.adpt.read.return_value = self.vios_feed_resp

        @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS', autospec=True)
        @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
        @mock.patch('pypowervm.tasks.partition.get_mgmt_partition',
                    autospec=True)
        @mock.patch('pypowervm.tasks.hdisk.discover_iscsi_initiator',
                    autospec=True)
        def init_vol_adpt(mock_initiator, mock_mgmt_part, mock_pvm_uuid,
                          mock_vios):
            self.iqn = 'iqn.2016-08.com.foo:bar'
            self.lun = 1
            self.host_ip = '10.0.0.1'
            self.user = 'user'
            self.password = 'password'
            self.serial = 'f042c68a-c5a5-476a-ba34-2f6d43f4226c'
            con_info = {
                'serial': self.serial,
                'driver_volume_type': 'iscsi',
                'connector': {},
                'data': {
                    'target_iqn': self.iqn,
                    'target_lun': self.lun,
                    'target_portal': self.host_ip,
                    'auth_username': self.user,
                    'auth_password': self.password,
                    'volume_id': 'a_volume_id',
                    'auth_method': 'CHAP'
                },
            }
            self.auth_method = 'CHAP'
            multi_con_info = {
                'serial': self.serial,
                'driver_volume_type': 'iser',
                'connector': {'multipath': True},
                'data': {
                    'target_iqn': self.iqn,
                    'target_lun': self.lun,
                    'target_portal': self.host_ip,
                    'auth_method': self.auth_method,
                    'auth_username': self.user,
                    'auth_password': self.password,
                    'discovery_auth_method': self.auth_method,
                    'discovery_auth_username': self.user,
                    'discovery_auth_password': self.password,
                    'target_iqns': [self.iqn],
                    'target_luns': [self.lun],
                    'target_portals': [self.host_ip],
                    'volume_id': 'b_volume_id',
                },
            }
            mock_inst = mock.MagicMock()
            mock_pvm_uuid.return_value = '1234'
            mock_initiator.return_value = 'initiator iqn'
            # The getter can just return the VIOS values (to remove a read
            # that would otherwise need to be mocked).
            mock_vios.getter.return_value = self.feed
            single_path = iscsi.IscsiVolumeAdapter(self.adpt, 'host_uuid',
                                                   mock_inst, con_info)
            multi_path = iscsi.IscsiVolumeAdapter(self.adpt, 'host_uuid',
                                                  mock_inst, multi_con_info)
            return single_path, multi_path
        self.vol_drv, self.multi_vol_drv = init_vol_adpt()

        # setup system_metadata tests
        self.devname = "/dev/fake"
        self.slot_mgr = mock.Mock()
        self.slot_mgr.build_map.get_vscsi_slot.return_value = 62, 'the_lua'

    @mock.patch('nova_powervm.virt.powervm.volume.vscsi.PVVscsiFCVolumeAdapter'
                '._validate_vios_on_connection', new=mock.Mock())
    @mock.patch('pypowervm.tasks.hdisk.discover_iscsi', autospec=True)
    @mock.patch('pypowervm.tasks.scsi_mapper.add_map', autospec=True)
    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping',
                autospec=True)
    @mock.patch('pypowervm.tasks.hdisk.lua_recovery', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_connect_volume(self, mock_get_vm_id, mock_lua_recovery,
                            mock_build_map, mock_add_map, mock_discover):
        # The mock return values
        mock_lua_recovery.return_value = (
            hdisk.LUAStatus.DEVICE_AVAILABLE, 'devname', 'udid')
        mock_get_vm_id.return_value = '2'
        mock_discover.return_value = '/dev/fake', 'fake_udid'

        def build_map_func(host_uuid, vios_w, lpar_uuid, pv,
                           lpar_slot_num=None, lua=None, target_name=None):
            self.assertEqual('host_uuid', host_uuid)
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertEqual('1234', lpar_uuid)
            self.assertIsInstance(pv, pvm_stor.PV)
            self.assertEqual('_volume_id', pv.tag[1:])
            self.assertEqual(62, lpar_slot_num)
            self.assertEqual('the_lua', lua)
            return 'fake_map'

        mock_build_map.side_effect = build_map_func
        # Run the method
        self.vol_drv.connect_volume(self.slot_mgr)

        # As initialized above, remove_maps returns True to trigger update.
        self.assertEqual(2, mock_add_map.call_count)
        self.assertEqual(2, self.ft_fx.patchers['update'].mock.call_count)
        self.assertEqual(2, mock_build_map.call_count)

        calls = [mock.call(self.adpt, self.host_ip, self.user, self.password,
                           self.iqn, self.feed[0].uuid, lunid=self.lun,
                           multipath=False, iface_name='default',
                           discovery_auth=None, discovery_username=None,
                           auth='CHAP', discovery_password=None),
                 mock.call(self.adpt, self.host_ip, self.user, self.password,
                           self.iqn, self.feed[1].uuid, lunid=self.lun,
                           multipath=False, iface_name='default',
                           discovery_auth=None, discovery_username=None,
                           auth='CHAP', discovery_password=None)]
        multi_calls = [
            mock.call(self.adpt, [self.host_ip], self.user, self.password,
                      [self.iqn], self.feed[0].uuid, lunid=[self.lun],
                      iface_name='iser', multipath=True,
                      auth=self.auth_method, discovery_auth=self.auth_method,
                      discovery_username=self.user,
                      discovery_password=self.password),
            mock.call(self.adpt, [self.host_ip], self.user, self.password,
                      [self.iqn], self.feed[1].uuid, lunid=[self.lun],
                      iface_name='iser', multipath=True,
                      auth=self.auth_method, discovery_auth=self.auth_method,
                      discovery_username=self.user,
                      discovery_password=self.password)]
        mock_discover.assert_has_calls(calls, any_order=True)
        self.multi_vol_drv.connect_volume(self.slot_mgr)
        mock_discover.assert_has_calls(multi_calls, any_order=True)

        # connect without using CHAP authentication.
        self.vol_drv.connection_info['data'].pop('auth_method')
        mock_discover.return_value = '/dev/fake', 'fake_udid2'
        mock_add_map.reset_mock()
        mock_discover.reset_mock()
        self.vol_drv.connect_volume(self.slot_mgr)
        calls = [mock.call(self.adpt, self.host_ip, None, None,
                           self.iqn, self.feed[0].uuid, lunid=self.lun,
                           multipath=False, iface_name='default',
                           discovery_auth=None, discovery_username=None,
                           auth=None, discovery_password=None),
                 mock.call(self.adpt, self.host_ip, None, None,
                           self.iqn, self.feed[1].uuid, lunid=self.lun,
                           multipath=False, iface_name='default',
                           discovery_auth=None, discovery_username=None,
                           auth=None, discovery_password=None)]
        mock_discover.assert_has_calls(calls, any_order=True)

    @mock.patch('nova_powervm.virt.powervm.volume.volume.VscsiVolumeAdapter'
                '._validate_vios_on_connection')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_map', autospec=True)
    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping',
                autospec=True)
    @mock.patch('nova_powervm.virt.powervm.volume.driver.PowerVMVolumeAdapter.'
                'vios_uuids', new_callable=mock.PropertyMock)
    @mock.patch('pypowervm.tasks.hdisk.discover_iscsi', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_connect_volume_active_vios(self, mock_get_vm_id, mock_discover,
                                        mock_vios_uuids, mock_build_map,
                                        mock_add_map, mock_validate_vios):
        # Mockups
        mock_build_map.return_value = 'fake_map'
        mock_get_vm_id.return_value = '2'
        mock_add_map.return_value = None
        mock_get_vm_id.return_value = 'partition_id'
        mock_discover.return_value = '/dev/fake', 'fake_udid'
        vios_ids = ['1300C76F-9814-4A4D-B1F0-5B69352A7DEA',
                    '7DBBE705-E4C4-4458-8223-3EBE07015CA9']
        mock_vios_uuids.return_value = vios_ids

        self.multi_vol_drv.connect_volume(self.slot_mgr)
        self.assertEqual(2, mock_discover.call_count)

        # If the vios entries exists in the list
        mock_discover.reset_mock()
        mock_discover.return_value = '/dev/fake2', 'fake_udid2'
        mock_vios_uuids.return_value = [vios_ids[0]]
        self.multi_vol_drv.connect_volume(self.slot_mgr)
        # Check if discover iscsi is called
        self.assertEqual(1, mock_discover.call_count)

    @mock.patch('pypowervm.tasks.hdisk.discover_iscsi', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_connect_volume_discover_fail(self, mock_get_vm_id, mock_discover):
        mock_get_vm_id.return_value = '2'
        mock_discover.side_effect = pvm_exc.ISCSIDiscoveryFailed(
            vios_uuid='fake_vios', status='fake_status')

        # Run the method
        self.assertRaises(p_exc.VolumeAttachFailed,
                          self.vol_drv.connect_volume, self.slot_mgr)

    @mock.patch('pypowervm.tasks.hdisk.discover_iscsi', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_connect_volume_job_fail(self, mock_get_vm_id, mock_discover):
        mock_get_vm_id.return_value = '2'
        mock_discover.side_effect = pvm_exc.JobRequestFailed(
            operation_name='ISCSIDiscovery', error='fake_err')

        # Run the method
        self.assertRaises(p_exc.VolumeAttachFailed,
                          self.multi_vol_drv.connect_volume, self.slot_mgr)

    @mock.patch('pypowervm.tasks.partition.get_active_vioses', autospec=True)
    @mock.patch('pypowervm.tasks.storage.rescan_vstor', autospec=True)
    def test_extend_volume(self, mock_rescan, mock_active_vioses):
        self.vol_drv._set_udid("vstor_uuid")
        mock_vios = mock.Mock(uuid='fake_uuid')
        # Test single vios
        mock_active_vioses.return_value = [mock_vios]
        self.vol_drv.extend_volume()
        mock_rescan.assert_called_once_with(self.vol_drv.vios_uuids[0],
                                            "vstor_uuid", self.adpt)
        mock_rescan.side_effect = pvm_exc.JobRequestFailed(
            operation_name='RescanVirtualDisk', error='fake_err')
        self.assertRaises(p_exc.VolumeExtendFailed, self.vol_drv.extend_volume)
        mock_rescan.side_effect = pvm_exc.VstorNotFound(
            stor_udid='stor_udid', vios_uuid='uuid')
        self.assertRaises(p_exc.VolumeExtendFailed, self.vol_drv.extend_volume)

        # Test multiple vios
        mock_active_vioses.return_value = [mock_vios, mock_vios]
        mock_rescan.reset_mock()
        mock_rescan.side_effect = [pvm_exc.JobRequestFailed(
            operation_name='RescanVirtualDisk', error='fake_err'), None]
        self.assertRaises(p_exc.VolumeExtendFailed, self.vol_drv.extend_volume)
        self.assertEqual(2, mock_rescan.call_count)
        mock_rescan.reset_mock()
        mock_rescan.side_effect = [None, pvm_exc.VstorNotFound(
            stor_udid='stor_udid', vios_uuid='uuid')]
        self.vol_drv.extend_volume()
        self.assertEqual(2, mock_rescan.call_count)

        self.vol_drv._set_udid(None)
        self.assertRaises(nova_exc.InvalidBDM, self.vol_drv.extend_volume)

    @mock.patch('nova_powervm.virt.powervm.volume.driver.PowerVMVolumeAdapter.'
                'vios_uuids', new_callable=mock.PropertyMock)
    @mock.patch('pypowervm.tasks.hdisk.remove_iscsi', autospec=True)
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.hdisk_from_uuid',
                autospec=True)
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_disconnect_on_active_vioses(self, mock_get_vm_id,
                                         mock_remove_maps,
                                         mock_hdisk_from_uuid,
                                         mock_remove_iscsi,
                                         mock_vios_uuids):
        # The mock return values
        mock_hdisk_from_uuid.return_value = 'device_name'
        mock_get_vm_id.return_value = '2'
        self.multi_vol_drv._set_udid('vstor_uuid')
        mock_remove_maps.return_value = 'removed'
        vios_ids = ['1300C76F-9814-4A4D-B1F0-5B69352A7DEA',
                    '7DBBE705-E4C4-4458-8223-3EBE07015CA9']
        mock_vios_uuids.return_value = vios_ids

        # Run the method
        self.multi_vol_drv.disconnect_volume(self.slot_mgr)
        self.assertEqual(2, mock_remove_iscsi.call_count)
        self.assertEqual(2, mock_remove_maps.call_count)

    @mock.patch('nova_powervm.virt.powervm.volume.driver.PowerVMVolumeAdapter.'
                'vios_uuids', new_callable=mock.PropertyMock)
    @mock.patch('pypowervm.tasks.hdisk.remove_iscsi', autospec=True)
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.hdisk_from_uuid',
                autospec=True)
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_disconnect_on_single_vios(self, mock_get_vm_id,
                                       mock_remove_maps,
                                       mock_hdisk_from_uuid,
                                       mock_remove_iscsi,
                                       mock_vios_uuids):
        # The mock return values
        mock_hdisk_from_uuid.return_value = 'device_name'
        mock_get_vm_id.return_value = '2'
        self.multi_vol_drv._set_udid('vstor_uuid')
        mock_remove_maps.return_value = 'removed'
        mock_vios_uuids.return_value = ['1300C76F-9814-4A4D-B1F0-5B69352A7DEA']

        # Run the method
        self.multi_vol_drv.disconnect_volume(self.slot_mgr)
        self.assertEqual(1, mock_remove_iscsi.call_count)
        self.assertEqual(1, mock_remove_maps.call_count)

    @mock.patch('pypowervm.tasks.hdisk.remove_iscsi', autospec=True)
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.hdisk_from_uuid',
                autospec=True)
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_disconnect_volume(self, mock_get_vm_id, mock_remove_maps,
                               mock_hdisk_from_uuid, mock_remove_iscsi):
        # The mock return values
        mock_hdisk_from_uuid.return_value = 'device_name'
        mock_get_vm_id.return_value = '2'
        self.vol_drv._set_udid('vstor_uuid')

        def validate_remove_maps(vios_w, vm_uuid, match_func):
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertEqual('2', vm_uuid)
            return 'removed'
        mock_remove_maps.side_effect = validate_remove_maps

        # Run the method
        self.vol_drv.disconnect_volume(self.slot_mgr)

        # As initialized above, remove_maps returns True to trigger update.
        self.assertEqual(2, mock_remove_maps.call_count)
        self.assertEqual(2, self.ft_fx.patchers['update'].mock.call_count)
        calls = [mock.call(self.adpt, self.iqn, self.feed[0].uuid,
                           lun=self.lun, iface_name='default',
                           portal=self.host_ip, multipath=False),
                 mock.call(self.adpt, self.iqn, self.feed[1].uuid,
                           lun=self.lun, iface_name='default',
                           portal=self.host_ip, multipath=False)]
        multi_calls = [mock.call(self.adpt, [self.iqn], self.feed[0].uuid,
                                 lun=[self.lun], iface_name='iser',
                                 portal=[self.host_ip], multipath=True),
                       mock.call(self.adpt, [self.iqn], self.feed[1].uuid,
                                 lun=[self.lun], iface_name='iser',
                                 portal=[self.host_ip], multipath=True)]
        mock_remove_iscsi.assert_has_calls(calls, any_order=True)
        mock_remove_iscsi.reset_mock()
        self.multi_vol_drv._set_udid('vstor_uuid')
        self.multi_vol_drv.disconnect_volume(self.slot_mgr)
        mock_remove_iscsi.assert_has_calls(multi_calls, any_order=True)

    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.hdisk_from_uuid',
                autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id', autospec=True)
    @mock.patch('pypowervm.tasks.hdisk.remove_iscsi', autospec=True)
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps', autospec=True)
    def test_disconnect_volume_no_devname(
            self, mock_remove_maps, mock_remove_iscsi, mock_get_vm_id,
            mock_hdisk_from_uuid):

        # Ensures that if device_name not found, then mappings are not
        # removed and disconnect return False.
        self.vol_drv._set_udid("vstor_uuid")
        mock_hdisk_from_uuid.return_value = None
        mock_get_vm_id.return_value = '2'

        # Run the method
        status = self.vol_drv.disconnect_volume(self.slot_mgr)

        # In this case no disconnect should happen
        # mock_remove_maps.assert_not_called()
        self.assertEqual(0, mock_remove_maps.call_count)
        self.assertEqual(0, mock_remove_iscsi.call_count)
        mock_hdisk_from_uuid.assert_called_with(mock.ANY, 'vstor_uuid')
        self.assertFalse(status)

        # Ensures that if UDID not found, then mappings are not
        # removed and disconnect return False.
        self.vol_drv._set_udid(None)
        mock_hdisk_from_uuid.reset_mock()

        # Run the method
        status = self.vol_drv.disconnect_volume(self.slot_mgr)

        # In this case no disconnect should happen
        self.assertEqual(0, mock_remove_maps.call_count)
        self.assertEqual(0, mock_remove_iscsi.call_count)
        self.assertEqual(0, mock_hdisk_from_uuid.call_count)
        self.assertFalse(status)

    def test_min_xags(self):
        xags = self.vol_drv.min_xags()
        self.assertEqual(1, len(xags))
        self.assertIn(pvm_const.XAG.VIO_SMAP, xags)

    def test_vol_type(self):
        self.assertEqual('iscsi', self.vol_drv.vol_type())

    @mock.patch('pypowervm.tasks.partition.get_active_vioses')
    @mock.patch('pypowervm.tasks.hdisk.discover_iscsi_initiator')
    def test_get_iscsi_initiators(self, mock_iscsi_init, mock_active_vioses):
        # Set up mocks and clear out data that may have been set by other
        # tests
        iscsi._ISCSI_INITIATORS = dict()
        mock_iscsi_init.return_value = 'test_initiator'

        vios_ids = ['1300C76F-9814-4A4D-B1F0-5B69352A7DEA',
                    '7DBBE705-E4C4-4458-8223-3EBE07015CA9']
        vios0 = mock.Mock(uuid=vios_ids[0])
        vios1 = mock.Mock(uuid=vios_ids[1])
        mock_active_vioses.return_value = [vios0, vios1]

        expected_output = {
            '1300C76F-9814-4A4D-B1F0-5B69352A7DEA': 'test_initiator',
            '7DBBE705-E4C4-4458-8223-3EBE07015CA9': 'test_initiator'
        }

        self.assertEqual(expected_output,
                         iscsi.get_iscsi_initiators(self.adpt, vios_ids))

        # Make sure it gets set properly in the backend
        self.assertEqual(expected_output, iscsi._ISCSI_INITIATORS)
        self.assertEqual(mock_active_vioses.call_count, 0)
        self.assertEqual(mock_iscsi_init.call_count, 2)

        # Invoke again, make sure it doesn't call down to the mgmt part again
        mock_iscsi_init.reset_mock()
        self.assertEqual(expected_output,
                         iscsi.get_iscsi_initiators(self.adpt, vios_ids))
        self.assertEqual(mock_active_vioses.call_count, 0)
        self.assertEqual(mock_iscsi_init.call_count, 0)

        # Invoke iscsi.get_iscsi_initiators with vios_id=None
        iscsi._ISCSI_INITIATORS = dict()
        mock_iscsi_init.reset_mock()
        self.assertEqual(expected_output,
                         iscsi.get_iscsi_initiators(self.adpt, None))
        self.assertEqual(expected_output, iscsi._ISCSI_INITIATORS)
        self.assertEqual(mock_active_vioses.call_count, 1)
        self.assertEqual(mock_iscsi_init.call_count, 2)

        # Invoke again with vios_id=None to ensure get_active_vioses,
        # discover_iscsi_initiator is not called
        mock_iscsi_init.reset_mock()
        mock_active_vioses.reset_mock()
        self.assertEqual(expected_output,
                         iscsi.get_iscsi_initiators(self.adpt, None))
        self.assertEqual(mock_active_vioses.call_count, 0)
        self.assertEqual(mock_iscsi_init.call_count, 0)

        # Invoke iscsi.get_iscsi_initiators with discover_iscsi_initiator()
        # raises ISCSIDiscoveryFailed exception
        iscsi._ISCSI_INITIATORS = dict()
        mock_iscsi_init.reset_mock()
        mock_iscsi_init.side_effect = pvm_exc.ISCSIDiscoveryFailed(
            vios_uuid='fake_vios_uid', status="fake_status")
        self.assertEqual(dict(),
                         iscsi.get_iscsi_initiators(self.adpt, vios_ids))
        self.assertEqual(dict(), iscsi._ISCSI_INITIATORS)

        # Invoke iscsi.get_iscsi_initiators with discover_iscsi_initiator()
        # raises JobRequestFailed exception
        iscsi._ISCSI_INITIATORS = dict()
        mock_iscsi_init.reset_mock()
        mock_iscsi_init.side_effect = pvm_exc.JobRequestFailed(
            operation_name='fake_operation_name', error="fake_error")
        self.assertEqual(dict(),
                         iscsi.get_iscsi_initiators(self.adpt, vios_ids))
        self.assertEqual(dict(), iscsi._ISCSI_INITIATORS)

    def test_get_iscsi_conn_props(self):
        # Get the conn props with auth enabled
        vios_w = mock.MagicMock()
        props = self.vol_drv._get_iscsi_conn_props(vios_w, auth=True)
        expected_props = {
            'target_iqn': self.iqn,
            'target_lun': self.lun,
            'target_portal': self.host_ip,
            'auth_username': self.user,
            'auth_password': self.password,
            'auth_method': 'CHAP'
        }
        self.assertItemsEqual(expected_props, props)

        # Check with multipath enabled
        mprops = self.multi_vol_drv._get_iscsi_conn_props(vios_w, auth=True)
        multi_props = {
            'discovery_auth_method': self.auth_method,
            'discovery_auth_username': self.user,
            'discovery_auth_password': self.password,
            'target_iqns': [self.iqn],
            'target_luns': [self.lun],
            'target_portals': [self.host_ip]
        }
        multi_props.update(expected_props)
        self.assertItemsEqual(multi_props, mprops)

        # Call without auth props
        props = self.vol_drv._get_iscsi_conn_props(vios_w, auth=False)
        expected_props.pop('auth_username')
        expected_props.pop('auth_password')
        expected_props.pop('auth_method')
        self.assertItemsEqual(expected_props, props)

        # KeyError
        self.vol_drv.connection_info['data'].pop('target_iqn')
        props = self.vol_drv._get_iscsi_conn_props(vios_w, auth=False)
        self.assertIsNone(props)

    @mock.patch('nova_powervm.virt.powervm.volume.driver.PowerVMVolumeAdapter.'
                'vios_uuids', new_callable=mock.PropertyMock)
    @mock.patch('pypowervm.tasks.hdisk.discover_iscsi')
    @mock.patch('pypowervm.tasks.storage.find_stale_lpars')
    def test_pre_live_migration(self, mock_fsl, mock_discover,
                                mock_vios_uuids):
        # The mock return values
        vios_ids = ['1300C76F-9814-4A4D-B1F0-5B69352A7DEA',
                    '7DBBE705-E4C4-4458-8223-3EBE07015CA9']
        mock_vios_uuids.return_value = vios_ids

        mock_fsl.return_value = []
        mock_discover.return_value = (
            'devname', 'udid')

        # Run the method
        migrate_data = {}
        self.vol_drv.pre_live_migration_on_destination(migrate_data)
        volume_key = 'vscsi-' + self.serial
        self.assertEqual(migrate_data, {volume_key: 'udid'})

        # Test exception path
        mock_discover.return_value = (
            'devname', None)

        # Run the method
        self.assertRaises(p_exc.VolumePreMigrationFailed,
                          self.vol_drv.pre_live_migration_on_destination, {})

        # Test when volume discover on a single vios
        mock_discover.reset_mock()
        mock_discover.side_effect = [('devname', 'udid'), ('devname', None)]
        self.vol_drv.pre_live_migration_on_destination(migrate_data)
        self.assertEqual(migrate_data, {volume_key: 'udid'})
        self.assertEqual(2, mock_discover.call_count)

        # Test with bad vios_uuid
        mock_discover.reset_mock()
        mock_vios_uuids.return_value = ['fake_vios']
        self.assertRaises(p_exc.VolumePreMigrationFailed,
                          self.vol_drv.pre_live_migration_on_destination, {})
        mock_discover.assert_not_called()

    @mock.patch('nova_powervm.virt.powervm.volume.volume.VscsiVolumeAdapter'
                '._set_udid', autospec=True)
    def test_post_live_migration_at_destination(self, mock_set_udid):
        volume_key = 'vscsi-' + self.serial
        mig_vol_stor = {volume_key: 'udid'}
        self.vol_drv.post_live_migration_at_destination(mig_vol_stor)
        mock_set_udid.assert_called_with(mock.ANY, 'udid')

    def test_post_live_migr_source(self):

        # Bad path.  volume id not found
        bad_data = {'vscsi-BAD': 'udid1'}
        # good path.
        good_data = {'vscsi-' + self.serial: 'udid1'}

        with mock.patch.object(self.vol_drv, '_cleanup_volume') as mock_cln:
            self.vol_drv.post_live_migration_at_source(bad_data)
            mock_cln.assert_called_once_with(None)

            mock_cln.reset_mock()
            self.vol_drv.post_live_migration_at_source(good_data)
            mock_cln.assert_called_once_with('udid1')

    @mock.patch('nova_powervm.virt.powervm.volume.driver.PowerVMVolumeAdapter.'
                'vios_uuids', new_callable=mock.PropertyMock)
    def test_is_volume_on_vios(self, mock_vios_uuids):
        # The mock return values
        mock_vios_uuids.return_value = ['fake_vios1', 'fake_vios2']

        with mock.patch.object(self.vol_drv,
                               '_discover_volume_on_vios') as mock_discover:
            found, udid = self.vol_drv.is_volume_on_vios(self.feed[0])
            mock_discover.assert_not_called()
            self.assertFalse(found)
            self.assertIsNone(udid)

            mock_discover.reset_mock()
            mock_discover.return_value = 'device1', 'udid1'
            vios_ids = ['1300C76F-9814-4A4D-B1F0-5B69352A7DEA',
                        '7DBBE705-E4C4-4458-8223-3EBE07015CA9']
            mock_vios_uuids.return_value = vios_ids

            found, udid = self.vol_drv.is_volume_on_vios(self.feed[0])
            mock_discover.assert_called_once_with(self.feed[0])
            self.assertTrue(found)
            self.assertEqual(udid, 'udid1')

            mock_discover.reset_mock()
            mock_discover.return_value = None, 'udid1'
            found, udid = self.vol_drv.is_volume_on_vios(self.feed[0])
            self.assertFalse(found)
            self.assertEqual(udid, 'udid1')

            mock_discover.reset_mock()
            mock_discover.return_value = 'device1', None
            found, udid = self.vol_drv.is_volume_on_vios(self.feed[0])
            self.assertFalse(found)
            self.assertIsNone(udid)
