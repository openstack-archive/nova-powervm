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
import os

from nova_powervm.virt.powervm import driver

from nova.virt import fake
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.tests.wrappers.util import pvmhttp

MS_HTTPRESP_FILE = "fake_managedsystem.txt"

FAKE_INST_UUID = 'b6513403-fd7f-4ad0-ab27-f73bacbd3929'
FAKE_INST_UUID_PVM = '36513403-FD7F-4AD0-AB27-F73BACBD3929'


class ImageAPI(fixtures.Fixture):
    """Mock out the Glance API."""

    def setUp(self):
        super(ImageAPI, self).setUp()
        self._img_api_patcher = mock.patch('nova.image.API')
        self.img_api = self._img_api_patcher.start()

        self.addCleanup(self.img_api)


class DiskAdapter(fixtures.Fixture):
    """Mock out the DiskAdapter."""

    def setUp(self):
        super(DiskAdapter, self).setUp()
        self._std_disk_adpt = mock.patch('nova_powervm.virt.powervm.disk.'
                                         'localdisk.LocalStorage')
        self.std_disk_adpt = self._std_disk_adpt.start()
        self.addCleanup(self._std_disk_adpt.stop)


class HostCPUStats(fixtures.Fixture):
    """Mock out the HostCPUStats."""

    def setUp(self):
        super(HostCPUStats, self).setUp()
        self._host_cpu_stats = mock.patch('nova_powervm.virt.powervm.host.'
                                          'HostCPUStats')
        self.host_cpu_stats = self._host_cpu_stats.start()
        self.addCleanup(self._host_cpu_stats.stop)


class VolumeAdapter(fixtures.Fixture):
    """Mock out the VolumeAdapter."""

    def setUp(self):
        super(VolumeAdapter, self).setUp()
        self._std_vol_adpt = mock.patch('nova_powervm.virt.powervm.volume.'
                                        'vscsi.VscsiVolumeAdapter',
                                        __name__='MockVSCSI')
        self.std_vol_adpt = self._std_vol_adpt.start()
        # We want to mock out the connection_info individually so it gives
        # back a new mock on every call.  That's because the vol id is
        # used for task names and we can't have duplicates.  Here we have
        # just one mock for simplicity of the vol driver but we need
        # mulitiple names.
        self.std_vol_adpt.return_value.connection_info.__getitem__\
            .side_effect = mock.MagicMock
        self.drv = self.std_vol_adpt.return_value
        self.addCleanup(self._std_vol_adpt.stop)


class PowerVMComputeDriver(fixtures.Fixture):
    """Construct a fake compute driver."""

    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage')
    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver._get_adapter')
    @mock.patch('nova_powervm.virt.powervm.mgmt.get_mgmt_partition')
    def _init_host(self, *args):
        file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'data', MS_HTTPRESP_FILE)
        ms_http = pvmhttp.load_pvm_resp(
            file_path, adapter=mock.Mock()).get_response()
        # Pretend it just returned one host
        ms_http.feed.entries = [ms_http.feed.entries[0]]
        self.drv.adapter.read.return_value = ms_http
        self.drv.init_host('FakeHost')

    def setUp(self):
        super(PowerVMComputeDriver, self).setUp()

        # Set up the mock CPU stats (init_host uses it)
        self.useFixture(HostCPUStats())

        self.drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        self.drv.adapter = self.useFixture(pvm_fx.AdapterFx()).adpt
        self._init_host()
        self.drv.image_api = mock.Mock()

        disk_adpt = self.useFixture(DiskAdapter())
        self.drv.disk_dvr = disk_adpt.std_disk_adpt
