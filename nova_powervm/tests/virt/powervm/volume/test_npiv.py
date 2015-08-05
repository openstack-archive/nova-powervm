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
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm.volume import npiv

VIOS_FEED = 'fake_vios_feed.txt'


class TestNPIVAdapter(test.TestCase):
    """Tests the NPIV Volume Connector Adapter."""

    def setUp(self):
        super(TestNPIVAdapter, self).setUp()

        # Find directory for response file(s)
        data_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(data_dir, '../data')

        def resp(file_name):
            file_path = os.path.join(data_dir, file_name)
            return pvmhttp.load_pvm_resp(file_path).get_response()
        self.vios_feed_resp = resp(VIOS_FEED)
        self.wwpn1 = '21000024FF649104'
        self.wwpn2 = '21000024FF649107'

        # Set up the mocks for the internal volume driver
        name = 'nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
        self.mock_port_count_p = mock.patch(name + '_ports_per_fabric')
        self.mock_port_count = self.mock_port_count_p.start()
        self.mock_port_count.return_value = 1

        self.mock_fabric_names_p = mock.patch(name + '_fabric_names')
        self.mock_fabric_names = self.mock_fabric_names_p.start()
        self.mock_fabric_names.return_value = ['A']

        self.mock_fabric_ports_p = mock.patch(name + '_fabric_ports')
        self.mock_fabric_ports = self.mock_fabric_ports_p.start()
        self.mock_fabric_ports.return_value = [self.wwpn1, self.wwpn2]

        # Fixtures
        self.adpt_fix = self.useFixture(fx.PyPowerVM())
        self.adpt = self.adpt_fix.apt

        @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
        def init_vol_adpt(mock_pvm_uuid):
            con_info = {'data': {'initiator_target_map': {'i1': ['t1'],
                                                          'i2': ['t2', 't3']},
                        'target_lun': '1', 'volume_id': 'id'}}
            mock_inst = mock.MagicMock()
            mock_pvm_uuid.return_value = '1234'
            return npiv.NPIVVolumeAdapter(self.adpt, 'host_uuid',
                                          mock_inst, con_info)
        self.vol_drv = init_vol_adpt()

    def tearDown(self):
        super(TestNPIVAdapter, self).tearDown()

        self.mock_port_count_p.stop()
        self.mock_fabric_names_p.stop()
        self.mock_fabric_ports_p.stop()

    @mock.patch('pypowervm.tasks.vfc_mapper.add_npiv_port_mappings')
    @mock.patch('pypowervm.tasks.vfc_mapper.remove_npiv_port_mappings')
    def test_connect_volume(self, mock_remove_p_maps, mock_add_p_maps):
        # Invoke
        meta_key = self.vol_drv._sys_fabric_state_key('A')
        self.vol_drv.instance.system_metadata = {meta_key: npiv.FS_MGMT_MAPPED}
        # Test connect volume when the fabric is mapped with mgmt partition
        self.vol_drv.connect_volume()

        # Verify
        # Mgmt mapping should be removed
        # Add the mapping with the instance.
        self.assertEqual(1, mock_add_p_maps.call_count)
        self.assertEqual(1, mock_remove_p_maps.call_count)
        # Check the fabric state should be mapped to instance
        fc_state = self.vol_drv._get_fabric_state('A')
        self.assertEqual(npiv.FS_INST_MAPPED, fc_state)

    @mock.patch('pypowervm.tasks.vfc_mapper.add_npiv_port_mappings')
    @mock.patch('pypowervm.tasks.vfc_mapper.remove_npiv_port_mappings')
    def test_connect_volume_inst_mapped(self, mock_remove_p_maps,
                                        mock_add_p_maps):
        # Invoke
        meta_key = self.vol_drv._sys_fabric_state_key('A')
        # Test subsequent connect volume calls when the fabric
        # is mapped with inst partition
        self.vol_drv.instance.system_metadata = {meta_key: npiv.FS_INST_MAPPED}
        self.vol_drv.connect_volume()

        # Verify
        # Remove mapping should not be called
        # Add the mapping with the instance.
        self.assertEqual(1, mock_add_p_maps.call_count)
        self.assertEqual(0, mock_remove_p_maps.call_count)
        # Check the fabric state should be mapped to instance
        fc_state = self.vol_drv._get_fabric_state('A')
        self.assertEqual(npiv.FS_INST_MAPPED, fc_state)

    @mock.patch('pypowervm.tasks.vfc_mapper.add_npiv_port_mappings')
    @mock.patch('pypowervm.tasks.vfc_mapper.remove_npiv_port_mappings')
    def test_connect_volume_fc_unmap(self, mock_remove_p_maps,
                                     mock_add_p_maps):
        # Invoke
        meta_key = self.vol_drv._sys_fabric_state_key('A')
        # TestCase when there is no mapping
        self.vol_drv.instance.system_metadata = {meta_key: npiv.FS_UNMAPPED}
        self.vol_drv.connect_volume()

        # Verify
        # Remove mapping should not be called
        # Add the mapping with the instance.
        self.assertEqual(1, mock_add_p_maps.call_count)
        self.assertEqual(0, mock_remove_p_maps.call_count)
        # Check the fabric state should be mapped to instance
        fc_state = self.vol_drv._get_fabric_state('A')
        self.assertEqual(npiv.FS_INST_MAPPED, fc_state)

    @mock.patch('pypowervm.tasks.vfc_mapper.remove_npiv_port_mappings')
    def test_disconnect_volume(self, mock_remove_p_maps):
        # Mock Data
        self.vol_drv.instance.task_state = 'deleting'

        # Invoke
        self.vol_drv.disconnect_volume()

        # Verify
        self.assertEqual(1, mock_remove_p_maps.call_count)

    @mock.patch('pypowervm.tasks.vfc_mapper.remove_npiv_port_mappings')
    def test_disconnect_volume_no_op(self, mock_remove_p_maps):
        """Tests that when the task state is not set, connections are left."""
        # Invoke
        self.vol_drv.disconnect_volume()

        # Verify
        self.assertEqual(0, mock_remove_p_maps.call_count)

    def test_disconnect_volume_no_op_other_state(self):
        """Tests that the deletion doesn't go through on certain states."""
        self.vol_drv.instance.task_state = task_states.RESUMING

        # Invoke
        self.vol_drv.disconnect_volume()
        self.assertEqual(0, self.adpt.read.call_count)

    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.wrap')
    def test_connect_volume_no_map(self, mock_vio_wrap):
        """Tests that if the VFC Mapping exists, another is not added."""
        # Mock Data
        self.vol_drv.connection_info = {'data': {'initiator_target_map':
                                                 {'a': None, 'b': None}}}

        mock_mapping = mock.MagicMock()
        mock_mapping.client_adapter.wwpns = {'a', 'b'}

        mock_vios = mock.MagicMock()
        mock_vios.vfc_mappings = [mock_mapping]

        mock_vio_wrap.return_value = mock_vios

        # Invoke
        self.vol_drv.connect_volume()

        # Verify
        self.assertEqual(0, self.adpt.update.call_count)

    @mock.patch('nova_powervm.virt.powervm.mgmt.get_mgmt_partition')
    @mock.patch('pypowervm.tasks.vfc_mapper.add_npiv_port_mappings')
    def test_wwpns(self, mock_add_port, mock_mgmt_part):
        """Tests that new WWPNs get generated properly."""
        # Mock Data
        mock_add_port.return_value = [('21000024FF649104', 'AA BB'),
                                      ('21000024FF649105', 'CC DD')]
        mock_vios = mock.MagicMock()
        mock_vios.uuid = '3443DB77-AED1-47ED-9AA5-3DB9C6CF7089'
        mock_mgmt_part.return_value = mock_vios
        self.adpt.read.return_value = self.vios_feed_resp

        meta_key = self.vol_drv._sys_meta_fabric_key('A')
        self.vol_drv.instance.system_metadata = {meta_key: None}

        # Invoke
        wwpns = self.vol_drv.wwpns()

        # Check
        self.assertListEqual(['AA', 'BB', 'CC', 'DD'], wwpns)
        self.assertEqual('21000024FF649104,AA,BB,21000024FF649105,CC,DD',
                         self.vol_drv.instance.system_metadata[meta_key])
        xags = [pvm_vios.VIOS.xags.FC_MAPPING, pvm_vios.VIOS.xags.STORAGE]
        self.adpt.read.assert_called_once_with('VirtualIOServer', xag=xags)
        self.assertEqual(1, mock_add_port.call_count)

        # Check when mgmt_uuid is None
        mock_add_port.reset_mock()
        mock_vios.uuid = None
        self.vol_drv.wwpns()
        self.assertEqual(0, mock_add_port.call_count)
        self.assertEqual('mgmt_mapped',
                         self.vol_drv._get_fabric_state('A'))

    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_get_fabric_state')
    def test_wwpns_on_sys_meta(self, mock_fabric_state):
        """Tests that previously stored WWPNs are returned."""
        # Mock
        mock_fabric_state.return_value = npiv.FS_INST_MAPPED
        self.vol_drv.instance.system_metadata = {
            self.vol_drv._sys_meta_fabric_key('A'): 'phys1,a,b,phys2,c,d'}

        # Invoke and Verify
        self.assertListEqual(['a', 'b', 'c', 'd'], self.vol_drv.wwpns())
