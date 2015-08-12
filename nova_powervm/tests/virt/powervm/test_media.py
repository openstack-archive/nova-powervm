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
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.tests.wrappers.util import pvmhttp
from pypowervm.wrappers import storage as pvm_stor
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm import exception as npvmex
from nova_powervm.virt.powervm import media as m

VOL_GRP_DATA = 'fake_volume_group.txt'
VOL_GRP_NOVG_DATA = 'fake_volume_group_no_vg.txt'

VOL_GRP_WITH_VIOS = 'fake_volume_group_with_vio_data.txt'
VIOS_WITH_VOL_GRP = 'fake_vios_with_volume_group_data.txt'

VIOS_NO_VG = 'fake_vios_feed_no_vg.txt'
VIOS_FEED = 'fake_vios_feed.txt'


class TestConfigDrivePowerVM(test.TestCase):
    """Unit Tests for the ConfigDrivePowerVM class."""

    def setUp(self):
        super(TestConfigDrivePowerVM, self).setUp()

        # Find directory for response file(s)
        data_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(data_dir, 'data')

        self.pypvm = self.useFixture(fx.PyPowerVM())
        self.apt = self.pypvm.apt

        def resp(file_name):
            file_path = os.path.join(data_dir, file_name)
            return pvmhttp.load_pvm_resp(
                file_path, adapter=self.apt).get_response()

        self.vol_grp_resp = resp(VOL_GRP_DATA)
        self.vol_grp_novg_resp = resp(VOL_GRP_NOVG_DATA)

        self.vg_to_vio = resp(VOL_GRP_WITH_VIOS)
        self.vio_to_vg = resp(VIOS_WITH_VOL_GRP)

        self.vio_feed_no_vg = resp(VIOS_NO_VG)
        self.vio_feed = resp(VIOS_FEED)

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
    def test_attach_vopt(self, mock_validate, mock_build_map, mock_add_map):
        # to act as the feed for FeedTaskFx and FeedTask.
        feed = [pvm_vios.VIOS.wrap(self.vio_to_vg)]
        ft_fx = pvm_fx.FeedTaskFx(feed)
        self.useFixture(ft_fx)

        mock_instance = mock.MagicMock(name='fake-instance')

        cfg_dr_builder = m.ConfigDrivePowerVM(self.apt, 'fake_host')
        cfg_dr_builder.vios_uuid = feed[0].uuid
        vopt = mock.Mock()
        self.apt.read.return_value = self.vio_to_vg

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

        cfg_dr_builder._attach_vopt(mock_instance, 'lpar_uuid', vopt)

        # Make sure they were called and validated
        self.assertEqual(1, mock_build_map.call_count)
        self.assertEqual(1, mock_add_map.call_count)
        self.assertEqual(1, ft_fx.patchers['update'].mock.call_count)

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

    def test_validate_opt_vg(self):
        self.apt.read.side_effect = [self.vio_feed, self.vol_grp_resp]
        vg_update = self.vol_grp_resp.feed.entries[0]
        self.apt.update_by_path.return_value = vg_update
        cfg_dr_builder = m.ConfigDrivePowerVM(self.apt, 'fake_host')
        self.assertEqual('1e46bbfd-73b6-3c2a-aeab-a1d3f065e92f',
                         cfg_dr_builder.vg_uuid)

    def test_validate_opt_vg_fail(self):
        self.apt.read.side_effect = [self.vio_feed_no_vg,
                                     self.vol_grp_novg_resp]
        self.assertRaises(npvmex.NoMediaRepoVolumeGroupFound,
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
        mock_remove_map.return_value = ('fake_vios',
                                        vg.vmedia_repos[0].optical_media)

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
