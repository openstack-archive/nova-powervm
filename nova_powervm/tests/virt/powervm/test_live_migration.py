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
#

import mock

from nova import objects
from nova import test

from nova_powervm.tests.virt import powervm
from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm import live_migration as lpm


class TestLPM(test.TestCase):
    def setUp(self):
        super(TestLPM, self).setUp()

        self.flags(disk_driver='localdisk', group='powervm')
        self.drv_fix = self.useFixture(fx.PowerVMComputeDriver())
        self.drv = self.drv_fix.drv
        self.apt = self.drv.adapter

        self.inst = objects.Instance(**powervm.TEST_INSTANCE)
        self.lpmsrc = lpm.LiveMigrationSrc(self.drv, self.inst, {})
        self.lpmdst = lpm.LiveMigrationDest(self.drv, self.inst)

    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM')
    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper')
    @mock.patch('pypowervm.tasks.vterm.close_vterm')
    @mock.patch('pypowervm.wrappers.managed_system.System.migration_data',
                new_callable=mock.PropertyMock, name='MigDataProp')
    def test_lpm_source(self, mock_migrdata, mock_vterm_close, mock_get_wrap,
                        mock_cd):
        migr_data = {'active_migrations_supported': 4,
                     'active_migrations_in_progress': 2}
        mock_migrdata.return_value = migr_data

        with mock.patch.object(
            self.lpmsrc, '_check_migration_ready', return_value=None):

            # Test the bad path first, then patch in values to make suceed
            self.lpmsrc.dest_data = {'dest_proc_compat': 'a,b,c'}
            mock_wrap = mock.Mock()
            mock_get_wrap.return_value = mock_wrap

            self.assertRaises(lpm.LiveMigrationProcCompat,
                              self.lpmsrc.check_source, 'context',
                              'block_device_info')

            # Patch the proc compat fields, to get further
            pm = mock.PropertyMock(return_value='b')
            type(mock_wrap).proc_compat_mode = pm

            self.assertRaises(lpm.LiveMigrationInvalidState,
                              self.lpmsrc.check_source, 'context',
                              'block_device_info')

            pm = mock.PropertyMock(return_value='Not_Migrating')
            type(mock_wrap).migration_state = pm

            # Finally, good path.
            self.lpmsrc.check_source('context', 'block_device_info')
            # Ensure we tried to remove the vopts.
            mock_cd.return_value.dlt_vopt.assert_called_once_with(
                mock.ANY)

            # Ensure migration counts are validated
            migr_data['active_migrations_in_progress'] = 4
            self.assertRaises(lpm.LiveMigrationCapacity,
                              self.lpmsrc.check_source, 'context',
                              'block_device_info')

            # Ensure the vterm was closed
            mock_vterm_close.assert_called_once_with(
                self.apt, mock_wrap.uuid)

    @mock.patch('pypowervm.wrappers.managed_system.System.migration_data',
                new_callable=mock.PropertyMock, name='MigDataProp')
    def test_lpm_dest(self, mock_migrdata):
        src_compute_info = {'stats': {'memory_region_size': 1}}
        dst_compute_info = {'stats': {'memory_region_size': 1}}

        migr_data = {'active_migrations_supported': 4,
                     'active_migrations_in_progress': 2}
        mock_migrdata.return_value = migr_data
        with mock.patch.object(self.drv.host_wrapper, 'refresh') as mock_rfh:

            self.lpmdst.check_destination(
                'context', src_compute_info, dst_compute_info)
            mock_rfh.assert_called_once_with()

            # Ensure migration counts are validated
            migr_data['active_migrations_in_progress'] = 4
            self.assertRaises(lpm.LiveMigrationCapacity,
                              self.lpmdst.check_destination, 'context',
                              src_compute_info, dst_compute_info)
            # Repair the stat
            migr_data['active_migrations_in_progress'] = 2

            # Ensure diff memory sizes raises an exception
            dst_compute_info['stats']['memory_region_size'] = 2
            self.assertRaises(lpm.LiveMigrationMRS,
                              self.lpmdst.check_destination, 'context',
                              src_compute_info, dst_compute_info)

    def test_pre_live_mig(self):
        self.lpmdst.pre_live_migration('context', 'block_device_info',
                                       'network_info', 'disk_info',
                                       {}, [])

    @mock.patch('pypowervm.tasks.migration.migrate_lpar')
    def test_live_migration(self, mock_migr):

        self.lpmsrc.lpar_w = mock.Mock()
        self.lpmsrc.dest_data = dict(
            dest_sys_name='a', dest_ip='1', dest_user_id='neo')
        self.lpmsrc.live_migration('context', 'migrate_data')
        mock_migr.called_once_with('context')

        # Test that we raise errors received during migration
        mock_migr.side_effect = ValueError()
        self.assertRaises(ValueError, self.lpmsrc.live_migration, 'context',
                          'migrate_data')
        mock_migr.called_once_with('context')

    def test_post_live_mig_src(self):
        self.lpmsrc.post_live_migration_at_source('network_info')

    def test_post_live_mig_dest(self):
        self.lpmdst.post_live_migration_at_destination('network_info')

    @mock.patch('pypowervm.tasks.migration.migrate_recover')
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

        lpar_w.can_lpm.return_value = (False, 'Not ready for migration reason')
        self.assertRaises(lpm.LiveMigrationNotReady,
                          self.lpmsrc._check_migration_ready, lpar_w, host_w)
