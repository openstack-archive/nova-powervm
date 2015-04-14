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
    @mock.patch('nova_powervm.virt.powervm.vios.get_vios_name_map')
    def test_connect_volume(self, mock_vio_name_map, mock_add_vscsi_mapping,
                            mock_discover_hdisk, mock_build_itls):
        con_info = {'data': {'initiator_target_map': {'i1': ['t1'],
                                                      'i2': ['t2', 't3']},
                    'target_lun': '1', 'volume_id': 'id'}}
        mock_discover_hdisk.return_value = (
            hdisk.LUAStatus.DEVICE_AVAILABLE, 'devname', 'udid')
        mock_vio_name_map.return_value = {'vio_name': 'vio_uuid',
                                          'vio_name1': 'vio_uuid1'}
        mock_instance = mock.Mock()
        mock_instance.system_metadata = {}

        vscsi.VscsiVolumeAdapter().connect_volume(None, 'host_uuid',
                                                  'vm_uuid', mock_instance,
                                                  con_info)
        # Confirm mapping called twice for two defined VIOS
        self.assertEqual(2, mock_add_vscsi_mapping.call_count)

    @mock.patch('pypowervm.tasks.hdisk.remove_hdisk')
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_pv_mapping')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.hdisk_from_uuid')
    def test_disconnect_volume(self, mock_hdisk_from_uuid,
                               mock_get_vm_id, mock_remove_pv_mapping,
                               mock_remove_hdisk):
        con_info = {'data': {'initiator_target_map': {'i1': ['t1'],
                                                      'i2': ['t2', 't3']},
                    'target_lun': '1', 'volume_id': 'id'}}
        # Build system_metadata key
        instance = mock.Mock()
        vios_uuid = '3443DB77-AED1-47ED-9AA5-3DB9C6CF7089'
        volid_meta_key = vscsi.VscsiVolumeAdapter()._build_udid_key(vios_uuid,
                                                                    'id')
        instance.system_metadata = {volid_meta_key: self.udid}
        # Set test scenario
        CONF.enable_remove_hdisk = False
        self.adpt.read.return_value = self.vios_feed_resp
        mock_hdisk_from_uuid.return_value = 'device_name'
        mock_get_vm_id.return_value = 'partion_id'
        # Run the test
        vscsi.VscsiVolumeAdapter().disconnect_volume(self.adpt, 'host_uuid',
                                                     'vm_uuid',
                                                     instance, con_info)
        self.assertEqual(1, mock_remove_pv_mapping.call_count)
        # Per conf setting remove_hdisk not called
        self.assertEqual(0, mock_remove_hdisk.call_count)
        # Verify entry deleted
        self.assertDictEqual({}, instance.system_metadata)

    @mock.patch('nova_powervm.virt.powervm.vios.get_physical_wwpns')
    def test_wwpns(self, mock_vio_wwpns):
        mock_vio_wwpns.return_value = ['aa', 'bb']

        vol_drv = vscsi.VscsiVolumeAdapter()
        wwpns = vol_drv.wwpns(mock.ANY, 'host_uuid', mock.ANY)

        self.assertListEqual(['aa', 'bb'], wwpns)

    def test_set_udid(self):
        # Mock Data
        instance = mock.Mock()
        instance.system_metadata = {}
        volid_meta_key = (vscsi.VscsiVolumeAdapter().
                          _build_udid_key(self.vios_uuid, self.volume_id))
        vscsi.VscsiVolumeAdapter()._set_udid(instance, self.vios_uuid,
                                             self.volume_id, self.udid)
        # Check
        self.assertEqual(self.udid,
                         instance.system_metadata[volid_meta_key])

    def test_get_udid(self):
        instance = mock.Mock()
        volid_meta_key = (vscsi.VscsiVolumeAdapter().
                          _build_udid_key(self.vios_uuid, self.volume_id))
        # set the key to retrieve
        instance.system_metadata = {volid_meta_key: self.udid}
        retrieved_udid = (vscsi.VscsiVolumeAdapter().
                          _get_udid(instance, self.vios_uuid, self.volume_id))
        # Check key found
        self.assertEqual(self.udid, retrieved_udid)

        # check key not found
        retrieved_udid = (vscsi.VscsiVolumeAdapter().
                          _get_udid(instance, self.vios_uuid,
                                    'non_existent_key'))
        # Check key not found
        self.assertIsNone(retrieved_udid)

    def test_delete_udid_key(self):
        instance = mock.Mock()
        # set the key to delete
        volid_meta_key = (vscsi.VscsiVolumeAdapter().
                          _build_udid_key(self.vios_uuid, self.volume_id))
        # set the key to retrieve
        instance.system_metadata = {volid_meta_key: self.udid}
        vscsi.VscsiVolumeAdapter()._delete_udid_key(instance, self.vios_uuid,
                                                    self.volume_id)
        # Verify empty list
        self.assertDictEqual({}, instance.system_metadata)
