# Copyright 2014 IBM Corp.
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
#
# @author: Drew Thorstensen, IBM Corp.


from nova import test
from nova.virt import fake

from nova_powervm.virt.powervm import driver


class TestPowerVMDriver(test.TestCase):
    def setUp(self):
        super(TestPowerVMDriver, self).setUp()

    def test_driver_create(self):
        """Validates that a driver of the PowerVM type can just be
        initialized.
        """
        test_drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        self.assertIsNotNone(test_drv)

    def test_driver_init(self):
        """Validates the PowerVM driver can be initialized for the host."""
        drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        drv.init_host('FakeHost')
        # Nothing to really check here specific to the host.
        self.assertIsNotNone(drv)
