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

from nova_powervm.virt.powervm.volume import npiv


class TestNPIVAdapter(test.TestCase):
    """Tests the NPIV Volume Connector Adapter."""

    def setUp(self):
        super(TestNPIVAdapter, self).setUp()
        self.vol_drv = npiv.NPIVVolumeAdapter()

    @mock.patch('pypowervm.wrappers.virtual_io_server.VFCMapping.bld')
    @mock.patch('nova_powervm.virt.powervm.vios.add_vfc_mapping')
    def test_connect_volume(self, mock_add_vfc, mock_vfc_map_bld):
        con_info = {'data': {'initiator_target_map': {'a': None,
                                                      'b': None}}}
        self.vol_drv.connect_volume(None, 'host_uuid', 'vios_uuid', 'vm_uuid',
                                    None, con_info, '/sda')
        self.assertEqual(1, mock_add_vfc.call_count)

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
