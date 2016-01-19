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

from nova_powervm.tests.virt import powervm
from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm.disk import driver as disk_dvr


class TestDiskAdapter(test.TestCase):
    """Unit Tests for the generic storage driver."""

    def setUp(self):
        super(TestDiskAdapter, self).setUp()
        self.useFixture(fx.ImageAPI())

        # These are not used currently.
        conn = {'adapter': None, 'host_uuid': None, 'mp_uuid': None}
        self.st_adpt = disk_dvr.DiskAdapter(conn)

    def test_capacity(self):
        """These are arbitrary capacity numbers."""
        self.assertEqual(2097152, self.st_adpt.capacity)
        self.assertEqual(0, self.st_adpt.capacity_used)

    def test_get_image_upload(self):
        # Test if there is an ID, that we get a file adapter back
        temp = self.st_adpt._get_image_upload(mock.Mock(), powervm.TEST_IMAGE1)
        self.assertIsInstance(temp, disk_dvr.IterableToFileAdapter)

    def test_get_info(self):
        # Ensure the base method returns empty dict
        self.assertEqual({}, self.st_adpt.get_info())

    def test_validate(self):
        # Ensure the base method returns error message
        self.assertIsNotNone(self.st_adpt.validate(None))
