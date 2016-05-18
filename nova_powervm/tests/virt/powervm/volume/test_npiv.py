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

from nova.compute import task_states
from oslo_serialization import jsonutils
from pypowervm import const as pvm_const
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.tests.test_utils import pvmhttp
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm import conf as cfg
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
        self.slot_mgr = mock.Mock()

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

    @mock.patch('pypowervm.tasks.vfc_mapper.add_map')
    def test_connect_volume(self, mock_add_map):
        # Mock
        self._basic_system_metadata(npiv.FS_UNMAPPED)
        self.slot_mgr.build_map.get_vfc_slots = mock.Mock(
            return_value=['62'])

        def add_map(vios_w, host_uuid, vm_uuid, port_map, **kwargs):
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertEqual('host_uuid', host_uuid)
            self.assertEqual('1234', vm_uuid)
            self.assertEqual(('21000024FF649104', 'AA BB'), port_map)
            return 'good'
        mock_add_map.side_effect = add_map

        # Test connect volume
        self.vol_drv.connect_volume(self.slot_mgr)

        # Verify that the appropriate connections were made.
        self.assertEqual(1, mock_add_map.call_count)
        mock_add_map.assert_called_once_with(
            mock.ANY, 'host_uuid', '1234', ('21000024FF649104', 'AA BB'),
            lpar_slot_num='62', provided={})
        self.assertEqual(1, self.ft_fx.patchers['update'].mock.call_count)
        self.assertEqual(npiv.FS_INST_MAPPED,
                         self.vol_drv._get_fabric_state('A'))

        # Verify the correct post execute methods were added to the feed task
        self.assertEqual('fab_slot_A_id',
                         self.vol_drv.stg_ftsk._post_exec[0].name)
        self.assertEqual('fab_A_id',
                         self.vol_drv.stg_ftsk._post_exec[1].name)

    def test_connect_volume_not_valid(self):
        """Validates that a connect will fail if in a bad state."""
        self.mock_inst_wrap.can_modify_io.return_value = False, 'Invalid I/O'
        self.assertRaises(exc.VolumeAttachFailed, self.vol_drv.connect_volume,
                          self.slot_mgr)

    def test_connect_volume_bad_wwpn(self):
        """Ensures an error is raised if a bad WWPN is used."""
        self._basic_system_metadata(npiv.FS_UNMAPPED, p_wwpn='bad')
        self.assertRaises(exc.VolumeAttachFailed, self.vol_drv.connect_volume,
                          self.slot_mgr)

    @mock.patch('pypowervm.tasks.vfc_mapper.add_map')
    def test_connect_volume_inst_mapped(self, mock_add_map):
        """Test if already connected to an instance, don't do anything"""
        self._basic_system_metadata(npiv.FS_INST_MAPPED)
        mock_add_map.return_value = None
        self.slot_mgr.build_map.get_vfc_slots = mock.Mock(
            return_value=['62'])

        # Test subsequent connect volume calls when the fabric is mapped with
        # inst partition
        self.vol_drv.connect_volume(self.slot_mgr)

        # Verify
        self.assertEqual(1, mock_add_map.call_count)
        self.assertEqual(0, self.ft_fx.patchers['update'].mock.call_count)

        # Check the fabric state remains mapped to instance
        self.assertEqual(npiv.FS_INST_MAPPED,
                         self.vol_drv._get_fabric_state('A'))

    def _basic_system_metadata(self, fabric_state, p_wwpn='21000024FF649104'):
        meta_fb_key = self.vol_drv._sys_meta_fabric_key('A')
        meta_fb_map = '%s,AA,BB' % p_wwpn
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
        self.vol_drv.disconnect_volume(self.slot_mgr)

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
        self.vol_drv.disconnect_volume(self.slot_mgr)

        # No mappings should have been removed
        self.assertFalse(mock_remove_maps.called)

    def test_disconnect_volume_not_valid(self):
        """Validates that a disconnect will fail if in a bad state."""
        self.mock_inst_wrap.can_modify_io.return_value = False, 'Bleh'
        self.assertRaises(exc.VolumeDetachFailed,
                          self.vol_drv.disconnect_volume, self.slot_mgr)

    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_fabric_names')
    def test_disconnect_volume_not_on_same_host(self, mock_names):
        """Validates a disconnect still removes mapping when host's differ."""
        self.vol_drv.instance.task_state = None
        self.vol_drv.instance.host = 'not_host_in_conf'

        self.vol_drv.disconnect_volume(self.slot_mgr)
        mock_names.assert_called_once()

    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_fabric_names')
    def test_disconnect_volume_spawning_not_on_same_host(self, mock_names):
        """Validates disconnect when instance is spawning on another host."""
        self.vol_drv.instance.task_state = 'spawning'
        self.vol_drv.instance.host = 'not_host_in_conf'

        self.vol_drv.disconnect_volume(self.slot_mgr)
        mock_names.assert_called_once()

    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_remove_maps_for_fabric')
    def test_disconnect_volume_no_op(self, mock_remove_maps):
        """Tests that when the task state is not set, connections are left."""
        # Invoke
        self.vol_drv.instance.task_state = None
        self.vol_drv.instance.host = None

        self.vol_drv.disconnect_volume(self.slot_mgr)

        # Verify
        self.assertEqual(0, mock_remove_maps.call_count)

    def test_disconnect_volume_no_op_other_state(self):
        """Tests that the deletion doesn't go through on certain states."""
        self.vol_drv.instance.task_state = task_states.RESUMING
        self.vol_drv.instance.host = CONF.host

        # Invoke
        self.vol_drv.disconnect_volume(self.slot_mgr)
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
        self.vol_drv.connect_volume(self.slot_mgr)

    def test_min_xags(self):
        xags = self.vol_drv.min_xags()
        self.assertEqual(2, len(xags))
        self.assertIn(pvm_const.XAG.VIO_STOR, xags)
        self.assertIn(pvm_const.XAG.VIO_FMAP, xags)

    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_get_fabric_meta')
    def test_is_initial_wwpn(self, mock_fabric_meta):
        # The deleting state is for roll back on spawn.  Migrating is a
        # scenario where you can't be creating new wwpns
        mock_fabric_meta.return_value = [('21000024FF649104', 'virt1 virt2')]
        bad_states = [task_states.DELETING, task_states.MIGRATING]
        for state in bad_states:
            self.vol_drv.instance.task_state = state
            self.assertFalse(self.vol_drv._is_initial_wwpn(
                npiv.FS_UNMAPPED, 'a'))

        # Task state should still be bad.
        self.assertFalse(self.vol_drv._is_initial_wwpn(npiv.FS_UNMAPPED, 'a'))

        # Set a good task state, but fails due to the WWPNs already being
        # hosted
        self.vol_drv.instance.task_state = task_states.NETWORKING
        self.assertFalse(self.vol_drv._is_initial_wwpn(npiv.FS_UNMAPPED, 'a'))

        # Validate that having no fabric metadata returns that this is an
        # initial wwpn
        mock_fabric_meta.return_value = []
        self.assertTrue(self.vol_drv._is_initial_wwpn(npiv.FS_UNMAPPED, 'a'))

        # Validate that has fabric metadata of a different host, and therefore
        # is still a valid initial wwpn.  It is initial because it simulates
        # a reschedule on a new host.
        mock_fabric_meta.return_value = [('BAD_WWPN', 'virt1 virt2')]
        self.assertTrue(self.vol_drv._is_initial_wwpn(npiv.FS_UNMAPPED, 'a'))

        # And now no task state.
        self.vol_drv.instance.task_state = None
        self.assertTrue(self.vol_drv._is_initial_wwpn(npiv.FS_UNMAPPED, 'a'))

    def test_is_migration_wwpn(self):
        inst = self.vol_drv.instance

        # Migrating on different host
        inst.task_state = task_states.MIGRATING
        inst.host = 'Not Correct Host'
        self.assertTrue(self.vol_drv._is_migration_wwpn(npiv.FS_INST_MAPPED))

        # Try if the instance isn't mapped
        self.assertFalse(self.vol_drv._is_migration_wwpn(npiv.FS_UNMAPPED))

        # Simulate a rollback on the target host from a live migration failure
        inst.task_state = None
        self.assertTrue(self.vol_drv._is_migration_wwpn(npiv.FS_INST_MAPPED))

        # Mapped but on same host
        inst.task_state = task_states.MIGRATING
        inst.host = CONF.host
        self.assertFalse(self.vol_drv._is_migration_wwpn(npiv.FS_INST_MAPPED))

    @mock.patch('pypowervm.tasks.vfc_mapper.derive_npiv_map')
    def test_configure_wwpns_for_migration(self, mock_derive):
        # Mock out the fabric
        meta_fb_key = self.vol_drv._sys_meta_fabric_key('A')
        meta_fb_map = '21000024FF649104,AA,BB,21000024FF649105,CC,DD'
        self.vol_drv.instance.system_metadata = {meta_fb_key: meta_fb_map}

        # Mock out what the derive returns
        expected_map = [('21000024FF649104', 'BB AA'),
                        ('21000024FF649105', 'DD CC')]
        mock_derive.return_value = expected_map

        # Invoke
        resp_maps = self.vol_drv._configure_wwpns_for_migration('A')

        # Make sure the updated maps are returned
        expected = [('21000024FF649104', 'BB AA'),
                    ('21000024FF649105', 'DD CC')]
        self.assertEqual(expected, resp_maps)
        mock_derive.assert_called_with(
            mock.ANY, ['21000024FF649104', '21000024FF649107'],
            ['BB', 'AA', 'DD', 'CC'])

    @mock.patch('pypowervm.tasks.vfc_mapper.derive_npiv_map')
    def test_configure_wwpns_for_migration_existing(self, mock_derive):
        """Validates nothing is done if WWPNs are already flipped."""
        # Mock out the fabric
        meta_fb_key = self.vol_drv._sys_meta_fabric_key('A')
        meta_fb_map = '21000024FF649104,C05076079CFF0FA0,C05076079CFF0FA1'
        meta_fb_st_key = self.vol_drv._sys_fabric_state_key('A')
        meta_fb_st_val = npiv.FS_MIGRATING
        self.vol_drv.instance.system_metadata = {
            meta_fb_key: meta_fb_map, meta_fb_st_key: meta_fb_st_val}

        # Invoke
        resp_maps = self.vol_drv._configure_wwpns_for_migration('A')

        # Make sure that the order of the client WWPNs is not changed.
        expected = [('21000024FF649104', 'C05076079CFF0FA0 C05076079CFF0FA1')]
        self.assertEqual(expected, resp_maps)
        self.assertFalse(mock_derive.called)

    @mock.patch('pypowervm.tasks.vfc_mapper.build_wwpn_pair')
    @mock.patch('pypowervm.tasks.vfc_mapper.derive_npiv_map')
    def test_wwpns(self, mock_derive, mock_build_pair):
        """Tests that new WWPNs get generated properly."""
        # Mock Data
        mock_derive.return_value = [('21000024FF649104', 'AA BB'),
                                    ('21000024FF649105', 'CC DD')]
        self.adpt.read.return_value = self.vios_feed_resp

        meta_key = self.vol_drv._sys_meta_fabric_key('A')
        self.vol_drv.instance.system_metadata = {meta_key: None}

        # Invoke
        wwpns = self.vol_drv.wwpns()

        # Check
        self.assertListEqual(['AA', 'CC'], wwpns)
        self.assertEqual('21000024FF649104,AA,BB,21000024FF649105,CC,DD',
                         self.vol_drv.instance.system_metadata[meta_key])
        self.assertEqual(1, mock_derive.call_count)
        self.assertTrue(self.vol_drv.instance.save.called)

    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_get_fabric_state')
    def test_wwpns_on_sys_meta(self, mock_fabric_state):
        """Tests that previously stored WWPNs are returned."""
        # Mock
        mock_fabric_state.return_value = npiv.FS_INST_MAPPED
        self.vol_drv.instance.host = CONF.host
        self.vol_drv.instance.system_metadata = {
            self.vol_drv._sys_meta_fabric_key('A'): 'phys1,a,b,phys2,c,d'}

        # Invoke and Verify
        self.assertListEqual(['a', 'c'], self.vol_drv.wwpns())

    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_configure_wwpns_for_migration')
    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_is_migration_wwpn')
    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_is_initial_wwpn')
    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_get_fabric_state')
    def test_wwpns_for_migration(self, mock_fabric_state, mock_initial,
                                 mock_migration, mock_configure):
        """Tests that wwpns for migration are generated properly."""
        # Mock
        mock_fabric_state.return_value = npiv.FS_INST_MAPPED
        mock_initial.return_value = False
        mock_migration.return_value = True
        mock_configure.return_value = [('phys1', 'a b'), ('phys2', 'c d')]
        self.vol_drv.stg_ftsk = mock.MagicMock()

        # Invoke and Verify
        self.assertListEqual(['a', 'c'], self.vol_drv.wwpns())

        # Verify that on migration, the WWPNs are reversed.
        self.assertEqual(1, self.vol_drv.stg_ftsk.feed.reverse.call_count)

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

    @mock.patch('pypowervm.tasks.vfc_mapper.find_vios_for_vfc_wwpns')
    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_set_fabric_meta')
    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_get_fabric_meta')
    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_fabric_names')
    def test_post_live_migration_at_destination(
            self, mock_fabric_names, mock_get_fabric_meta,
            mock_set_fabric_meta, mock_find_wwpns):
        mock_fabric_names.return_value = ['A', 'B']
        mock_get_fabric_meta.side_effect = [
            [('S1', 'AA BB'), ('S2', 'CC DD')],
            [('S3', 'EE FF')]]

        # This represents the new physical WWPNs on the target server side.
        mock_find_wwpns.side_effect = [
            (None, mock.Mock(backing_port=mock.Mock(wwpn='T1'))),
            (None, mock.Mock(backing_port=mock.Mock(wwpn='T2'))),
            (None, mock.Mock(backing_port=mock.Mock(wwpn='T3')))]

        # Execute the test
        mig_vol_stor = {}
        self.vol_drv.post_live_migration_at_destination(mig_vol_stor)

        # Client WWPNs should be flipped and the new physical WWPNs should be
        # associated with them.
        mock_set_fabric_meta.assert_any_call(
            'A', [('T1', 'BB AA'), ('T2', 'DD CC')])
        mock_set_fabric_meta.assert_any_call(
            'B', [('T3', 'FF EE')])

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
            return mock.Mock(client_adapter=mock.Mock(lpar_slot_num=slot))

        mock_find_vios_for_vfc_wwpns.side_effect = [
            (None, mock_client_adpt(1)), (None, mock_client_adpt(2)),
            (None, mock_client_adpt(3))]

        # Execute the test
        mig_data = {}
        self.vol_drv.pre_live_migration_on_source(mig_data)

        self.assertEqual('[1, 2]', mig_data.get('src_npiv_fabric_slots_A'))
        self.assertEqual('[3]', mig_data.get('src_npiv_fabric_slots_B'))
        # Ensure only string data is placed in the dict.
        for key in mig_data:
            self.assertEqual(str, type(mig_data[key]))

    @mock.patch('pypowervm.tasks.vfc_mapper.'
                'build_migration_mappings_for_fabric')
    @mock.patch('nova_powervm.virt.powervm.volume.npiv.NPIVVolumeAdapter.'
                '_fabric_names')
    def test_pre_live_migration_on_destination(
            self, mock_fabric_names, mock_build_mig_map):
        mock_fabric_names.return_value = ['A', 'B']

        mig_data = {'src_npiv_fabric_slots_A': jsonutils.dumps([1, 2]),
                    'src_npiv_fabric_slots_B': jsonutils.dumps([3])}

        mock_build_mig_map.side_effect = [['a'], ['b']]
        self.vol_drv.stg_ftsk = mock.MagicMock()

        # Execute the test
        self.vol_drv.pre_live_migration_on_destination(mig_data)

        self.assertEqual('["a"]', mig_data.get('dest_npiv_fabric_mapping_A'))
        self.assertEqual('["b"]', mig_data.get('dest_npiv_fabric_mapping_B'))
        # Ensure only string data is placed in the dict.
        for key in mig_data:
            self.assertEqual(str, type(mig_data[key]))

        # Order of the mappings is not important.
        self.assertEqual(
            {'b', 'a'},
            set(jsonutils.loads(mig_data.get('vfc_lpm_mappings'))))

        # Verify that on migration, the WWPNs are reversed.
        self.assertEqual(2, self.vol_drv.stg_ftsk.feed.reverse.call_count)

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

        # Clear out the metadata and make sure it sticks.
        self.vol_drv._set_fabric_meta('A', [])
        self.assertEqual(self.vol_drv.instance.system_metadata, {})

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

    def test_vol_type(self):
        self.assertEqual('npiv', self.vol_drv.vol_type())
