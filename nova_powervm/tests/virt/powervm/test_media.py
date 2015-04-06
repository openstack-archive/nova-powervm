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
from pypowervm.wrappers import storage as pvm_stor

from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm import media as m

VOL_GRP_DATA = 'fake_volume_group.txt'
VOL_GRP_NOVG_DATA = 'fake_volume_group_no_vg.txt'

VOL_GRP_WITH_VIOS = 'fake_volume_group_with_vio_data.txt'
VIOS_WITH_VOL_GRP = 'fake_vios_with_volume_group_data.txt'

VIOS_NO_VG = 'fake_vios_feed_no_vg.txt'


class TestConfigDrivePowerVM(test.TestCase):
    """Unit Tests for the ConfigDrivePowerVM class."""

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

        self.vg_to_vio = resp(VOL_GRP_WITH_VIOS)
        self.vio_to_vg = resp(VIOS_WITH_VOL_GRP)

        self.vio_feed_no_vg = resp(VIOS_NO_VG)

        self.pypvm = self.useFixture(fx.PyPowerVM())
        self.apt = self.pypvm.apt

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
                '_upload_vopt')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_vscsi_mapping')
    def test_crt_cfg_drv_vopt(self, mock_add_map, mock_upld,
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
        self.assertTrue(mock_add_map.called)

    def test_validate_opt_vg(self):
        self.apt.read.return_value = self.vol_grp_resp
        vg_update = self.vol_grp_resp.feed.entries[0]
        self.apt.update_by_path.return_value = vg_update
        cfg_dr_builder = m.ConfigDrivePowerVM(self.apt, 'fake_host')
        self.assertEqual('1e46bbfd-73b6-3c2a-aeab-a1d3f065e92f',
                         cfg_dr_builder.vg_uuid)

    def test_validate_opt_vg_fail(self):
        self.apt.read.side_effect = [self.vio_feed_no_vg,
                                     self.vol_grp_novg_resp]
        self.assertRaises(m.NoMediaRepoVolumeGroupFound,
                          m.ConfigDrivePowerVM, self.apt, 'fake_host')

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_vopt_mapping')
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_validate_vopt_vg')
    def test_dlt_vopt(self, mock_vop_valid, mock_remove_map):

        # Set up the mock data.
        resp = mock.MagicMock(name='resp')
        type(resp).body = mock.PropertyMock(return_value='2')
        self.apt.read.side_effect = [resp, self.vg_to_vio]

        # We tell it to remove all the optical medias.
        vg = pvm_stor.VG.wrap(self.vg_to_vio)
        mock_remove_map.return_value = vg.vmedia_repos[0].optical_media

        # Make sure that the first update is a VIO and doesn't have the vopt
        # mapping
        def validate_update(*kargs, **kwargs):
            vol_grp = kargs[0]
            # This is the VG update.  Make sure there are no optical medias
            # anymore.
            self.assertEqual(0, len(vol_grp.vmedia_repos[0].optical_media))
            return vol_grp.entry

        self.apt.update_by_path.side_effect = validate_update

        # Invoke the operation
        cfg_dr = m.ConfigDrivePowerVM(self.apt, 'fake_host')
        cfg_dr.dlt_vopt('fake_lpar_uuid')

        self.assertEqual(1, self.apt.update_by_path.call_count)
