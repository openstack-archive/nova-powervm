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

import pypowervm.exceptions as pexc
from pypowervm.tasks import hdisk
from pypowervm.tests.wrappers.util import pvmhttp

CONF = cfg.CONF

VIOS_FEED = 'fake_vios_feed.txt'

I_WWPN_1 = '21000024FF649104'
I_WWPN_2 = '21000024FF649105'


class TestVSCSIAdapter(test.TestCase):
    """Tests the vSCSI Volume Connector Adapter."""
    def setUp(self):
        super(TestVSCSIAdapter, self).setUp()
        self.pypvm_fix = self.useFixture(fx.PyPowerVM())
        self.adpt = self.pypvm_fix.apt

        @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
        def init_vol_adpt(mock_pvm_uuid):
            con_info = {'data': {'initiator_target_map': {I_WWPN_1: ['t1'],
                                                          I_WWPN_2: ['t2',
                                                                     't3']},
                        'target_lun': '1', 'volume_id': 'id'}}
            mock_inst = mock.MagicMock()
            mock_pvm_uuid.return_value = '1234'
            return vscsi.VscsiVolumeAdapter(self.adpt, 'host_uuid',
                                            mock_inst, con_info)
        self.vol_drv = init_vol_adpt()
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
        mock_discover_hdisk.return_value = (
            hdisk.LUAStatus.DEVICE_AVAILABLE, 'devname', 'udid')

        self.adpt.read.return_value = self.vios_feed_resp
        mock_build_itls.return_value = [mock.MagicMock()]

        self.vol_drv.connect_volume()

        # Single mapping
        self.assertEqual(1, mock_add_vscsi_mapping.call_count)
        mock_add_vscsi_mapping.assert_called_with(
            'host_uuid', '3443DB77-AED1-47ED-9AA5-3DB9C6CF7089', '1234',
            mock.ANY)
        self.assertListEqual(['3443DB77-AED1-47ED-9AA5-3DB9C6CF7089'],
                             self.vol_drv._vioses_modified)

    @mock.patch('pypowervm.tasks.hdisk.build_itls')
    @mock.patch('pypowervm.tasks.hdisk.discover_hdisk')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_vscsi_mapping')
    def test_connect_volume_to_initiatiors(self, mock_add_vscsi_mapping,
                                           mock_discover_hdisk,
                                           mock_build_itls):
        """Tests that the connect w/out initiators throws errors."""
        mock_discover_hdisk.return_value = (
            hdisk.LUAStatus.DEVICE_AVAILABLE, 'devname', 'udid')

        self.adpt.read.return_value = self.vios_feed_resp
        mock_instance = mock.Mock()
        mock_instance.system_metadata = {}

        mock_build_itls.return_value = []
        self.assertRaises(pexc.VolumeAttachFailed,
                          self.vol_drv.connect_volume)

    @mock.patch('pypowervm.tasks.hdisk.remove_hdisk')
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_pv_mapping')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.hdisk_from_uuid')
    def test_disconnect_volume(self, mock_hdisk_from_uuid,
                               mock_get_vm_id, mock_remove_pv_mapping,
                               mock_remove_hdisk):

        # Set test scenario
        self.adpt.read.return_value = self.vios_feed_resp
        mock_hdisk_from_uuid.return_value = 'device_name'
        mock_get_vm_id.return_value = 'partion_id'

        # Run the test
        self.vol_drv.disconnect_volume()

        self.assertEqual(1, mock_remove_pv_mapping.call_count)
        self.assertEqual(1, mock_remove_hdisk.call_count)

    @mock.patch('nova_powervm.virt.powervm.vios.get_physical_wwpns')
    def test_wwpns(self, mock_vio_wwpns):
        mock_vio_wwpns.return_value = ['aa', 'bb']

        wwpns = self.vol_drv.wwpns()

        self.assertListEqual(['aa', 'bb'], wwpns)

    def test_set_udid(self):

        # Mock connection info
        udid_key = vscsi._build_udid_key(self.vios_uuid, self.volume_id)
        self.vol_drv.connection_info['data'][udid_key] = self.udid

        # Set the UDID
        self.vol_drv._set_udid(self.vios_uuid, self.volume_id,
                               self.udid)

        # Verify
        self.assertEqual(self.udid,
                         self.vol_drv.connection_info['data'][udid_key])

    def test_get_udid(self):

        udid_key = vscsi._build_udid_key(self.vios_uuid, self.volume_id)
        self.vol_drv.connection_info['data'][udid_key] = self.udid

        # Set the key to retrieve
        retrieved_udid = self.vol_drv._get_udid(self.vios_uuid, self.volume_id)

        # Check key found
        self.assertEqual(self.udid, retrieved_udid)

        # Check key not found
        retrieved_udid = (self.vol_drv._get_udid(self.vios_uuid,
                                                 'non_existent_key'))

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
