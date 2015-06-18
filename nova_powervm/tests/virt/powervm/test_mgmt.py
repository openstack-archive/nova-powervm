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
from pypowervm.tests.wrappers.util import pvmhttp
from pypowervm.wrappers import logical_partition as pvm_lpar

from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm import mgmt

LPAR_HTTPRESP_FILE = "lpar.txt"


class TestMgmt(test.TestCase):
    def setUp(self):
        super(TestMgmt, self).setUp()
        self.pypvm = self.useFixture(fx.PyPowerVM())
        self.apt = self.pypvm.apt

        lpar_http = pvmhttp.load_pvm_resp(LPAR_HTTPRESP_FILE, adapter=self.apt)
        self.assertNotEqual(lpar_http, None,
                            "Could not load %s " % LPAR_HTTPRESP_FILE)

        self.resp = lpar_http.response

    def test_get_mgmt_partition(self):
        self.apt.read.return_value = self.resp
        mp_wrap = mgmt.get_mgmt_partition(self.apt)
        self.assertIsInstance(mp_wrap, pvm_lpar.LPAR)
        self.assertTrue(mp_wrap.is_mgmt_partition)

    @mock.patch('glob.glob')
    @mock.patch('nova.utils.execute')
    @mock.patch('os.path.realpath')
    def test_discover_vscsi_disk(self, mock_realpath, mock_exec, mock_glob):
        scanpath = '/sys/bus/vio/devices/30000005/host*/scsi_host/host*/scan'
        udid = ('275b5d5f88fa5611e48be9000098be9400'
                '13fb2aa55a2d7b8d150cb1b7b6bc04d6')
        devlink = ('/dev/disk/by-id/scsi-SIBM_3303_NVDISK' + udid)
        mapping = mock.Mock()
        mapping.client_adapter.slot_number = 5
        mapping.backing_storage.udid = udid
        # Realistically, first glob would return  e.g. .../host0/.../host0/...
        # but it doesn't matter for test purposes.
        mock_glob.side_effect = [[scanpath], [devlink]]
        mgmt.discover_vscsi_disk(mapping)
        mock_glob.assert_has_calls(
            [mock.call(scanpath), mock.call('/dev/disk/by-id/*' + udid[-32:])])
        mock_exec.assert_called_with('tee', '-a', scanpath,
                                     process_input='- - -', run_as_root=True)
        mock_realpath.assert_called_with(devlink)

    @mock.patch('glob.glob')
    @mock.patch('nova.utils.execute')
    def test_discover_vscsi_disk_not_one_result(self, mock_exec, mock_glob):
        """Zero or more than one disk is found by discover_vscsi_disk."""
        udid = ('275b5d5f88fa5611e48be9000098be9400'
                '13fb2aa55a2d7b8d150cb1b7b6bc04d6')
        mapping = mock.Mock()
        mapping.client_adapter.slot_number = 5
        mapping.backing_storage.udid = udid
        # No disks found
        mock_glob.side_effect = [['path'], []]
        self.assertRaises(mgmt.UniqueDiskDiscoveryException,
                          mgmt.discover_vscsi_disk, mapping)
        # Multiple disks found
        mock_glob.side_effect = [['path'], ['/dev/sde', '/dev/sdf']]
        self.assertRaises(mgmt.UniqueDiskDiscoveryException,
                          mgmt.discover_vscsi_disk, mapping)
