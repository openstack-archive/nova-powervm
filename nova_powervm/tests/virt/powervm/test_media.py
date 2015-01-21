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
from pypowervm.tests.wrappers.util import pvmhttp

from nova_powervm.virt.powervm import media as m

VOL_GRP_DATA = 'fake_volume_group.txt'
VOL_GRP_NOVG_DATA = 'fake_volume_group_no_vg.txt'


class TestConfigDrivePowerVM(test.TestCase):
    """Unit Tests for the ConfigDrivePowerVM class.
    """

    def setUp(self):
        super(TestConfigDrivePowerVM, self).setUp()

        # Find directory for response file(s)
        data_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(data_dir, 'data')

        def resp(file_name):
            file_path = os.path.join(data_dir, file_name)
            return pvmhttp.load_pvm_resp(file_path).get_response()

        self.vol_grp_resp = resp(VOL_GRP_DATA)
        self.vol_grp_novg_resp = resp(VOL_GRP_NOVG_DATA)

    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('pypowervm.adapter')
    @mock.patch('nova.api.metadata.base.InstanceMetadata')
    @mock.patch('nova.virt.configdrive.ConfigDriveBuilder.make_drive')
    def test_crt_cfg_dr_iso(self, mock_mkdrv, mock_meta, mock_adpt,
                            mock_vopt_valid):
        """Validates that the image creation method works."""
        cfg_dr_builder = m.ConfigDrivePowerVM(mock_adpt, 'fake_host',
                                              'fake_vios')
        mock_instance = mock.MagicMock()
        mock_instance.name = 'fake-instance'
        mock_files = mock.MagicMock()
        mock_net = mock.MagicMock()
        iso_path, file_name = cfg_dr_builder._create_cfg_dr_iso(mock_instance,
                                                                mock_files,
                                                                mock_net)
        self.assertEqual('fake_instance_config.iso', file_name)
        self.assertEqual('/tmp/cfgdrv/fake_instance_config.iso', iso_path)

    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_create_cfg_dr_iso')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    @mock.patch('os.path.getsize')
    @mock.patch('os.remove')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_upload_lv')
    @mock.patch('pypowervm.wrappers.virtual_io_server.crt_scsi_map_to_vopt')
    @mock.patch('pypowervm.adapter')
    def test_crt_cfg_drv_vopt(self, mock_adpt, mock_vio_w, mock_upld, mock_rm,
                              mock_size, mock_validate, mock_cfg_iso):
        # Mock Returns
        mock_cfg_iso.return_value = '/tmp/cfgdrv/fake.iso', 'fake.iso'
        mock_size.return_value = 10000

        # Run
        cfg_dr_builder = m.ConfigDrivePowerVM(mock_adpt, 'fake_host',
                                              'fake_vios')
        resp = cfg_dr_builder.create_cfg_drv_vopt(mock.MagicMock(),
                                                  mock.MagicMock(),
                                                  mock.MagicMock(),
                                                  'fake_lpar')
        self.assertIsNotNone(resp)
        self.assertTrue(mock_upld.called)

    @mock.patch('pypowervm.adapter.Adapter')
    def test_validate_opt_vg(self, mock_adpt):
        mock_adpt.read.return_value = self.vol_grp_resp
        cfg_dr_builder = m.ConfigDrivePowerVM(mock_adpt, 'fake_host',
                                              'fake_vios')
        self.assertEqual('1e46bbfd-73b6-3c2a-aeab-a1d3f065e92f',
                         cfg_dr_builder._validate_vopt_vg())

    @mock.patch('pypowervm.adapter.Adapter')
    def test_validate_opt_vg_fail(self, mock_adpt):
        mock_adpt.read.return_value = self.vol_grp_novg_resp
        self.assertRaises(m.NoMediaRepoVolumeGroupFound,
                          m.ConfigDrivePowerVM, mock_adpt, 'fake_host',
                          'fake_vios')
