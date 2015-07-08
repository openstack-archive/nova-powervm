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

import mock

from nova import test
import os

from oslo_config import cfg

from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm.volume import vscsi

from pypowervm.tasks import hdisk
from pypowervm.tests.wrappers.util import pvmhttp

CONF = cfg.CONF

VIOS_FEED = 'fake_vios_feed.txt'


class TestVSCSIAdapter(test.TestCase):
    """Tests the vSCSI Volume Connector Adapter."""

    def setUp(self):
        super(TestVSCSIAdapter, self).setUp()
        self.pypvm_fix = self.useFixture(fx.PyPowerVM())
        self.adpt = self.pypvm_fix.apt

        # Find directory for response file(s)
        data_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(data_dir, '../data')

        def resp(file_name):
            file_path = os.path.join(data_dir, file_name)
            return pvmhttp.load_pvm_resp(file_path).get_response()
        self.vios_feed_resp = resp(VIOS_FEED)

        # setup system_metadata tests
        self.volume_id = 'f042c68a-c5a5-476a-ba34-2f6d43f4226c'
        self.vios_uuid = '3443DB77-AED1-47ED-9AA5-3DB9C6CF7089'
        self.udid = (
            '01M0lCTTIxNDUxMjQ2MDA1MDc2ODAyODI4NjFEODgwMDAwMDAwMDAwMDA1Rg==')

    @mock.patch('pypowervm.tasks.hdisk.build_itls')
    @mock.patch('pypowervm.tasks.hdisk.discover_hdisk')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_vscsi_mapping')
    def test_connect_volume(self, mock_add_vscsi_mapping,
                            mock_discover_hdisk, mock_build_itls):
        con_info = {'data': {'initiator_target_map': {'i1': ['t1'],
                                                      'i2': ['t2', 't3']},
                    'target_lun': '1', 'volume_id': 'id'}}
        mock_discover_hdisk.return_value = (
            hdisk.LUAStatus.DEVICE_AVAILABLE, 'devname', 'udid')

        self.adpt.read.return_value = self.vios_feed_resp
        mock_instance = mock.Mock()
        mock_instance.system_metadata = {}

        vscsi.VscsiVolumeAdapter().connect_volume(self.adpt, 'host_uuid',
                                                  'vm_uuid', mock_instance,
                                                  con_info)
        # Single mapping
        self.assertEqual(1, mock_add_vscsi_mapping.call_count)
        mock_add_vscsi_mapping.assert_called_with(
            'host_uuid', '3443DB77-AED1-47ED-9AA5-3DB9C6CF7089', 'vm_uuid',
            mock.ANY)

    @mock.patch('pypowervm.tasks.hdisk.remove_hdisk')
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_pv_mapping')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.hdisk_from_uuid')
    def test_disconnect_volume(self, mock_hdisk_from_uuid,
                               mock_get_vm_id, mock_remove_pv_mapping,
                               mock_remove_hdisk):
        vol_drv = vscsi.VscsiVolumeAdapter()

        # Mock up the connection info
        vios_uuid = '3443DB77-AED1-47ED-9AA5-3DB9C6CF7089'
        volid_meta_key = vol_drv._build_udid_key(vios_uuid, 'id')
        con_info = {'data': {'initiator_target_map': {'i1': ['t1'],
                                                      'i2': ['t2', 't3']},
                             volid_meta_key: self.udid,
                    'target_lun': '1', 'volume_id': 'id'}}

        # Build the mock instance
        instance = mock.Mock()

        # Set test scenario
        self.adpt.read.return_value = self.vios_feed_resp
        mock_hdisk_from_uuid.return_value = 'device_name'
        mock_get_vm_id.return_value = 'partion_id'

        # Run the test
        vol_drv.disconnect_volume(self.adpt, 'host_uuid', 'vm_uuid', instance,
                                  con_info)
        self.assertEqual(1, mock_remove_pv_mapping.call_count)
        self.assertEqual(1, mock_remove_hdisk.call_count)

    @mock.patch('nova_powervm.virt.powervm.vios.get_physical_wwpns')
    def test_wwpns(self, mock_vio_wwpns):
        mock_vio_wwpns.return_value = ['aa', 'bb']

        vol_drv = vscsi.VscsiVolumeAdapter()
        wwpns = vol_drv.wwpns(mock.ANY, 'host_uuid', mock.ANY)

        self.assertListEqual(['aa', 'bb'], wwpns)

    def test_set_udid(self):
        vol_adpt = vscsi.VscsiVolumeAdapter()

        # Mock connection info
        connection_info = {'data': {}}
        udid_key = vol_adpt._build_udid_key(self.vios_uuid, self.volume_id)

        # Set the UDID
        vol_adpt._set_udid(connection_info, self.vios_uuid, self.volume_id,
                           self.udid)

        # Verify
        self.assertEqual(self.udid, connection_info['data'][udid_key])

    def test_get_udid(self):
        vol_adpt = vscsi.VscsiVolumeAdapter()

        # Mock connection info
        connection_info = {'data': {}}
        udid_key = vol_adpt._build_udid_key(self.vios_uuid, self.volume_id)
        connection_info['data'][udid_key] = self.udid

        # Set the key to retrieve
        retrieved_udid = vol_adpt._get_udid(connection_info, self.vios_uuid,
                                            self.volume_id)

        # Check key found
        self.assertEqual(self.udid, retrieved_udid)

        # Check key not found
        retrieved_udid = (vscsi.VscsiVolumeAdapter().
                          _get_udid(connection_info, self.vios_uuid,
                                    'non_existent_key'))
        # Check key not found
        self.assertIsNone(retrieved_udid)
