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

from nova.compute import task_states
from nova import test
import os
from pypowervm.tests.wrappers.util import pvmhttp

from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm.volume import npiv

VIOS_FEED = 'fake_vios_feed.txt'


class TestNPIVAdapter(test.TestCase):
    """Tests the NPIV Volume Connector Adapter."""

    def setUp(self):
        super(TestNPIVAdapter, self).setUp()
        self.vol_drv = npiv.NPIVVolumeAdapter()

        # Fixtures
        self.adpt_fix = self.useFixture(fx.PyPowerVM())
        self.adpt = self.adpt_fix.apt

        # Find directory for response file(s)
        data_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(data_dir, '../data')

        def resp(file_name):
            file_path = os.path.join(data_dir, file_name)
            return pvmhttp.load_pvm_resp(file_path).get_response()
        self.vios_feed_resp = resp(VIOS_FEED)

    @mock.patch('pypowervm.wrappers.virtual_io_server.VStorageMapping.'
                '_client_lpar_href')
    def test_connect_volume(self, mock_href):
        # Mock Data
        con_info = {'data': {'initiator_target_map': {'a': None,
                                                      'b': None}}}
        mock_href.return_value = 'fake_uri'
        self.adpt.read.return_value = self.vios_feed_resp.feed.entries[0]

        # A validation function on the update.
        def validate_update(*kargs, **kwargs):
            vios_w = kargs[0]
            self.assertEqual(1, len(vios_w.vfc_mappings))

        self.adpt.update.side_effect = validate_update

        # Invoke
        self.vol_drv.connect_volume(self.adpt, 'host_uuid', 'vios_uuid',
                                    'vm_uuid', 'vios_name',
                                    mock.MagicMock(), con_info)

        # Verify
        self.assertEqual(1, self.adpt.update.call_count)

    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.wrap')
    def test_connect_volume_no_map(self, mock_vio_wrap):
        """Tests that if the VFC Mapping exists, another is not added."""
        # Mock Data
        con_info = {'data': {'initiator_target_map': {'a': None,
                                                      'b': None}}}

        mock_mapping = mock.MagicMock()
        mock_mapping.client_adapter.wwpns = set(['a', 'b'])

        mock_vios = mock.MagicMock()
        mock_vios.vfc_mappings = [mock_mapping]

        mock_vio_wrap.return_value = mock_vios

        # Invoke
        self.vol_drv.connect_volume(self.adpt, 'host_uuid', 'vios_uuid',
                                    'vm_uuid', 'vios_name', mock.MagicMock(),
                                    con_info)

        # Verify
        self.assertEqual(0, self.adpt.update.call_count)

    def test_disconnect_volume_no_op(self):
        """Tests that the deletion doesn't go through on certain states."""
        inst = mock.MagicMock()
        inst.task_state = task_states.RESUMING
        self.vol_drv.disconnect_volume(self.adpt, 'host_uuid', 'vios_uuid',
                                       'vm_uuid', inst, mock.ANY)
        self.assertEqual(0, self.adpt.read.call_count)

    @mock.patch('pypowervm.jobs.wwpn.build_wwpn_pair')
    def test_wwpns(self, mock_build_wwpns):
        """Tests that new WWPNs get generated properly."""
        # Mock Data
        inst = mock.MagicMock()
        inst.system_metadata = {npiv.WWPN_SYSTEM_METADATA_KEY: None}
        mock_build_wwpns.return_value = ['aa', 'bb']

        # invoke
        wwpns = self.vol_drv.wwpns(mock.ANY, 'host_uuid', inst)

        # Check
        self.assertListEqual(['aa', 'bb'], wwpns)
        self.assertEqual('aa bb',
                         inst.system_metadata[npiv.WWPN_SYSTEM_METADATA_KEY])

    def test_wwpns_on_sys_meta(self):
        """Tests that previously stored WWPNs are returned."""
        # Mock
        inst = mock.MagicMock()
        inst.system_metadata = {npiv.WWPN_SYSTEM_METADATA_KEY: 'a b'}

        # Invoke
        wwpns = self.vol_drv.wwpns(mock.ANY, 'host_uuid', inst)

        # Verify
        self.assertListEqual(['a', 'b'], wwpns)

    def test_wwpn_match(self):
        self.assertTrue(self.vol_drv._wwpn_match(['a', 'b'], ['b', 'a']))
        self.assertTrue(self.vol_drv._wwpn_match(set(['a', 'b']),
                                                 ['b', 'a']))
        self.assertTrue(self.vol_drv._wwpn_match(set(['A', 'B']),
                                                 ['b', 'a']))
        self.assertFalse(self.vol_drv._wwpn_match(set(['a', 'b']),
                                                  ['b', 'a', 'c']))
        self.assertFalse(self.vol_drv._wwpn_match(set(['a', 'b', 'c']),
                                                  ['b', 'a']))
        self.assertFalse(self.vol_drv._wwpn_match(['a', 'b', 'c'],
                                                  ['b', 'a']))
