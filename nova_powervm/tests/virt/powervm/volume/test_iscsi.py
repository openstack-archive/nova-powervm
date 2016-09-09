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
from nova_powervm.virt.powervm.volume import iscsi

from pypowervm import const as pvm_const
from pypowervm.tasks import hdisk
from pypowervm.tests.tasks.util import load_file
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.wrappers import storage as pvm_stor
from pypowervm.wrappers import virtual_io_server as pvm_vios

CONF = cfg.CONF

VIOS_FEED = 'fake_vios_feed2.txt'


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

        @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.getter')
        @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
        @mock.patch('pypowervm.tasks.partition.get_mgmt_partition')
        @mock.patch('pypowervm.tasks.hdisk.discover_iscsi_initiator')
        def init_vol_adpt(mock_initiator, mock_mgmt_part, mock_pvm_uuid,
                          mock_getter):

            con_info = {
                'data': {
                    'target_iqn': 'iqn.2016-08.bar.foo:target',
                    'target_lun': '1',
                    'target_portal': '10.0.0.1',
                    'auth_username': 'user',
                    'auth_password': 'password',
                    'volume_id': 'f042c68a-c5a5-476a-ba34-2f6d43f4226c'
                },
            }
            mock_inst = mock.MagicMock()
            mock_pvm_uuid.return_value = '1234'
            mock_initiator.return_value = 'initiatior iqn'
            # The getter can just return the VIOS values (to remove a read
            # that would otherwise need to be mocked).
            mock_getter.return_value = self.feed

            return iscsi.IscsiVolumeAdapter(self.adpt, 'host_uuid', mock_inst,
                                            con_info)
        self.vol_drv = init_vol_adpt()

        # setup system_metadata tests
        self.devname = "/dev/fake"
        self.slot_mgr = mock.Mock()
        self.slot_mgr.build_map.get_vscsi_slot.return_value = 62, 'the_lua'

    @mock.patch('pypowervm.tasks.hdisk.discover_iscsi')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_map')
    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('pypowervm.tasks.hdisk.lua_recovery')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_connect_volume(self, mock_get_vm_id, mock_lua_recovery,
                            mock_build_map, mock_add_map, mock_discover):
        # The mock return values
        mock_lua_recovery.return_value = (
            hdisk.LUAStatus.DEVICE_AVAILABLE, 'devname', 'udid')
        mock_get_vm_id.return_value = '2'
        mock_discover.return_value = '/dev/fake'

        def build_map_func(host_uuid, vios_w, lpar_uuid, pv,
                           lpar_slot_num=None, lua=None, target_name=None):
            self.assertEqual('host_uuid', host_uuid)
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertEqual('1234', lpar_uuid)
            self.assertIsInstance(pv, pvm_stor.PV)
            self.assertEqual(62, lpar_slot_num)
            self.assertEqual('the_lua', lua)
            self.assertEqual('ISCSI-target', target_name)
            return 'fake_map'

        mock_build_map.side_effect = build_map_func

        # Run the method
        self.vol_drv.connect_volume(self.slot_mgr)

        # As initialized above, remove_maps returns True to trigger update.
        self.assertEqual(1, mock_add_map.call_count)
        self.assertEqual(1, self.ft_fx.patchers['update'].mock.call_count)
        self.assertEqual(1, mock_build_map.call_count)

    @mock.patch('pypowervm.tasks.hdisk.remove_iscsi')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.hdisk_from_uuid')
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_disconnect_volume(self, mock_get_vm_id, mock_remove_maps,
                               mock_hdisk_from_uuid, mock_remove_iscsi):
        # The mock return values
        mock_hdisk_from_uuid.return_value = 'device_name'
        mock_get_vm_id.return_value = 'partition_id'
        self.vol_drv._set_devname('/dev/fake')

        def validate_remove_maps(vios_w, vm_uuid, match_func):
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertEqual('partition_id', vm_uuid)
            return 'removed'
        mock_remove_maps.side_effect = validate_remove_maps

        # Run the method
        self.vol_drv.disconnect_volume(self.slot_mgr)

        # As initialized above, remove_maps returns True to trigger update.
        self.assertEqual(1, mock_remove_maps.call_count)
        fake_iqn = self.vol_drv.connection_info['data']['target_iqn']
        mock_remove_iscsi.assert_called_once_with(
            self.adpt, fake_iqn, '3443DB77-AED1-47ED-9AA5-3DB9C6CF7089')
        self.assertEqual(1, self.ft_fx.patchers['update'].mock.call_count)

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
