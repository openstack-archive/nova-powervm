# Copyright 2015, 2017 IBM Corp.
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
from pypowervm import const as pvm_const

from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm.disk import driver as disk_dvr


class TestDiskAdapter(test.NoDBTestCase):
    """Unit Tests for the generic storage driver."""

    def setUp(self):
        super(TestDiskAdapter, self).setUp()
        self.useFixture(fx.ImageAPI())

        # Return the mgmt uuid
        self.mgmt_uuid = self.useFixture(fixtures.MockPatch(
            'nova_powervm.virt.powervm.mgmt.mgmt_uuid')).mock
        self.mgmt_uuid.return_value = 'mp_uuid'

        # The values (adapter and host uuid) are not used in the base.
        # Default them to None.
        self.st_adpt = disk_dvr.DiskAdapter(None, None)

    def test_capacity(self):
        """These are arbitrary capacity numbers."""
        self.assertEqual(2097152, self.st_adpt.capacity)
        self.assertEqual(0, self.st_adpt.capacity_used)

    def test_get_info(self):
        # Ensure the base method returns empty dict
        self.assertEqual({}, self.st_adpt.get_info())

    def test_validate(self):
        # Ensure the base method returns error message
        self.assertIsNotNone(self.st_adpt.validate(None))

    @mock.patch("pypowervm.util.sanitize_file_name_for_api")
    def test_get_disk_name(self, mock_san):
        inst = mock.Mock()
        inst.configure_mock(name='a_name_that_is_longer_than_eight',
                            uuid='01234567-abcd-abcd-abcd-123412341234')

        # Long
        self.assertEqual(mock_san.return_value,
                         self.st_adpt._get_disk_name('type', inst))
        mock_san.assert_called_with(inst.name, prefix='type_',
                                    max_len=pvm_const.MaxLen.FILENAME_DEFAULT)

        mock_san.reset_mock()

        # Short
        self.assertEqual(mock_san.return_value,
                         self.st_adpt._get_disk_name('type', inst, short=True))
        mock_san.assert_called_with('a_name_t_0123', prefix='t_',
                                    max_len=pvm_const.MaxLen.VDISK_NAME)

    @mock.patch("pypowervm.util.sanitize_file_name_for_api")
    def test_get_name_by_uuid(self, mock_san):
        uuid = '01234567-abcd-abcd-abcd-123412341234'

        # Long
        self.assertEqual(mock_san.return_value,
                         self.st_adpt.get_name_by_uuid('type', uuid))
        mock_san.assert_called_with(uuid, prefix='type_',
                                    max_len=pvm_const.MaxLen.FILENAME_DEFAULT)

        mock_san.reset_mock()

        # Short
        self.assertEqual(mock_san.return_value,
                         self.st_adpt.get_name_by_uuid('type', uuid,
                                                       short=True))
        mock_san.assert_called_with(uuid, prefix='t_',
                                    max_len=pvm_const.MaxLen.VDISK_NAME)
