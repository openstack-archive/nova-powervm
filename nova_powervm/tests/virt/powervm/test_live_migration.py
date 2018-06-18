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
#

from __future__ import absolute_import

import fixtures
import mock

from nova import exception
from nova import objects
from nova.objects import migrate_data as mig_obj
from nova import test
from nova.tests.unit import fake_network

from nova_powervm.tests.virt import powervm
from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm import live_migration as lpm


class TestLPM(test.NoDBTestCase):
    def setUp(self):
        super(TestLPM, self).setUp()

        self.flags(disk_driver='localdisk', group='powervm')
        self.drv_fix = self.useFixture(fx.PowerVMComputeDriver())
        self.drv = self.drv_fix.drv
        self.apt = self.drv.adapter

        self.inst = objects.Instance(**powervm.TEST_INSTANCE)

        self.network_infos = fake_network.fake_get_instance_nw_info(self, 1)
        self.inst.info_cache = objects.InstanceInfoCache(
            network_info=self.network_infos)

        self.mig_data = mig_obj.PowerVMLiveMigrateData()
        self.mig_data.host_mig_data = {}
        self.mig_data.dest_ip = '1'
        self.mig_data.dest_user_id = 'neo'
        self.mig_data.dest_sys_name = 'a'
        self.mig_data.public_key = 'PublicKey'
        self.mig_data.dest_proc_compat = 'a,b,c'
        self.mig_data.vol_data = {}
        self.mig_data.vea_vlan_mappings = {}

        self.lpmsrc = lpm.LiveMigrationSrc(self.drv, self.inst, self.mig_data)
        self.lpmdst = lpm.LiveMigrationDest(self.drv, self.inst)

        self.add_key = self.useFixture(fixtures.MockPatch(
            'pypowervm.tasks.management_console.add_authorized_key')).mock
        self.get_key = self.useFixture(fixtures.MockPatch(
            'pypowervm.tasks.management_console.get_public_key')).mock
        self.get_key.return_value = 'PublicKey'

        # Short path to the host's migration_data
        self.host_mig_data = self.drv.host_wrapper.migration_data

    @mock.patch('pypowervm.tasks.storage.ScrubOrphanStorageForLpar',
                autospec=True)
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM',
                autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper',
                autospec=True)
    @mock.patch('pypowervm.tasks.vterm.close_vterm', autospec=True)
    def test_lpm_source(self, mock_vterm_close, mock_get_wrap,
                        mock_cd, mock_scrub):
        self.host_mig_data['active_migrations_supported'] = 4
        self.host_mig_data['active_migrations_in_progress'] = 2

        with mock.patch.object(
            self.lpmsrc, '_check_migration_ready', return_value=None):

            # Test the bad path first, then patch in values to make succeed
            mock_wrap = mock.Mock(id=123)
            mock_get_wrap.return_value = mock_wrap

            self.assertRaises(exception.MigrationPreCheckError,
                              self.lpmsrc.check_source, 'context',
                              'block_device_info', [])

            # Patch the proc compat fields, to get further
            pm = mock.PropertyMock(return_value='b')
            type(mock_wrap).proc_compat_mode = pm

            self.assertRaises(exception.MigrationPreCheckError,
                              self.lpmsrc.check_source, 'context',
                              'block_device_info', [])

            pm = mock.PropertyMock(return_value='Not_Migrating')
            type(mock_wrap).migration_state = pm

            # Get a volume driver.
            mock_vol_drv = mock.MagicMock()

            # Finally, good path.
            self.lpmsrc.check_source('context', 'block_device_info',
                                     [mock_vol_drv])
            # Ensure we built a scrubber.
            mock_scrub.assert_called_with(mock.ANY, 123)
            # Ensure we added the subtasks to remove the vopts.
            mock_cd.return_value.dlt_vopt.assert_called_once_with(
                mock.ANY, stg_ftsk=mock_scrub.return_value,
                remove_mappings=False)
            # And ensure the scrubber was executed
            mock_scrub.return_value.execute.assert_called_once_with()
            mock_vol_drv.pre_live_migration_on_source.assert_called_once_with(
                {})

            # Ensure migration counts are validated
            self.host_mig_data['active_migrations_in_progress'] = 4
            self.assertRaises(exception.MigrationPreCheckError,
                              self.lpmsrc.check_source, 'context',
                              'block_device_info', [])

            # Ensure the vterm was closed
            mock_vterm_close.assert_called_once_with(
                self.apt, mock_wrap.uuid)

    def test_lpm_dest(self):
        src_compute_info = {'stats': {'memory_region_size': 1}}
        dst_compute_info = {'stats': {'memory_region_size': 1}}

        self.host_mig_data['active_migrations_supported'] = 4
        self.host_mig_data['active_migrations_in_progress'] = 2
        with mock.patch.object(self.drv.host_wrapper, 'refresh') as mock_rfh:

            self.lpmdst.check_destination(
                'context', src_compute_info, dst_compute_info)
            mock_rfh.assert_called_once_with()

            # Ensure migration counts are validated
            self.host_mig_data['active_migrations_in_progress'] = 4
            self.assertRaises(exception.MigrationPreCheckError,
                              self.lpmdst.check_destination, 'context',
                              src_compute_info, dst_compute_info)
            # Repair the stat
            self.host_mig_data['active_migrations_in_progress'] = 2

            # Ensure diff memory sizes raises an exception
            dst_compute_info['stats']['memory_region_size'] = 2
            self.assertRaises(exception.MigrationPreCheckError,
                              self.lpmdst.check_destination, 'context',
                              src_compute_info, dst_compute_info)

    @mock.patch('pypowervm.tasks.storage.ComprehensiveScrub', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vif.'
                'pre_live_migrate_at_destination', autospec=True)
    def test_pre_live_mig(self, mock_vif_pre, mock_scrub):
        vol_drv = mock.MagicMock()
        network_infos = [{'type': 'pvm_sea'}]

        def update_vea_mapping(adapter, host_uuid, instance, network_info,
                               vea_vlan_mappings):
            # Make sure what comes in is None, but that we change it.
            self.assertEqual(vea_vlan_mappings, {})
            vea_vlan_mappings['test'] = 'resp'

        mock_vif_pre.side_effect = update_vea_mapping

        resp = self.lpmdst.pre_live_migration(
            'context', 'block_device_info', network_infos, 'disk_info',
            self.mig_data, [vol_drv])

        # Make sure the pre_live_migrate_at_destination was invoked for the vif
        mock_vif_pre.assert_called_once_with(
            self.drv.adapter, self.drv.host_uuid, self.inst, network_infos[0],
            mock.ANY)
        self.assertEqual({'test': 'resp'}, self.mig_data.vea_vlan_mappings)

        # Make sure we get something back, and that the volume driver was
        # invoked.
        self.assertIsNotNone(resp)
        vol_drv.pre_live_migration_on_destination.assert_called_once_with(
            self.mig_data.vol_data)
        self.assertEqual(1, mock_scrub.call_count)
        self.add_key.assert_called_once_with(self.apt, 'PublicKey')

        vol_drv.reset_mock()
        raising_vol_drv = mock.Mock()
        raising_vol_drv.pre_live_migration_on_destination.side_effect = (
            Exception('foo'))
        self.assertRaises(
            exception.MigrationPreCheckError, self.lpmdst.pre_live_migration,
            'context', 'block_device_info', network_infos, 'disk_info',
            self.mig_data, [vol_drv, raising_vol_drv])
        vol_drv.pre_live_migration_on_destination.assert_called_once_with({})
        (raising_vol_drv.pre_live_migration_on_destination.
            assert_called_once_with({}))

    def test_src_cleanup(self):
        vol_drv = mock.Mock()
        self.lpmdst.cleanup_volume(vol_drv)
        # Ensure the volume driver is not called
        self.assertEqual(0, vol_drv.cleanup_volume_at_destination.call_count)

    def test_src_cleanup_valid(self):
        vol_drv = mock.Mock()
        self.lpmdst.pre_live_vol_data = {'vscsi-vol-id': 'fake_udid'}
        self.lpmdst.cleanup_volume(vol_drv)
        # Ensure the volume driver was called to clean up the volume.
        vol_drv.cleanup_volume_at_destination.assert_called_once()

    @mock.patch('pypowervm.tasks.migration.migrate_lpar', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.live_migration.LiveMigrationSrc.'
                '_convert_nl_io_mappings', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vif.pre_live_migrate_at_source',
                autospec=True)
    def test_live_migration(self, mock_vif_pre_lpm, mock_convert_mappings,
                            mock_migr):
        mock_trunk = mock.MagicMock()
        mock_vif_pre_lpm.return_value = [mock_trunk]
        mock_convert_mappings.return_value = ['AABBCCDDEEFF/5']

        self.lpmsrc.lpar_w = mock.Mock()
        self.lpmsrc.live_migration('context', self.mig_data)
        mock_migr.assert_called_once_with(
            self.lpmsrc.lpar_w, 'a', sdn_override=True, tgt_mgmt_svr='1',
            tgt_mgmt_usr='neo', validate_only=False,
            virtual_fc_mappings=None, virtual_scsi_mappings=None,
            vlan_check_override=True, vlan_mappings=['AABBCCDDEEFF/5'])

        # Network assertions
        mock_vif_pre_lpm.assert_called_once_with(
            self.drv.adapter, self.drv.host_uuid, self.inst, mock.ANY)
        mock_trunk.delete.assert_called_once()

        # Test that we raise errors received during migration
        mock_migr.side_effect = ValueError()
        self.assertRaises(ValueError, self.lpmsrc.live_migration, 'context',
                          self.mig_data)
        mock_migr.assert_called_with(
            self.lpmsrc.lpar_w, 'a', sdn_override=True, tgt_mgmt_svr='1',
            tgt_mgmt_usr='neo', validate_only=False,
            virtual_fc_mappings=None, virtual_scsi_mappings=None,
            vlan_mappings=['AABBCCDDEEFF/5'], vlan_check_override=True)

    def test_convert_nl_io_mappings(self):
        # Test simple None case
        self.assertIsNone(self.lpmsrc._convert_nl_io_mappings(None))

        # Do some mappings
        test_mappings = {'aa:bb:cc:dd:ee:ff': 5, 'aa:bb:cc:dd:ee:ee': 126}
        expected = ['AABBCCDDEEFF/5', 'AABBCCDDEEEE/126']
        self.assertEqual(
            set(expected),
            set(self.lpmsrc._convert_nl_io_mappings(test_mappings)))

    @mock.patch('pypowervm.tasks.migration.migrate_recover', autospec=True)
    def test_rollback(self, mock_migr):
        self.lpmsrc.lpar_w = mock.Mock()

        # Test no need to rollback
        self.lpmsrc.lpar_w.migration_state = 'Not_Migrating'
        self.lpmsrc.rollback_live_migration('context')
        self.assertTrue(self.lpmsrc.lpar_w.refresh.called)
        self.assertFalse(mock_migr.called)

        # Test calling the rollback
        self.lpmsrc.lpar_w.reset_mock()
        self.lpmsrc.lpar_w.migration_state = 'Pretend its Migrating'
        self.lpmsrc.rollback_live_migration('context')
        self.assertTrue(self.lpmsrc.lpar_w.refresh.called)
        mock_migr.assert_called_once_with(self.lpmsrc.lpar_w, force=True)

        # Test exception from rollback
        mock_migr.reset_mock()
        self.lpmsrc.lpar_w.reset_mock()
        mock_migr.side_effect = ValueError()
        self.lpmsrc.rollback_live_migration('context')
        self.assertTrue(self.lpmsrc.lpar_w.refresh.called)
        mock_migr.assert_called_once_with(self.lpmsrc.lpar_w, force=True)

    def test_check_migration_ready(self):
        lpar_w, host_w = mock.Mock(), mock.Mock()
        lpar_w.can_lpm.return_value = (True, None)
        self.lpmsrc._check_migration_ready(lpar_w, host_w)
        lpar_w.can_lpm.assert_called_once_with(host_w, migr_data={})

        lpar_w.can_lpm.return_value = (False, 'This is the reason message.')
        self.assertRaises(exception.MigrationPreCheckError,
                          self.lpmsrc._check_migration_ready, lpar_w, host_w)

    @mock.patch('pypowervm.tasks.migration.migrate_abort', autospec=True)
    def test_migration_abort(self, mock_mig_abort):
        self.lpmsrc.lpar_w = mock.Mock()
        self.lpmsrc.migration_abort()
        mock_mig_abort.assert_called_once_with(self.lpmsrc.lpar_w)

    @mock.patch('pypowervm.tasks.migration.migrate_recover', autospec=True)
    def test_migration_recover(self, mock_mig_recover):
        self.lpmsrc.lpar_w = mock.Mock()
        self.lpmsrc.migration_recover()
        mock_mig_recover.assert_called_once_with(
            self.lpmsrc.lpar_w, force=True)

    @mock.patch('nova_powervm.virt.powervm.vif.post_live_migrate_at_source',
                autospec=True)
    def test_post_live_migration_at_source(self, mock_vif_post_lpm_at_source):
        network_infos = [{'devname': 'tap-dev1', 'address': 'mac-addr1',
                          'network': {'bridge': 'br-int'}, 'id': 'vif_id_1'},
                         {'devname': 'tap-dev2', 'address': 'mac-addr2',
                          'network': {'bridge': 'br-int'}, 'id': 'vif_id_2'}]
        self.lpmsrc.post_live_migration_at_source(network_infos)
        # Assertions
        for network_info in network_infos:
            mock_vif_post_lpm_at_source.assert_any_call(mock.ANY, mock.ANY,
                                                        mock.ANY, network_info)

    @mock.patch('nova_powervm.virt.powervm.tasks.storage.SaveBDM.execute',
                autospec=True)
    def test_post_live_migration_at_dest(self, mock_save_bdm):
        bdm1, bdm2, vol_drv1, vol_drv2 = [mock.Mock()] * 4
        vals = [(bdm1, vol_drv1), (bdm2, vol_drv2)]
        self.lpmdst.pre_live_vol_data = {'vscsi-vol-id': 'fake_udid',
                                         'vscsi-vol-id2': 'fake_udid2'}
        self.lpmdst.post_live_migration_at_destination('network_infos', vals)
        # Assertions

        for bdm, vol_drv in vals:
            vol_drv.post_live_migration_at_destination.assert_called_with(
                mock.ANY)
        self.assertEqual(len(vals), mock_save_bdm.call_count)
