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
import retrying

from nova import exception
from nova import test
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.tests.test_utils import pvmhttp

from nova_powervm.virt.powervm import exception as npvmex
from nova_powervm.virt.powervm import mgmt

LPAR_HTTPRESP_FILE = "lpar.txt"


class TestMgmt(test.TestCase):
    def setUp(self):
        super(TestMgmt, self).setUp()
        self.apt = self.useFixture(pvm_fx.AdapterFx()).adpt

        lpar_http = pvmhttp.load_pvm_resp(LPAR_HTTPRESP_FILE, adapter=self.apt)
        self.assertNotEqual(lpar_http, None,
                            "Could not load %s " % LPAR_HTTPRESP_FILE)

        self.resp = lpar_http.response

    @mock.patch('glob.glob')
    @mock.patch('nova.utils.execute')
    @mock.patch('os.path.realpath')
    def test_discover_vscsi_disk(self, mock_realpath, mock_exec, mock_glob):
        scanpath = '/sys/bus/vio/devices/30000005/host*/scsi_host/host*/scan'
        udid = ('275b5d5f88fa5611e48be9000098be9400'
                '13fb2aa55a2d7b8d150cb1b7b6bc04d6')
        devlink = ('/dev/disk/by-id/scsi-SIBM_3303_NVDISK' + udid)
        mapping = mock.Mock()
        mapping.client_adapter.lpar_slot_num = 5
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

    @mock.patch('retrying.retry')
    @mock.patch('glob.glob')
    @mock.patch('nova.utils.execute')
    def test_discover_vscsi_disk_not_one_result(self, mock_exec, mock_glob,
                                                mock_retry):
        """Zero or more than one disk is found by discover_vscsi_disk."""
        def validate_retry(kwargs):
            self.assertIn('retry_on_result', kwargs)
            self.assertEqual(250, kwargs['wait_fixed'])
            self.assertEqual(300000, kwargs['stop_max_delay'])

        def raiser(unused):
            raise retrying.RetryError(mock.Mock(attempt_number=123))

        def retry_passthrough(**kwargs):
            validate_retry(kwargs)

            def wrapped(_poll_for_dev):
                return _poll_for_dev
            return wrapped

        def retry_timeout(**kwargs):
            validate_retry(kwargs)

            def wrapped(_poll_for_dev):
                return raiser
            return wrapped

        udid = ('275b5d5f88fa5611e48be9000098be9400'
                '13fb2aa55a2d7b8d150cb1b7b6bc04d6')
        mapping = mock.Mock()
        mapping.client_adapter.lpar_slot_num = 5
        mapping.backing_storage.udid = udid
        # No disks found
        mock_retry.side_effect = retry_timeout
        mock_glob.side_effect = lambda path: []
        self.assertRaises(npvmex.NoDiskDiscoveryException,
                          mgmt.discover_vscsi_disk, mapping)
        # Multiple disks found
        mock_retry.side_effect = retry_passthrough
        mock_glob.side_effect = [['path'], ['/dev/sde', '/dev/sdf']]
        self.assertRaises(npvmex.UniqueDiskDiscoveryException,
                          mgmt.discover_vscsi_disk, mapping)

    @mock.patch('time.sleep')
    @mock.patch('os.path.realpath')
    @mock.patch('os.stat')
    @mock.patch('nova.utils.execute')
    def test_remove_block_dev(self, mock_exec, mock_stat, mock_realpath,
                              mock_sleep):
        link = '/dev/link/foo'
        realpath = '/dev/sde'
        delpath = '/sys/block/sde/device/delete'
        mock_realpath.return_value = realpath

        # Good path
        mock_stat.side_effect = (None, None, OSError())
        mgmt.remove_block_dev(link)
        mock_realpath.assert_called_with(link)
        mock_stat.assert_has_calls([mock.call(realpath), mock.call(delpath),
                                    mock.call(realpath)])
        mock_exec.assert_called_with('tee', '-a', delpath, process_input='1',
                                     run_as_root=True)
        self.assertEqual(0, mock_sleep.call_count)

        # Device param not found
        mock_exec.reset_mock()
        mock_stat.reset_mock()
        mock_stat.side_effect = (OSError(), None, None)
        self.assertRaises(exception.InvalidDevicePath, mgmt.remove_block_dev,
                          link)
        # stat was called once; exec was not called
        self.assertEqual(1, mock_stat.call_count)
        self.assertEqual(0, mock_exec.call_count)

        # Delete special file not found
        mock_exec.reset_mock()
        mock_stat.reset_mock()
        mock_stat.side_effect = (None, OSError(), None)
        self.assertRaises(exception.InvalidDevicePath, mgmt.remove_block_dev,
                          link)
        # stat was called twice; exec was not called
        self.assertEqual(2, mock_stat.call_count)
        self.assertEqual(0, mock_exec.call_count)

        # Deletion was attempted, but device is still there
        mock_exec.reset_mock()
        mock_stat.reset_mock()
        mock_sleep.reset_mock()
        mock_stat.side_effect = lambda path: 1
        self.assertRaises(
            npvmex.DeviceDeletionException, mgmt.remove_block_dev, link)
        # stat was called many times; exec was called once
        self.assertTrue(mock_stat.call_count > 4)
        self.assertEqual(1, mock_exec.call_count)
        # sleep was called many times
        self.assertTrue(mock_sleep.call_count)
