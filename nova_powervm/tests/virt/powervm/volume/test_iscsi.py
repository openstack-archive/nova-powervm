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
                    'auth_password': self.password
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
                    'target_portals': [self.host_ip]
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
            self.assertEqual(62, lpar_slot_num)
            self.assertEqual('the_lua', lua)
            self.assertEqual('ISCSI-bar_%s' % self.lun, target_name)
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
                           auth=None, discovery_password=None),
                 mock.call(self.adpt, self.host_ip, self.user, self.password,
                           self.iqn, self.feed[1].uuid, lunid=self.lun,
                           multipath=False, iface_name='default',
                           discovery_auth=None, discovery_username=None,
                           auth=None, discovery_password=None)]
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

    @mock.patch('pypowervm.tasks.hdisk.discover_iscsi', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_connect_volume_discover_fail(self, mock_get_vm_id, mock_discover):
        mock_get_vm_id.return_value = '2'
        mock_discover.side_effect = pvm_exc.ISCSIDiscoveryFailed(
            vios_uuid='fake_vios', status='fake_status')

        # Run the method
        self.assertRaises(pvm_exc.MultipleExceptionsInFeedTask,
                          self.vol_drv.connect_volume, self.slot_mgr)

    @mock.patch('pypowervm.tasks.hdisk.discover_iscsi', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_connect_volume_job_fail(self, mock_get_vm_id, mock_discover):
        mock_get_vm_id.return_value = '2'
        mock_discover.side_effect = pvm_exc.JobRequestFailed(
            operation_name='ISCSIDiscovery', error='fake_err')

        # Run the method
        self.assertRaises(pvm_exc.MultipleExceptionsInFeedTask,
                          self.vol_drv.connect_volume, self.slot_mgr)

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
        self.vol_drv._set_devname('/dev/fake')
        self.multi_vol_drv._set_devname('/dev/fake')

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
        self.multi_vol_drv.disconnect_volume(self.slot_mgr)
        mock_remove_iscsi.assert_has_calls(multi_calls, any_order=True)

    def test_min_xags(self):
        xags = self.vol_drv.min_xags()
        self.assertEqual(1, len(xags))
        self.assertIn(pvm_const.XAG.VIO_SMAP, xags)

    def test_vol_type(self):
        self.assertEqual('iscsi', self.vol_drv.vol_type())

    def test_set_devname(self):

        # Mock connection info
        self.vol_drv.connection_info['data'][iscsi.DEVNAME_KEY] = None

        # Set the Device Name
        self.vol_drv._set_devname(self.devname)

        # Verify
        dev_name = self.vol_drv.connection_info['data'][iscsi.DEVNAME_KEY]
        self.assertEqual(self.devname, dev_name)

    def test_get_devname(self):

        # Set the value to retrieve
        self.vol_drv.connection_info['data'][iscsi.DEVNAME_KEY] = self.devname
        retrieved_devname = self.vol_drv._get_devname()
        # Check key found
        self.assertEqual(self.devname, retrieved_devname)

        # Check key not found
        self.vol_drv.connection_info['data'].pop(iscsi.DEVNAME_KEY)
        retrieved_devname = self.vol_drv._get_devname()
        # Check key not found
        self.assertIsNone(retrieved_devname)
