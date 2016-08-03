# Copyright 2016 IBM Corp.
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

import fixtures
import mock
from nova import test
from pypowervm import exceptions as pvm_exc
import time

from nova_powervm.tests.virt import powervm
from nova_powervm.tests.virt.powervm.nvram import fake_api
from nova_powervm.virt.powervm.nvram import api
from nova_powervm.virt.powervm.nvram import manager
from nova_powervm.virt.powervm import vm


class TestNvramManager(test.TestCase):
    def setUp(self):
        super(TestNvramManager, self).setUp()
        self.fake_store = fake_api.NoopNvramStore()
        self.fake_exp_store = fake_api.ExpNvramStore()
        self.mock_store = self.useFixture(
            fixtures.MockPatchObject(self.fake_store, 'store')).mock
        self.mock_fetch = self.useFixture(
            fixtures.MockPatchObject(self.fake_store, 'fetch')).mock
        self.mock_remove = self.useFixture(
            fixtures.MockPatchObject(self.fake_store, 'delete')).mock

    @mock.patch('nova_powervm.virt.powervm.nvram.manager.LOG.warning')
    @mock.patch.object(vm, 'get_instance_wrapper')
    def test_store_with_exception(self, mock_get_inst, mock_log):
        mock_resp = mock.Mock()
        mock_resp.status = 410
        mock_resp.reqpath = (
            '/rest/api/uom/ManagedSystem/c5d782c7-44e4-3086-ad15-'
            'b16fb039d63b/LogicalPartition/1B5FB633-16D1-4E10-A14'
            '5-E6FB905161A3?group=None')
        mock_get_inst.side_effect = pvm_exc.HttpError(mock_resp)
        mgr = manager.NvramManager(self.fake_store, mock.Mock(), mock.Mock())
        mgr.store(powervm.TEST_INST1)
        mock_log.assert_called_once_with(u'Unable to store the NVRAM for '
                                         u'instance: %s',
                                         powervm.TEST_INST1.name)

    @mock.patch('nova_powervm.virt.powervm.nvram.manager.LOG.warning')
    @mock.patch.object(vm, 'get_instance_wrapper')
    def test_store_with_not_found_exc(self, mock_get_inst, mock_log):
        mock_resp = mock.Mock()
        mock_resp.status = 404
        mock_resp.reqpath = (
            '/rest/api/uom/ManagedSystem/c5d782c7-44e4-3086-ad15-'
            'b16fb039d63b/LogicalPartition/1B5FB633-16D1-4E10-A14'
            '5-E6FB905161A3?group=None')
        mock_get_inst.side_effect = pvm_exc.HttpError(mock_resp)
        mgr = manager.NvramManager(self.fake_store, mock.Mock(), mock.Mock())
        mgr.store(powervm.TEST_INST1)
        mock_log.assert_not_called()

    @mock.patch.object(vm, 'get_instance_wrapper')
    def test_manager(self, mock_get_inst):

        mgr = manager.NvramManager(self.fake_store, mock.Mock(), mock.Mock())
        mgr.store(powervm.TEST_INST1)
        mgr.store(powervm.TEST_INST2)

        mgr.fetch(powervm.TEST_INST2)

        mgr.remove(powervm.TEST_INST2)

        # Simulate a quick repeated stores of the same LPAR by poking the Q.
        mgr._queue.put(powervm.TEST_INST1)
        mgr._queue.put(powervm.TEST_INST1)
        mgr._queue.put(powervm.TEST_INST2)
        time.sleep(0)

        mgr.shutdown()
        self.mock_store.assert_has_calls(
            [mock.call(powervm.TEST_INST1, mock.ANY),
             mock.call(powervm.TEST_INST2, mock.ANY)])
        self.mock_fetch.assert_called_with(powervm.TEST_INST2)
        self.mock_remove.assert_called_with(powervm.TEST_INST2)

        # Test when fetch returns an exception
        mgr_exp = manager.NvramManager(self.fake_exp_store,
                                       mock.Mock(), mock.Mock())
        self.assertRaises(api.NVRAMDownloadException,
                          mgr_exp.fetch, powervm.TEST_INST2)

        # Test exception being logged but not raised during remove
        mgr_exp.remove(powervm.TEST_INST2)
        self.mock_remove.assert_called_with(powervm.TEST_INST2)
