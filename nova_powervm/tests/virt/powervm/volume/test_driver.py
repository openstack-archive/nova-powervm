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
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova import test

from nova_powervm.virt.powervm import volume


class TestVolumeAdapter(test.TestCase):

    def setUp(self):
        super(TestVolumeAdapter, self).setUp()

        # Enable passing through the can attach/detach checks
        self.mock_get_inst_wrap_p = mock.patch('nova_powervm.virt.powervm.vm.'
                                               'get_instance_wrapper')
        self.mock_get_inst_wrap = self.mock_get_inst_wrap_p.start()
        self.addCleanup(self.mock_get_inst_wrap_p.stop)
        self.mock_inst_wrap = mock.MagicMock()
        self.mock_inst_wrap.can_modify_io.return_value = (True, None)
        self.mock_get_inst_wrap.return_value = self.mock_inst_wrap


class TestInitMethods(test.TestCase):

    @mock.patch('pypowervm.tasks.hdisk.discover_iscsi_initiator')
    @mock.patch('pypowervm.tasks.partition.get_mgmt_partition')
    def test_get_iscsi_initiator(self, mock_mgmt, mock_iscsi_init):
        # Set up mocks and clear out data that may have been set by other
        # tests
        mock_adpt = mock.Mock()
        mock_mgmt.return_value = mock.Mock(spec=pvm_vios.VIOS)
        mock_iscsi_init.return_value = 'test_initiator'

        self.assertEqual('test_initiator',
                         volume.get_iscsi_initiator(mock_adpt))

        # Make sure it gets set properly in the backend
        self.assertEqual('test_initiator', volume._ISCSI_INITIATOR)
        self.assertTrue(volume._ISCSI_LOOKUP_COMPLETE)
        mock_mgmt.assert_called_once_with(mock_adpt)
        self.assertEqual(1, mock_mgmt.call_count)

        # Invoke again, make sure it doesn't call down to the mgmt part again
        self.assertEqual('test_initiator',
                         volume.get_iscsi_initiator(mock_adpt))
        self.assertEqual(1, mock_mgmt.call_count)
