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
from oslo_config import cfg

from nova.compute import task_states
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.tests.test_utils import pvmhttp
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.tests.virt.powervm.volume import test_driver as test_vol
from nova_powervm.virt.powervm import exception as exc
from nova_powervm.virt.powervm.volume import npiv

VIOS_FEED = 'fake_vios_feed2.txt'

CONF = cfg.CONF


class TestNPIVAdapter(test_vol.TestVolumeAdapter):
    """Tests the NPIV Volume Connector Adapter."""

    def setUp(self):
        super(TestNPIVAdapter, self).setUp()

        self.adpt = self.useFixture(pvm_fx.AdapterFx()).adpt

        def resp(file_name):
            return pvmhttp.load_pvm_resp(
                file_name, adapter=self.adpt).get_response()
        self.vios_feed_resp = resp(VIOS_FEED)
        self.wwpn1 = '21000024FF649104'
        self.wwpn2 = '21000024FF649107'
        self.vios_uuid = '3443DB77-AED1-47ED-9AA5-3DB9C6CF7089'

        # Set up the transaction manager
        feed = pvm_vios.VIOS.wrap(self.vios_feed_resp)
        self.ft_fx = pvm_fx.FeedTaskFx(feed)
        self.useFixture(self.ft_fx)

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

        @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.getter')
        @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
        def init_vol_adpt(mock_pvm_uuid, mock_getter):
            con_info = {'data': {'initiator_target_map': {'i1': ['t1'],
                                                          'i2': ['t2', 't3']},
                        'target_lun': '1', 'volume_id': 'id'}}
            mock_inst = mock.MagicMock()
            mock_pvm_uuid.return_value = '1234'

            # The getter can just return the VIOS values (to remove a read
            # that would otherwise need to be mocked).
            mock_getter.return_value = feed

            return npiv.NPIVVolumeAdapter(self.adpt, 'host_uuid', mock_inst,
                                          con_info)
        self.vol_drv = init_vol_adpt()

    def tearDown(self):
        super(TestNPIVAdapter, self).tearDown()

        self.mock_port_count_p.stop()
        self.mock_fabric_names_p.stop()
        self.mock_fabric_ports_p.stop()

    @mock.patch('nova_powervm.virt.powervm.mgmt.get_mgmt_partition')
    @mock.patch('pypowervm.tasks.vfc_mapper.add_map')
    @mock.patch('pypowervm.tasks.vfc_mapper.remove_maps')
    def test_connect_volume(self, mock_remove_maps, mock_add_map,
                            mock_mgmt_lpar_id):
        # Mock
        self._basic_system_metadata(npiv.FS_MGMT_MAPPED)
        mock_mgmt_lpar_id.return_value = mock.Mock(uuid='1')

        def validate_remove_maps(vios_w, lpar_uuid, client_adpt=None,
                                 port_map=None, **kwargs):
            self.assertEqual('1', lpar_uuid)
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertIsNone(client_adpt)
            self.assertEqual(('21000024FF649104', 'AA BB'), port_map)
            return 'removed'
        mock_remove_maps.side_effect = validate_remove_maps

        def add_map(vios_w, host_uuid, vm_uuid, port_map, **kwargs):
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertEqual('host_uuid', host_uuid)
            self.assertEqual('1234', vm_uuid)
            self.assertEqual(('21000024FF649104', 'AA BB'), port_map)
            return 'good'
        mock_add_map.side_effect = add_map

        # Test connect volume when the fabric is mapped with mgmt partition
        self.vol_drv.connect_volume()

        # Verify.  Mgmt mapping should be removed
        self.assertEqual(1, mock_remove_maps.call_count)
        self.assertEqual(1, mock_add_map.call_count)
        self.assertEqual(1, self.ft_fx.patchers['update'].mock.call_count)
        self.assertEqual(npiv.FS_INST_MAPPED,
                         self.vol_drv._get_fabric_state('A'))

    def test_connect_volume_not_valid(self):
        """Validates that a connect will fail if in a bad state."""
        self.mock_inst_wrap.can_modify_io.return_value = False, 'Bleh'
        self.assertRaises(exc.VolumeAttachFailed, self.vol_drv.connect_volume)

    @mock.patch('pypowervm.tasks.vfc_mapper.add_map')
    @mock.patch('pypowervm.tasks.vfc_mapper.remove_npiv_port_mappings')
    def test_connect_volume_inst_mapped(self, mock_remove_p_maps,
                                        mock_add_map):
        """Test if already connected to an instance, don't do anything"""
        self._basic_system_metadata(npiv.FS_INST_MAPPED)
        mock_add_map.return_value = None

        # Test subsequent connect volume calls when the fabric is mapped with
        # inst partition
        self.vol_drv.connect_volume()

        # Verify
        # Remove mapping should not be called
        self.assertEqual(0, mock_remove_p_maps.call_count)
        self.assertEqual(1, mock_add_map.call_count)
        self.assertEqual(0, self.ft_fx.patchers['update'].mock.call_count)

        # Check the fabric state remains mapped to instance
        self.assertEqual(npiv.FS_INST_MAPPED,
                         self.vol_drv._get_fabric_state('A'))

    @mock.patch('pypowervm.tasks.vfc_mapper.add_map')
    @mock.patch('pypowervm.tasks.vfc_mapper.remove_npiv_port_mappings')
    def test_connect_volume_fc_unmap(self, mock_remove_p_maps,
                                     mock_add_map):
        # Mock
        self._basic_system_metadata(npiv.FS_UNMAPPED)

        def add_map(vios_w, host_uuid, vm_uuid, port_map, **kwargs):
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertEqual('host_uuid', host_uuid)
            self.assertEqual('1234', vm_uuid)
            self.assertEqual(('21000024FF649104', 'AA BB'), port_map)
            return 'good'
        mock_add_map.side_effect = add_map

        # TestCase when there is no mapping
        self.vol_drv.connect_volume()

        # Remove mapping should not be called
        self.assertEqual(0, mock_remove_p_maps.call_count)
        self.assertEqual(1, mock_add_map.call_count)
        self.assertEqual(1, self.ft_fx.patchers['update'].mock.call_count)

    def _basic_system_metadata(self, fabric_state):
        meta_fb_key = self.vol_drv._sys_meta_fabric_key('A')
        meta_fb_map = '21000024FF649104,AA,BB'
        meta_st_key = self.vol_drv._sys_fabric_state_key('A')
        self.vol_drv.instance.system_metadata = {meta_st_key: fabric_state,
                                                 meta_fb_key: meta_fb_map}

    @mock.patch('pypowervm.tasks.vfc_mapper.remove_maps')
    @mock.patch('pypowervm.tasks.vfc_mapper.find_vios_for_vfc_wwpns')
    def test_disconnect_volume(self, mock_find_vios, mock_remove_maps):
        # Mock Data
        self.vol_drv.instance.task_state = 'deleting'

        meta_key = self.vol_drv._sys_meta_fabric_key('A')
        meta_map = '21000024FF649104,AA,BB,21000024FF649105,CC,DD'
        self.vol_drv.instance.system_metadata = {meta_key: meta_map}
        mock_find_vios.return_value = (mock.Mock(uuid=self.vios_uuid),)

        # Invoke
        self.vol_drv.disconnect_volume()

        # Two maps removed on one VIOS
        self.assertEqual(2, mock_remove_maps.call_count)
        self.assertEqual(1, self.ft_fx.patchers['update'].mock.call_count)

    @mock.patch('pypowervm.tasks.vfc_mapper.remove_maps')
    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_get_fabric_meta')
    def test_disconnect_volume_no_fabric_meta(self, mock_get_fabric_meta,
                                              mock_remove_maps):
        # Mock Data.  The fabric_names is set to A by setUp.
        # Force a None return
        self.vol_drv.instance.task_state = 'deleting'
        mock_get_fabric_meta.return_value = []

        # Invoke
        self.vol_drv.disconnect_volume()

        # No mappings should have been removed
        self.assertFalse(mock_remove_maps.called)

    def test_disconnect_volume_not_valid(self):
        """Validates that a disconnect will fail if in a bad state."""
        self.mock_inst_wrap.can_modify_io.return_value = False, 'Bleh'
        self.assertRaises(exc.VolumeDetachFailed,
                          self.vol_drv.disconnect_volume)

    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_remove_maps_for_fabric')
    def test_disconnect_volume_no_op(self, mock_remove_maps):
        """Tests that when the task state is not set, connections are left."""
        # Invoke
        self.vol_drv.disconnect_volume()

        # Verify
        self.assertEqual(0, mock_remove_maps.call_count)

    def test_disconnect_volume_no_op_other_state(self):
        """Tests that the deletion doesn't go through on certain states."""
        self.vol_drv.instance.task_state = task_states.RESUMING

        # Invoke
        self.vol_drv.disconnect_volume()
        self.assertEqual(0, self.adpt.read.call_count)

    def test_connect_volume_no_map(self):
        """Tests that if the VFC Mapping exists, another is not added."""
        # Mock Data
        self.vol_drv._fabric_names.return_value = {}
        self.vol_drv.connection_info = {'data': {'initiator_target_map':
                                                 {'a': None, 'b': None},
                                                 'volume_id': 'vid'}}

        mock_mapping = mock.MagicMock()
        mock_mapping.client_adapter.wwpns = {'a', 'b'}

        mock_vios = mock.MagicMock()
        mock_vios.vfc_mappings = [mock_mapping]

        # Invoke
        self.vol_drv.connect_volume()

    def test_min_xags(self):
        xags = self.vol_drv.min_xags()
        self.assertEqual(2, len(xags))
        self.assertIn(pvm_vios.VIOS.xags.STORAGE, xags)
        self.assertIn(pvm_vios.VIOS.xags.FC_MAPPING, xags)

    def test_is_initial_wwpn(self):
        bad_states = [task_states.DELETING, task_states.MIGRATING]
        for state in bad_states:
            self.vol_drv.instance.task_state = state
            self.assertFalse(self.vol_drv._is_initial_wwpn(
                npiv.FS_UNMAPPED, 'a'))

        # Task state should still be bad.
        self.assertFalse(self.vol_drv._is_initial_wwpn(npiv.FS_UNMAPPED, 'a'))

        # Set a good task state
        self.vol_drv.instance.task_state = task_states.NETWORKING
        self.assertTrue(self.vol_drv._is_initial_wwpn(npiv.FS_UNMAPPED, 'a'))

        # And now no task state.
        self.vol_drv.instance.task_state = None
        self.assertTrue(self.vol_drv._is_initial_wwpn(npiv.FS_UNMAPPED, 'a'))

    def test_is_migration_wwpn(self):
        inst = self.vol_drv.instance
        inst.task_state = task_states.MIGRATING
        inst.host = 'Not Correct Host'
        self.assertTrue(self.vol_drv._is_migration_wwpn(npiv.FS_INST_MAPPED))

        # Try if the instance isn't mapped
        self.assertFalse(self.vol_drv._is_migration_wwpn(npiv.FS_UNMAPPED))

        # Mapped but bad task state
        inst.task_state = task_states.DELETING
        self.assertFalse(self.vol_drv._is_migration_wwpn(npiv.FS_INST_MAPPED))

        # Mapped but on same host
        inst.task_state = task_states.MIGRATING
        inst.host = CONF.host
        self.assertFalse(self.vol_drv._is_migration_wwpn(npiv.FS_INST_MAPPED))

    @mock.patch('nova_powervm.virt.powervm.mgmt.get_mgmt_partition')
    @mock.patch('pypowervm.tasks.vfc_mapper.derive_npiv_map')
    @mock.patch('pypowervm.tasks.vfc_mapper.add_npiv_port_mappings')
    def test_configure_wwpns_for_migration(
            self, mock_add_npiv_port, mock_derive, mock_mgmt_lpar):
        # Mock out the fabric
        meta_fb_key = self.vol_drv._sys_meta_fabric_key('A')
        meta_fb_map = '21000024FF649104,AA,BB,21000024FF649105,CC,DD'
        self.vol_drv.instance.system_metadata = {meta_fb_key: meta_fb_map}

        # Mock the mgmt partition
        mock_mgmt_lpar.return_value = mock.Mock(uuid=0)

        # Mock out what the derive returns
        expected_map = [('21000024FF649104', 'BB AA'),
                        ('21000024FF649105', 'DD CC')]
        mock_derive.return_value = expected_map

        # Invoke
        resp_maps = self.vol_drv._configure_wwpns_for_migration('A')

        # Make sure the add port was done properly
        mock_add_npiv_port.assert_called_once_with(
            self.vol_drv.adapter, self.vol_drv.host_uuid, 0, expected_map)

        # Make sure the updated maps are returned
        expected = [('21000024FF649104', 'BB AA'),
                    ('21000024FF649105', 'DD CC')]
        self.assertEqual(expected, resp_maps)

    @mock.patch('nova_powervm.virt.powervm.mgmt.get_mgmt_partition')
    @mock.patch('pypowervm.tasks.vfc_mapper.add_npiv_port_mappings')
    def test_configure_wwpns_for_migration_existing(
            self, mock_add_npiv_port, mock_mgmt_lpar):
        """Validates nothing is done if WWPNs are already mapped."""
        # Mock out the fabric
        meta_fb_key = self.vol_drv._sys_meta_fabric_key('A')
        meta_fb_map = '21000024FF649104,c05076079cff0fa0,c05076079cff0fa1'
        self.vol_drv.instance.system_metadata = {meta_fb_key: meta_fb_map}

        # Mock the mgmt partition
        mock_mgmt_lpar.return_value = mock.Mock(uuid=0)

        # Invoke
        resp_maps = self.vol_drv._configure_wwpns_for_migration('A')

        # Make sure invocations were not made to do any adds
        self.assertFalse(mock_add_npiv_port.called)
        expected = [('21000024FF649104', 'C05076079CFF0FA0 C05076079CFF0FA1')]
        self.assertEqual(expected, resp_maps)

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
        self.assertListEqual(['AA', 'CC'], wwpns)
        self.assertEqual('21000024FF649104,AA,BB,21000024FF649105,CC,DD',
                         self.vol_drv.instance.system_metadata[meta_key])
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
        self.assertListEqual(['a', 'c'], self.vol_drv.wwpns())

    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_get_fabric_state')
    def test_wwpns_bad_task_state(self, mock_fabric_state):
        """Tests behavior with a bad task state."""
        # Mock
        mock_fabric_state.return_value = npiv.FS_UNMAPPED
        self.vol_drv.instance.system_metadata = {
            self.vol_drv._sys_meta_fabric_key('A'): 'phys1,a,b,phys2,c,d'}

        # Invoke and Verify
        for state in [task_states.DELETING, task_states.MIGRATING]:
            self.vol_drv.instance.task_state = state
            self.assertListEqual(['a', 'c'], self.vol_drv.wwpns())

    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_set_fabric_meta')
    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_get_fabric_meta')
    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_fabric_names')
    def test_post_live_migration_at_destination(
            self, mock_fabric_names, mock_get_fabric_meta,
            mock_set_fabric_meta):
        mock_fabric_names.return_value = ['A', 'B']
        mock_get_fabric_meta.side_effect = [
            [('11', 'AA BB'), ('22', 'CC DD')],
            [('33', 'EE FF')]]

        # Execute the test
        mig_vol_stor = {}
        self.vol_drv.post_live_migration_at_destination(mig_vol_stor)

        mock_set_fabric_meta.assert_any_call(
            'A', [('11', 'BB AA'), ('22', 'DD CC')])
        mock_set_fabric_meta.assert_any_call(
            'B', [('33', 'FF EE')])

        # Invoke a second time.  Should not 're-flip' or even call set.
        mock_set_fabric_meta.reset_mock()
        self.vol_drv.post_live_migration_at_destination(mig_vol_stor)
        self.assertFalse(mock_set_fabric_meta.called)

    @mock.patch('pypowervm.tasks.vfc_mapper.find_vios_for_vfc_wwpns')
    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_get_fabric_meta')
    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_fabric_names')
    def test_pre_live_migration_on_source(
            self, mock_fabric_names, mock_get_fabric_meta,
            mock_find_vios_for_vfc_wwpns):
        mock_fabric_names.return_value = ['A', 'B']
        mock_get_fabric_meta.side_effect = [
            [('11', 'AA BB'), ('22', 'CC DD')],
            [('33', 'EE FF')]]

        def mock_client_adpt(slot):
            return mock.Mock(client_adapter=mock.Mock(slot_number=slot))

        mock_find_vios_for_vfc_wwpns.side_effect = [
            (None, mock_client_adpt(1)), (None, mock_client_adpt(2)),
            (None, mock_client_adpt(3))]

        # Execute the test
        mig_data = {}
        self.vol_drv.pre_live_migration_on_source(mig_data)

        self.assertEqual([1, 2], mig_data.get('npiv_fabric_slots_A'))
        self.assertEqual([3], mig_data.get('npiv_fabric_slots_B'))

    @mock.patch('pypowervm.tasks.vfc_mapper.remove_maps')
    @mock.patch('pypowervm.tasks.vfc_mapper.find_vios_for_vfc_wwpns')
    @mock.patch('pypowervm.tasks.vfc_mapper.'
                'build_migration_mappings_for_fabric')
    @mock.patch('nova_powervm.virt.powervm.mgmt.get_mgmt_partition')
    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_get_fabric_meta')
    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_fabric_names')
    def test_pre_live_migration_on_destination(
            self, mock_fabric_names, mock_get_fabric_meta, mock_mgmt_lpar_id,
            mock_build_mig_map, mock_find_vios_for_vfc_wwpns, mock_remove_map):
        mock_fabric_names.return_value = ['A', 'B']
        mock_get_fabric_meta.side_effect = [[], []]
        mock_mgmt_lpar_id.return_value = mock.Mock(uuid='1')

        src_mig_data = {'npiv_fabric_slots_A': [1, 2],
                        'npiv_fabric_slots_B': [3]}
        dest_mig_data = {}

        mock_build_mig_map.side_effect = [['a'], ['b']]

        # Execute the test
        self.vol_drv.pre_live_migration_on_destination(
            src_mig_data, dest_mig_data)

        self.assertEqual(['a'], dest_mig_data.get('npiv_fabric_mapping_A'))
        self.assertEqual(['b'], dest_mig_data.get('npiv_fabric_mapping_B'))

        # Order of the mappings is not important.
        self.assertEqual(set(['b', 'a']),
                         set(dest_mig_data.get('vfc_lpm_mappings')))

        mock_find_vios_for_vfc_wwpns.return_value = None, None
        dest_mig_data = {}
        mock_fabric_names.return_value = ['A', 'B']
        mock_get_fabric_meta.side_effect = [
            [('11', 'AA BB'), ('22', 'CC DD')],
            [('33', 'EE FF')]]
        mock_build_mig_map.side_effect = [['a'], ['b']]

        # Execute the test
        with self.assertLogs(npiv.__name__, level='WARNING'):
            self.vol_drv.pre_live_migration_on_destination(
                src_mig_data, dest_mig_data)
            # remove_map should not be called since vios_w is None
            self.assertEqual(0, mock_remove_map.call_count)

    def test_set_fabric_meta(self):
        port_map = [('1', 'aa AA'), ('2', 'bb BB'),
                    ('3', 'cc CC'), ('4', 'dd DD'),
                    ('5', 'ee EE'), ('6', 'ff FF'),
                    ('7', 'gg GG'), ('8', 'hh HH'),
                    ('9', 'ii II'), ('10', 'jj JJ')]
        expected = {'npiv_adpt_wwpns_A':
                    '1,aa,AA,2,bb,BB,3,cc,CC,4,dd,DD',
                    'npiv_adpt_wwpns_A_2':
                    '5,ee,EE,6,ff,FF,7,gg,GG,8,hh,HH',
                    'npiv_adpt_wwpns_A_3':
                    '9,ii,II,10,jj,JJ'}
        self.vol_drv.instance.system_metadata = dict()
        self.vol_drv._set_fabric_meta('A', port_map)
        self.assertEqual(self.vol_drv.instance.system_metadata, expected)

    def test_get_fabric_meta(self):
        system_meta = {'npiv_adpt_wwpns_A':
                       '1,aa,AA,2,bb,BB,3,cc,CC,4,dd,DD',
                       'npiv_adpt_wwpns_A_2':
                       '5,ee,EE,6,ff,FF,7,gg,GG,8,hh,HH',
                       'npiv_adpt_wwpns_A_3':
                       '9,ii,II,10,jj,JJ'}
        expected = [('1', 'aa AA'), ('2', 'bb BB'),
                    ('3', 'cc CC'), ('4', 'dd DD'),
                    ('5', 'ee EE'), ('6', 'ff FF'),
                    ('7', 'gg GG'), ('8', 'hh HH'),
                    ('9', 'ii II'), ('10', 'jj JJ')]
        self.vol_drv.instance.system_metadata = system_meta
        fabric_meta = self.vol_drv._get_fabric_meta('A')
        self.assertEqual(fabric_meta, expected)
