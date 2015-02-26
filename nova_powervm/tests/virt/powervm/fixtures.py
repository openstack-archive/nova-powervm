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
#

from __future__ import absolute_import

import fixtures
import mock

from nova_powervm.virt.powervm import driver

from nova.virt import fake


class PyPowerVM(fixtures.Fixture):
    """Patch out PyPowerVM Session and Adapter."""

    def __init__(self):
        pass

    def setUp(self):
        super(PyPowerVM, self).setUp()
        self._sess_patcher = mock.patch('pypowervm.adapter.Session')
        self._apt_patcher = mock.patch('pypowervm.adapter.Adapter')
        self.sess = self._sess_patcher.start()
        self.apt = self._apt_patcher.start()

        self.addCleanup(self._sess_patcher.stop)
        self.addCleanup(self._apt_patcher.stop)


class ImageAPI(fixtures.Fixture):
    """Mock out the Glance API."""

    def __init__(self):
        pass

    def setUp(self):
        super(ImageAPI, self).setUp()
        self._img_api_patcher = mock.patch('nova.image.API')
        self.img_api = self._img_api_patcher.start()

        self.addCleanup(self.img_api)


class PowerVMComputeDriver(fixtures.Fixture):
    """Construct a fake compute driver."""

    def __init__(self):
        pass

    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage')
    @mock.patch('pypowervm.wrappers.managed_system.find_entry_by_mtms')
    def _init_host(self, *args):
        self.drv.init_host('FakeHost')

    def setUp(self):
        super(PowerVMComputeDriver, self).setUp()

        self.pypvm = PyPowerVM()
        self.pypvm.setUp()
        self.addCleanup(self.pypvm.cleanUp)

        self.drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        self._init_host()
        self.drv.adapter = self.pypvm.apt
        self.drv.image_api = mock.Mock()
