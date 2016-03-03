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
from swiftclient import service as swft_srv

from nova_powervm.tests.virt import powervm
from nova_powervm.virt.powervm.nvram import api
from nova_powervm.virt.powervm.nvram import swift


class TestSwiftStore(test.TestCase):
    def setUp(self):
        super(TestSwiftStore, self).setUp()
        self.flags(swift_password='secret', swift_auth_url='url',
                   group='powervm')
        self.swift_store = swift.SwiftNvramStore()

        self.swift_srv = self.useFixture(
            fixtures.MockPatch('swiftclient.service.SwiftService')).mock

    def test_run_operation(self):

        fake_result = [{'key1': 'value1'}, {'2key1', '2value1'}]
        fake_result2 = fake_result[0]

        def fake_generator(alist):
            for item in alist:
                yield item

        # Address the 'list' method that should be called.
        list_op = (self.swift_srv.return_value.__enter__.
                   return_value.list)

        # Setup expected results
        list_op.return_value = fake_generator(fake_result)
        results = self.swift_store._run_operation(None, 'list', 1, x=2)

        self.swift_srv.assert_called_once_with(
            options=self.swift_store.options)

        list_op.assert_called_once_with(1, x=2)
        # Returns a copy of the results
        self.assertEqual(results, fake_result)
        self.assertNotEqual(id(results), id(fake_result))

        # Try a single result - Setup expected results
        list_op.reset_mock()
        list_op.return_value = fake_result2
        results = self.swift_store._run_operation(None, 'list', 3, x=4)

        list_op.assert_called_once_with(3, x=4)
        # Returns the actual result
        self.assertEqual(results, fake_result2)
        self.assertEqual(id(results), id(fake_result2))

        # Should raise any swift errors encountered
        list_op.side_effect = swft_srv.SwiftError('Error message.')
        self.assertRaises(swft_srv.SwiftError, self.swift_store._run_operation,
                          None, 'list', 3, x=4)

    def _build_results(self, names):
        listing = [{'name': name} for name in names]
        return [{'success': True, 'listing': listing}]

    def test_get_name_from_listing(self):
        names = self.swift_store._get_name_from_listing(
            self._build_results(['snoopy']))
        self.assertEqual(['snoopy'], names)

    def test_get_container_names(self):
        with mock.patch.object(self.swift_store, '_run_operation') as mock_run:
            mock_run.return_value = self._build_results(['container'])
            names = self.swift_store._get_container_names()
            self.assertEqual(['container'], names)
            mock_run.assert_called_once_with(None, 'list',
                                             options={'long': True})

    def test_get_object_names(self):
        with mock.patch.object(self.swift_store, '_run_operation') as mock_run:
            mock_run.return_value = self._build_results(['obj', 'obj2'])
            names = self.swift_store._get_object_names('powervm_nvram')
            self.assertEqual(['obj', 'obj2'], names)
            mock_run.assert_called_once_with(None, 'list',
                                             container='powervm_nvram',
                                             options={'long': True})

    def test_underscore_store(self):
        with mock.patch.object(self.swift_store, '_run_operation') as mock_run:
            mock_run.return_value = self._build_results(['obj'])
            self.swift_store._store(powervm.TEST_INST1, 'data')
            mock_run.assert_called_once_with(None, 'upload', 'powervm_nvram',
                                             mock.ANY)

            # Test unsuccessful upload
            mock_run.return_value[0]['success'] = False
            self.assertRaises(api.NVRAMUploadException,
                              self.swift_store._store, powervm.TEST_INST1,
                              'data')

    def test_store(self):
        # Test forcing a update
        with mock.patch.object(self.swift_store, '_store') as mock_store:
            self.swift_store.store(powervm.TEST_INST1, 'data', force=True)
            mock_store.assert_called_once_with(powervm.TEST_INST1, 'data')

        with mock.patch.object(
            self.swift_store, '_store') as mock_store, mock.patch.object(
            self.swift_store, '_run_operation') as mock_run:

            data_md5_hash = '8d777f385d3dfec8815d20f7496026dc'
            results = self._build_results(['obj'])
            results[0]['headers'] = {'etag': data_md5_hash}
            mock_run.return_value = results
            self.swift_store.store(powervm.TEST_INST1, 'data', force=False)
            self.assertFalse(mock_store.called)
            mock_run.assert_called_once_with(
                None, 'stat', options={'long': True},
                container='powervm_nvram', objects=[powervm.TEST_INST1.uuid])

    @mock.patch('os.remove')
    @mock.patch('tempfile.NamedTemporaryFile')
    def test_fetch(self, mock_tmpf, mock_rmv):
        with mock.patch('nova_powervm.virt.powervm.nvram.swift.open',
                        mock.mock_open(read_data='data to read')
                        ) as m_open, mock.patch.object(
                self.swift_store, '_run_operation') as mock_run:
            mock_run.return_value = self._build_results(['obj'])
            mock_tmpf.return_value.__enter__.return_value.name = 'fname'

            data = self.swift_store.fetch(powervm.TEST_INST1)
            self.assertEqual('data to read', data)
            mock_rmv.assert_called_once_with(m_open.return_value.name)

            # Bad result from the download
            mock_run.return_value[0]['success'] = False
            self.assertRaises(api.NVRAMDownloadException,
                              self.swift_store.fetch, powervm.TEST_INST1)

    def test_delete(self):
        with mock.patch.object(self.swift_store, '_run_operation') as mock_run:
            mock_run.return_value = self._build_results(['obj'])
            self.swift_store.delete(powervm.TEST_INST1)
            mock_run.assert_called_once_with(None, 'delete',
                                             container='powervm_nvram',
                                             objects=[powervm.TEST_INST1.uuid])

            # Bad result from the operation
            mock_run.return_value[0]['success'] = False
            self.assertRaises(api.NVRAMDeleteException,
                              self.swift_store.delete, powervm.TEST_INST1)
