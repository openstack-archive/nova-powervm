# Copyright 2016, 2018 IBM Corp.
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


class TestNvramManager(test.NoDBTestCase):
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
        self.mock_exp_remove = self.useFixture(
            fixtures.MockPatchObject(self.fake_exp_store, 'delete')).mock

    @mock.patch('nova_powervm.virt.powervm.nvram.manager.LOG.exception',
                autospec=True)
    @mock.patch.object(vm, 'get_instance_wrapper', autospec=True)
    def test_store_with_exception(self, mock_get_inst, mock_log):
        mock_get_inst.side_effect = pvm_exc.HttpError(mock.Mock())
        mgr = manager.NvramManager(self.fake_store, mock.Mock(), mock.Mock())
        mgr.store(powervm.TEST_INST1.uuid)
        self.assertEqual(1, mock_log.call_count)

    @mock.patch('nova_powervm.virt.powervm.nvram.manager.LOG.warning',
                autospec=True)
    @mock.patch.object(vm, 'get_instance_wrapper', autospec=True)
    def test_store_with_not_found_exc(self, mock_get_inst, mock_log):
        mock_get_inst.side_effect = pvm_exc.HttpNotFound(mock.Mock())
        mgr = manager.NvramManager(self.fake_store, mock.Mock(), mock.Mock())
        mgr.store(powervm.TEST_INST1.uuid)
        self.assertEqual(0, mock_log.call_count)

    @mock.patch.object(vm, 'get_instance_wrapper', autospec=True)
    def test_manager(self, mock_get_inst):

        mgr = manager.NvramManager(self.fake_store, mock.Mock(), mock.Mock())
        mgr.store(powervm.TEST_INST1.uuid)
        mgr.store(powervm.TEST_INST2)

        mgr.fetch(powervm.TEST_INST2)
        mgr.fetch(powervm.TEST_INST2.uuid)
        mgr.remove(powervm.TEST_INST2)

        # Simulate a quick repeated stores of the same LPAR by poking the Q.
        mgr._queue.put(powervm.TEST_INST1)
        mgr._queue.put(powervm.TEST_INST1)
        mgr._queue.put(powervm.TEST_INST2)
        time.sleep(0)

        mgr.shutdown()
        self.mock_store.assert_has_calls(
            [mock.call(powervm.TEST_INST1.uuid, mock.ANY),
             mock.call(powervm.TEST_INST2.uuid, mock.ANY)])
        self.mock_fetch.assert_has_calls(
            [mock.call(powervm.TEST_INST2.uuid)] * 2)
        self.mock_remove.assert_called_once_with(powervm.TEST_INST2.uuid)

        self.mock_remove.reset_mock()

        # Test when fetch returns an exception
        mgr_exp = manager.NvramManager(self.fake_exp_store,
                                       mock.Mock(), mock.Mock())
        self.assertRaises(api.NVRAMDownloadException,
                          mgr_exp.fetch, powervm.TEST_INST2)

        # Test exception being logged but not raised during remove
        mgr_exp.remove(powervm.TEST_INST2.uuid)
        self.mock_exp_remove.assert_called_once_with(powervm.TEST_INST2.uuid)
