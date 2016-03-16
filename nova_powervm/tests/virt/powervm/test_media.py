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
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.wrappers import storage as pvm_stg
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.virt.powervm import exception as npvmex
from nova_powervm.virt.powervm import media as m


class TestConfigDrivePowerVM(test.TestCase):
    """Unit Tests for the ConfigDrivePowerVM class."""

    def setUp(self):
        super(TestConfigDrivePowerVM, self).setUp()

        self.apt = self.useFixture(pvm_fx.AdapterFx()).adpt

        # Wipe out the static variables, so that the revalidate is called
        m.ConfigDrivePowerVM._cur_vios_uuid = None
        m.ConfigDrivePowerVM._cur_vios_name = None
        m.ConfigDrivePowerVM._cur_vg_uuid = None

    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('nova.api.metadata.base.InstanceMetadata')
    @mock.patch('nova.virt.configdrive.ConfigDriveBuilder.make_drive')
    def test_crt_cfg_dr_iso(self, mock_mkdrv, mock_meta, mock_vopt_valid):
        """Validates that the image creation method works."""
        cfg_dr_builder = m.ConfigDrivePowerVM(self.apt, 'host_uuid')
        mock_instance = mock.MagicMock()
        mock_instance.name = 'fake-instance'
        mock_instance.uuid = '1e46bbfd-73b6-3c2a-aeab-a1d3f065e92f'
        mock_files = mock.MagicMock()
        mock_net = mock.MagicMock()
        iso_path, file_name = cfg_dr_builder._create_cfg_dr_iso(mock_instance,
                                                                mock_files,
                                                                mock_net)
        self.assertEqual('cfg_fake_instance.iso', file_name)
        self.assertEqual('/tmp/cfgdrv/cfg_fake_instance.iso', iso_path)
        # Make sure the length is limited properly
        mock_instance.name = 'fake-instance-with-name-that-is-too-long'
        iso_path, file_name = cfg_dr_builder._create_cfg_dr_iso(mock_instance,
                                                                mock_files,
                                                                mock_net)
        self.assertEqual('cfg_fake_instance_with_name_that_.iso', file_name)
        self.assertEqual('/tmp/cfgdrv/cfg_fake_instance_with_name_that_.iso',
                         iso_path)

    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_create_cfg_dr_iso')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('os.path.getsize')
    @mock.patch('os.remove')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_upload_vopt')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_attach_vopt')
    def test_crt_cfg_drv_vopt(self, mock_attach, mock_upld,
                              mock_rm, mock_size, mock_validate, mock_cfg_iso):
        # Mock Returns
        mock_cfg_iso.return_value = '/tmp/cfgdrv/fake.iso', 'fake.iso'
        mock_size.return_value = 10000
        mock_upld.return_value = (mock.Mock(), None)

        # Run
        cfg_dr_builder = m.ConfigDrivePowerVM(self.apt, 'fake_host')
        cfg_dr_builder.create_cfg_drv_vopt(mock.MagicMock(), mock.MagicMock(),
                                           mock.MagicMock(), 'fake_lpar')
        self.assertTrue(mock_upld.called)
        self.assertTrue(mock_attach.called)
        mock_attach.assert_called_with(mock.ANY, 'fake_lpar', mock.ANY, None)

    @mock.patch('pypowervm.tasks.scsi_mapper.add_map')
    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('pypowervm.utils.transaction.WrapperTask')
    def test_attach_vopt(self, mock_class_wrapper_task, mock_validate,
                         mock_build_map, mock_add_map):
        # Create objects to test with
        mock_instance = mock.MagicMock(name='fake-instance')
        cfg_dr_builder = m.ConfigDrivePowerVM(self.apt, 'fake_host')
        vopt = mock.Mock()
        mock_vios = mock.Mock(spec=pvm_vios.VIOS)
        mock_vios.configure_mock(name='vios name')

        # Mock methods not currently under test
        mock_wrapper_task = mock.MagicMock()
        mock_class_wrapper_task.return_value = mock_wrapper_task

        def call_param(param):
            param(mock_vios)
        mock_wrapper_task.add_functor_subtask.side_effect = call_param

        def validate_build(host_uuid, vios_w, lpar_uuid, vopt_elem):
            self.assertEqual('fake_host', host_uuid)
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertEqual('lpar_uuid', lpar_uuid)
            self.assertEqual(vopt, vopt_elem)
            return 'map'
        mock_build_map.side_effect = validate_build

        def validate_add(vios_w, mapping):
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertEqual(mapping, 'map')
            return 'added'
        mock_add_map.side_effect = validate_add

        # Run the actual test
        cfg_dr_builder._attach_vopt(mock_instance, 'lpar_uuid', vopt)

        # Make sure they were called and validated
        self.assertTrue(mock_wrapper_task.execute.called)
        self.assertEqual(1, mock_build_map.call_count)
        self.assertEqual(1, mock_add_map.call_count)

    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    def test_mgmt_cna_to_vif(self, mock_validate):
        mock_cna = mock.MagicMock()
        mock_cna.mac = "FAD4433ED120"

        # Run
        cfg_dr_builder = m.ConfigDrivePowerVM(self.apt, 'fake_host')
        vif = cfg_dr_builder._mgmt_cna_to_vif(mock_cna)

        # Validate
        self.assertEqual(vif.get('address'), "fa:d4:43:3e:d1:20")
        self.assertEqual(vif.get('id'), 'mgmt_vif')
        self.assertIsNotNone(vif.get('network'))
        self.assertEqual(1, len(vif.get('network').get('subnets')))
        subnet = vif.get('network').get('subnets')[0]
        self.assertEqual(6, subnet.get('version'))
        self.assertEqual('fe80::/64', subnet.get('cidr'))
        ip = subnet.get('ips')[0]
        self.assertEqual('fe80::f8d4:43ff:fe3e:d120', ip.get('address'))

    def test_mac_to_link_local(self):
        mac = 'fa:d4:43:3e:d1:20'
        self.assertEqual('fe80::f8d4:43ff:fe3e:d120',
                         m.ConfigDrivePowerVM._mac_to_link_local(mac))

        mac = '00:00:00:00:00:00'
        self.assertEqual('fe80::0200:00ff:fe00:0000',
                         m.ConfigDrivePowerVM._mac_to_link_local(mac))

        mac = 'ff:ff:ff:ff:ff:ff'
        self.assertEqual('fe80::fdff:ffff:feff:ffff',
                         m.ConfigDrivePowerVM._mac_to_link_local(mac))

    @mock.patch('pypowervm.wrappers.storage.VG.get')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.get')
    def test_validate_vopt_vg1(self, mock_vios_get, mock_vg_get):
        """One VIOS, rootvg found; locals are set."""
        # Init objects to test with
        mock_vg = mock.Mock()
        mock_vg.configure_mock(name='rootvg',
                               uuid='1e46bbfd-73b6-3c2a-aeab-a1d3f065e92f',
                               vmedia_repos=['repo'])
        mock_vg_get.return_value = [mock_vg]
        mock_vios = mock.Mock()
        mock_vios.configure_mock(name='the_vios', uuid='vios_uuid',
                                 rmc_state='active')
        mock_vios_get.return_value = [mock_vios]

        # Run
        cfg_dr_builder = m.ConfigDrivePowerVM(self.apt, 'fake_host')

        # Validate
        self.assertEqual('1e46bbfd-73b6-3c2a-aeab-a1d3f065e92f',
                         cfg_dr_builder.vg_uuid)
        self.assertEqual('the_vios', cfg_dr_builder.vios_name)
        self.assertEqual('vios_uuid', cfg_dr_builder.vios_uuid)

    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '__init__', new=mock.MagicMock(return_value=None))
    def _mock_cfg_dr_no_validate(self):
        """Mock ConfigDrivePowerVM without running _validate_vopt_vg."""
        cfg_dr = m.ConfigDrivePowerVM(self.apt, 'fake_host')
        cfg_dr.adapter = self.apt
        cfg_dr.host_uuid = 'fake_host'
        return cfg_dr

    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.get')
    @mock.patch('pypowervm.wrappers.storage.VG.get')
    def test_validate_vopt_vg2(self, mock_vg_get, mock_vios_get):
        """Dual VIOS, first is inactive; statics are set."""
        # Init objects to test with
        cfg_dr = self._mock_cfg_dr_no_validate()
        vwrap1 = mock.Mock(rmc_state='#busy')
        vwrap2 = mock.Mock()
        vwrap2.configure_mock(name='vname', rmc_state='active', uuid='vio_id')
        mock_vios_get.return_value = [vwrap1, vwrap2]
        vg_wrap = mock.Mock()
        vg_wrap.configure_mock(name='rootvg', vmedia_repos=[1], uuid='vg_uuid')
        mock_vg_get.return_value = [vg_wrap]

        # Run
        cfg_dr._validate_vopt_vg()

        # Validate
        self.assertEqual('vg_uuid', cfg_dr._cur_vg_uuid)
        self.assertEqual('vio_id', cfg_dr._cur_vios_uuid)
        self.assertEqual('vname', cfg_dr._cur_vios_name)

    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.get')
    @mock.patch('pypowervm.wrappers.storage.VG.get')
    @mock.patch('pypowervm.wrappers.storage.VMediaRepos.bld')
    def test_validate_vopt_vg3(self, mock_vmr_bld, mock_vg_get, mock_vios_get):
        """Dual VIOS, multiple VGs, repos on non-rootvg."""
        cfg_dr = self._mock_cfg_dr_no_validate()
        vwrap1 = mock.Mock()
        vwrap1.configure_mock(name='vio1', rmc_state='active', uuid='vio_id1')
        vwrap2 = mock.Mock()
        vwrap2.configure_mock(name='vio2', rmc_state='active', uuid='vio_id2')
        mock_vios_get.return_value = [vwrap1, vwrap2]
        vg1 = mock.Mock()
        vg1.configure_mock(name='rootvg', vmedia_repos=[], uuid='vg1')
        vg2 = mock.Mock()
        vg2.configure_mock(name='other1vg', vmedia_repos=[], uuid='vg2')
        vg3 = mock.Mock()
        vg3.configure_mock(name='rootvg', vmedia_repos=[], uuid='vg3')
        vg4 = mock.Mock()
        vg4.configure_mock(name='other2vg', vmedia_repos=[1], uuid='vg4')

        # 1: Find the media repos on non-rootvg on the second VIOS
        mock_vg_get.side_effect = [[vg1, vg2], [vg3, vg4]]

        cfg_dr._validate_vopt_vg()

        # Found the repos on VIOS 2, VG 2
        self.assertEqual('vg4', cfg_dr._cur_vg_uuid)
        self.assertEqual('vio_id2', cfg_dr._cur_vios_uuid)
        self.assertEqual('vio2', cfg_dr._cur_vios_name)

        mock_vios_get.reset_mock()
        mock_vg_get.reset_mock()

        # 2: At this point, the statics are set.  If we validate again, and the
        # VG.get returns the right one, we should bail out early.
        mock_vg_get.side_effect = None
        mock_vg_get.return_value = vg4

        cfg_dr._validate_vopt_vg()

        # Statics unchanged
        self.assertEqual('vg4', cfg_dr._cur_vg_uuid)
        self.assertEqual('vio_id2', cfg_dr._cur_vios_uuid)
        self.assertEqual('vio2', cfg_dr._cur_vios_name)
        # We didn't have to query the VIOS
        mock_vios_get.assert_not_called()
        # We only did VG.get once
        self.assertEqual(1, mock_vg_get.call_count)

        mock_vg_get.reset_mock()

        # 3: Same again, but this time the repos is somewhere else.  We should
        # find it.
        vg4.vmedia_repos = []
        vg2.vmedia_repos = [1]
        # The first VG.get is looking for the already-set repos.  The second
        # will be the feed from the first VIOS.  There should be no third call,
        # since we should find the repos on VIOS 2.
        mock_vg_get.side_effect = [vg4, [vg1, vg2]]

        cfg_dr._validate_vopt_vg()

        self.assertEqual('vg2', cfg_dr._cur_vg_uuid)
        self.assertEqual('vio_id1', cfg_dr._cur_vios_uuid)
        self.assertEqual('vio1', cfg_dr._cur_vios_name)

        mock_vg_get.reset_mock()
        mock_vios_get.reset_mock()

        # 4: No repository anywhere - need to create one.  The default VG name
        # (rootvg) exists in multiple places.  Ensure we create in the first
        # one, for efficiency.
        vg2.vmedia_repos = []
        mock_vg_get.side_effect = [vg1, [vg1, vg2], [vg3, vg4]]
        vg1.update.return_value = vg1

        cfg_dr._validate_vopt_vg()

        self.assertEqual('vg1', cfg_dr._cur_vg_uuid)
        self.assertEqual('vio_id1', cfg_dr._cur_vios_uuid)
        self.assertEqual('vio1', cfg_dr._cur_vios_name)
        self.assertEqual([mock_vmr_bld.return_value], vg1.vmedia_repos)

        mock_vg_get.reset_mock()
        mock_vios_get.reset_mock()
        vg1.update.reset_mock()

        # 5: No repos - need to create.  Make sure conf setting is honored.
        vg1.vmedia_repos = []
        self.flags(vopt_media_volume_group='other2vg', group='powervm')

        mock_vg_get.side_effect = [vg1, [vg1, vg2], [vg3, vg4]]
        vg4.update.return_value = vg4

        cfg_dr._validate_vopt_vg()

        self.assertEqual('vg4', cfg_dr._cur_vg_uuid)
        self.assertEqual('vio_id2', cfg_dr._cur_vios_uuid)
        self.assertEqual('vio2', cfg_dr._cur_vios_name)
        self.assertEqual([mock_vmr_bld.return_value], vg4.vmedia_repos)
        vg1.update.assert_not_called()

        mock_vg_get.reset_mock()
        mock_vios_get.reset_mock()

        # 6: No repos, and a configured VG name that doesn't exist
        vg4.vmedia_repos = []
        self.flags(vopt_media_volume_group='mythicalvg', group='powervm')
        mock_vg_get.side_effect = [vg1, [vg1, vg2], [vg3, vg4]]

        self.assertRaises(npvmex.NoMediaRepoVolumeGroupFound,
                          cfg_dr._validate_vopt_vg)

    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg', new=mock.MagicMock())
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                'add_dlt_vopt_tasks')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.wrap',
                new=mock.MagicMock())
    @mock.patch('pypowervm.utils.transaction.FeedTask')
    @mock.patch('pypowervm.utils.transaction.FeedTask.execute')
    def test_dlt_vopt_no_map(self, mock_execute, mock_class_feed_task,
                             mock_add_dlt_vopt_tasks):
        # Init objects to test with
        mock_feed_task = mock.MagicMock()
        mock_class_feed_task.return_value = mock_feed_task

        # Invoke the operation
        cfg_dr = m.ConfigDrivePowerVM(self.apt, 'fake_host')
        cfg_dr.dlt_vopt('2', remove_mappings=False)

        # Verify expected methods were called
        mock_add_dlt_vopt_tasks.assert_called_with(
            '2', mock_feed_task, remove_mappings=False)
        self.assertTrue(mock_feed_task.execute.called)

    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg', new=mock.MagicMock())
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id',
                new=mock.MagicMock(return_value='2'))
    @mock.patch('pypowervm.tasks.scsi_mapper.gen_match_func')
    @mock.patch('pypowervm.tasks.scsi_mapper.find_maps')
    def test_add_dlt_vopt_tasks(self, mock_find_maps, mock_gen_match_func):
        # Init objects to test with
        cfg_dr = m.ConfigDrivePowerVM(self.apt, 'fake_host')
        stg_ftsk = mock.MagicMock()
        cfg_dr.vios_uuid = 'vios_uuid'
        lpar_uuid = 'lpar_uuid'

        # Run
        cfg_dr.add_dlt_vopt_tasks(lpar_uuid, stg_ftsk)

        # Validate
        mock_gen_match_func.assert_called_with(pvm_stg.VOptMedia)
        mock_find_maps.assert_called_with(
            stg_ftsk.get_wrapper().scsi_mappings, client_lpar_id='2',
            match_func=mock_gen_match_func.return_value)
        self.assertTrue(stg_ftsk.add_post_execute.called)
        self.assertTrue(
            stg_ftsk.wrapper_tasks['vios_uuid'].add_functor_subtask.called)
