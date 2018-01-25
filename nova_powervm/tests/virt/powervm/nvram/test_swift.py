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

import mock
from nova import test
from requests.exceptions import RequestException
from swiftclient import exceptions as swft_exc
from swiftclient import service as swft_srv

from nova_powervm.tests.virt import powervm
from nova_powervm.virt.powervm.nvram import api
from nova_powervm.virt.powervm.nvram import swift


class TestSwiftStore(test.NoDBTestCase):

    def setUp(self):
        super(TestSwiftStore, self).setUp()
        self.flags(swift_password='secret', swift_auth_url='url',
                   group='powervm')

        self.swift_store = swift.SwiftNvramStore()

    def test_run_operation(self):

        fake_result = [{'key1': 'value1'}, {'2key1', '2value1'}]
        fake_result2 = fake_result[0]

        def fake_generator(alist):
            for item in alist:
                yield item

        # Address the 'list' method that should be called.
        list_op = mock.Mock()
        self.swift_store.swift_service = mock.Mock(list=list_op)

        # Setup expected results
        list_op.return_value = fake_generator(fake_result)
        results = self.swift_store._run_operation('list', 1, x=2)

        list_op.assert_called_once_with(1, x=2)
        # Returns a copy of the results
        self.assertEqual(results, fake_result)
        self.assertNotEqual(id(results), id(fake_result))

        # Try a single result - Setup expected results
        list_op.reset_mock()
        list_op.return_value = fake_result2
        results = self.swift_store._run_operation('list', 3, x=4)

        list_op.assert_called_once_with(3, x=4)
        # Returns the actual result
        self.assertEqual(results, fake_result2)
        self.assertEqual(id(results), id(fake_result2))

        # Should raise any swift errors encountered
        list_op.side_effect = swft_srv.SwiftError('Error message.')
        self.assertRaises(swft_srv.SwiftError, self.swift_store._run_operation,
                          'list', 3, x=4)

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
            mock_run.assert_called_once_with('list',
                                             options={'long': True})

    @mock.patch('nova_powervm.virt.powervm.nvram.swift.SwiftNvramStore.'
                '_get_container_names', autospec=True)
    def test_get_object_names(self, mock_container_names):
        with mock.patch.object(self.swift_store, '_run_operation') as mock_run:
            mock_run.return_value = self._build_results(['obj', 'obj2'])

            # First run, no containers.
            mock_container_names.return_value = []
            names = self.swift_store._get_object_names('powervm_nvram')
            self.assertEqual([], names)
            self.assertEqual(1, mock_container_names.call_count)

            # Test without a prefix
            mock_container_names.return_value = ['powervm_nvram']
            names = self.swift_store._get_object_names('powervm_nvram')
            self.assertEqual(['obj', 'obj2'], names)
            mock_run.assert_called_once_with(
                'list', container='powervm_nvram',
                options={'long': True, 'prefix': None})
            self.assertEqual(mock_container_names.call_count, 2)

            # Test with a prefix
            names = self.swift_store._get_object_names('powervm_nvram',
                                                       prefix='obj')
            self.assertEqual(['obj', 'obj2'], names)
            mock_run.assert_called_with(
                'list', container='powervm_nvram',
                options={'long': True, 'prefix': 'obj'})

            # Second run should not increment the call count here
            self.assertEqual(mock_container_names.call_count, 2)

    @mock.patch('swiftclient.service.SwiftUploadObject', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.nvram.swift.SwiftNvramStore.'
                '_exists', autospec=True)
    def test_underscore_store(self, mock_exists, mock_swiftuploadobj):
        mock_exists.return_value = True
        with mock.patch.object(self.swift_store, '_run_operation') as mock_run:
            mock_run.return_value = self._build_results(['obj'])
            self.swift_store._store(powervm.TEST_INST1.uuid, 'data')
            mock_run.assert_called_once_with('upload', 'powervm_nvram',
                                             mock.ANY, options=None)

            # Test unsuccessful upload
            mock_result = [{'success': False,
                            'error': RequestException('Error Message.')}]
            mock_run.return_value = mock_result
            self.assertRaises(api.NVRAMUploadException,
                              self.swift_store._store, powervm.TEST_INST1.uuid,
                              'data')

            # Test retry upload
            mock_run.reset_mock()
            mock_swiftuploadobj.reset_mock()
            mock_res_obj = {'success': False,
                            'error': swft_exc.
                            ClientException('Error Message.'),
                            'object': '6ecb1386-53ab-43da-9e04-54e986ad4a9d'}
            mock_run.side_effect = [mock_res_obj,
                                    self._build_results(['obj'])]
            self.swift_store._store(powervm.TEST_INST1.uuid, 'data')
            mock_run.assert_called_with('upload', 'powervm_nvram',
                                        mock.ANY, options=None)
            self.assertEqual(mock_run.call_count, 2)
            self.assertEqual(mock_swiftuploadobj.call_count, 2)

    @mock.patch('swiftclient.service.SwiftUploadObject', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.nvram.swift.SwiftNvramStore.'
                '_exists', autospec=True)
    def test_underscore_store_not_exists(self, mock_exists,
                                         mock_swiftuploadobj):
        mock_exists.return_value = False
        with mock.patch.object(self.swift_store, '_run_operation') as mock_run:
            mock_run.return_value = self._build_results(['obj'])
            self.swift_store._store(powervm.TEST_INST1.uuid, 'data')
            mock_run.assert_called_once_with(
                'upload', 'powervm_nvram', mock.ANY,
                options={'leave_segments': True})

            # Test retry upload
            mock_run.reset_mock()
            mock_swiftuploadobj.reset_mock()
            mock_res_obj = {'success': False,
                            'error': swft_exc.
                            ClientException('Error Message.'),
                            'object': '6ecb1386-53ab-43da-9e04-54e986ad4a9d'}
            mock_run.side_effect = [mock_res_obj,
                                    self._build_results(['obj'])]
            self.swift_store._store(powervm.TEST_INST1.uuid, 'data')
            mock_run.assert_called_with('upload', 'powervm_nvram', mock.ANY,
                                        options={'leave_segments': True})
            self.assertEqual(mock_run.call_count, 2)
            self.assertEqual(mock_swiftuploadobj.call_count, 2)

    @mock.patch('nova_powervm.virt.powervm.nvram.swift.SwiftNvramStore.'
                '_exists', autospec=True)
    def test_store(self, mock_exists):
        # Test forcing a update
        with mock.patch.object(self.swift_store, '_store') as mock_store:
            mock_exists.return_value = False
            self.swift_store.store(powervm.TEST_INST1.uuid, 'data', force=True)
            mock_store.assert_called_once_with(powervm.TEST_INST1.uuid,
                                               'data', exists=False)

        with mock.patch.object(
            self.swift_store, '_store') as mock_store, mock.patch.object(
                self.swift_store, '_run_operation') as mock_run:

            mock_exists.return_value = True
            data_md5_hash = '8d777f385d3dfec8815d20f7496026dc'
            results = self._build_results(['obj'])
            results[0]['headers'] = {'etag': data_md5_hash}
            mock_run.return_value = results
            self.swift_store.store(powervm.TEST_INST1.uuid, 'data',
                                   force=False)
            self.assertFalse(mock_store.called)
            mock_run.assert_called_once_with(
                'stat', options={'long': True},
                container='powervm_nvram', objects=[powervm.TEST_INST1.uuid])

    def test_store_slot_map(self):
        # Test forcing a update
        with mock.patch.object(self.swift_store, '_store') as mock_store:
            self.swift_store.store_slot_map("test_slot", 'data')
            mock_store.assert_called_once_with(
                'test_slot', 'data')

    @mock.patch('os.remove', autospec=True)
    @mock.patch('tempfile.NamedTemporaryFile', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.nvram.swift.SwiftNvramStore.'
                '_exists', autospec=True)
    def test_fetch(self, mock_exists, mock_tmpf, mock_rmv):
        mock_exists.return_value = True
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

    @mock.patch('os.remove', autospec=True)
    @mock.patch('tempfile.NamedTemporaryFile', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.nvram.swift.SwiftNvramStore.'
                '_exists', autospec=True)
    def test_fetch_slot_map(self, mock_exists, mock_tmpf, mock_rmv):
        mock_exists.return_value = True
        with mock.patch('nova_powervm.virt.powervm.nvram.swift.open',
                        mock.mock_open(read_data='data to read')
                        ) as m_open, mock.patch.object(
                self.swift_store, '_run_operation') as mock_run:
            mock_run.return_value = self._build_results(['obj'])
            mock_tmpf.return_value.__enter__.return_value.name = 'fname'

            data = self.swift_store.fetch_slot_map("test_slot")
            self.assertEqual('data to read', data)
            mock_rmv.assert_called_once_with(m_open.return_value.name)

    @mock.patch('os.remove', autospec=True)
    @mock.patch('tempfile.NamedTemporaryFile', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.nvram.swift.SwiftNvramStore.'
                '_exists', autospec=True)
    def test_fetch_slot_map_no_exist(self, mock_exists, mock_tmpf, mock_rmv):
        mock_exists.return_value = False
        data = self.swift_store.fetch_slot_map("test_slot")
        self.assertIsNone(data)

        # Make sure the remove (part of the finally block) is never called.
        # Should not get that far.
        self.assertFalse(mock_rmv.called)

    def test_delete(self):
        with mock.patch.object(self.swift_store, '_run_operation') as mock_run:
            mock_run.return_value = self._build_results(['obj'])
            self.swift_store.delete(powervm.TEST_INST1)
            mock_run.assert_called_once_with('delete',
                                             container='powervm_nvram',
                                             objects=[powervm.TEST_INST1.uuid])

            # Bad result from the operation
            mock_run.return_value[0]['success'] = False
            self.assertRaises(api.NVRAMDeleteException,
                              self.swift_store.delete, powervm.TEST_INST1)

    def test_delete_slot_map(self):
        with mock.patch.object(self.swift_store, '_run_operation') as mock_run:
            mock_run.return_value = self._build_results(['obj'])
            self.swift_store.delete_slot_map('test_slot')
            mock_run.assert_called_once_with(
                'delete', container='powervm_nvram', objects=['test_slot'])

            # Bad result from the operation
            mock_run.return_value[0]['success'] = False
            self.assertRaises(
                api.NVRAMDeleteException, self.swift_store.delete_slot_map,
                'test_slot')

    @mock.patch('nova_powervm.virt.powervm.nvram.swift.SwiftNvramStore.'
                '_get_object_names', autospec=True)
    def test_exists(self, mock_get_obj_names):
        # Test where there are elements in here
        mock_get_obj_names.return_value = ['obj', 'obj1', 'obj2']
        self.assertTrue(self.swift_store._exists('obj'))

        # Test where there are objects that start with the prefix, but aren't
        # actually there themselves
        mock_get_obj_names.return_value = ['obj1', 'obj2']
        self.assertFalse(self.swift_store._exists('obj'))

    def test_optional_options(self):
        """Test optional config values."""
        # Not in the sparse one from setUp()
        self.assertIsNone(self.swift_store.options['os_cacert'])
        self.assertIsNone(self.swift_store.options['os_endpoint_type'])
        # Create a new one with the optional values set
        self.flags(swift_cacert='/path/to/ca.pem', group='powervm')
        self.flags(swift_endpoint_type='internalURL', group='powervm')
        swift_store = swift.SwiftNvramStore()
        self.assertEqual('/path/to/ca.pem', swift_store.options['os_cacert'])
        self.assertEqual('internalURL',
                         swift_store.options['os_endpoint_type'])
