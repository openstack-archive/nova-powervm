# Copyright 2015, 2016 IBM Corp.
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
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.tests.test_utils import pvmhttp

MS_HTTPRESP_FILE = "fake_managedsystem.txt"

FAKE_INST_UUID = 'b6513403-fd7f-4ad0-ab27-f73bacbd3929'
FAKE_INST_UUID_PVM = '36513403-FD7F-4AD0-AB27-F73BACBD3929'


class ImageAPI(fixtures.Fixture):
    """Mock out the Glance API."""

    def setUp(self):
        super(ImageAPI, self).setUp()
        self.img_api_fx = self.useFixture(fixtures.MockPatch('nova.image.API'))


class DiskAdapter(fixtures.Fixture):
    """Mock out the DiskAdapter."""

    def setUp(self):
        super(DiskAdapter, self).setUp()
        self.std_disk_adpt_fx = self.useFixture(
            fixtures.MockPatch('nova_powervm.virt.powervm.disk.localdisk.'
                               'LocalStorage'))
        self.std_disk_adpt = self.std_disk_adpt_fx.mock


class HostCPUStats(fixtures.Fixture):
    """Mock out the HostCPUStats."""

    def setUp(self):
        super(HostCPUStats, self).setUp()
        self.host_cpu_stats = self.useFixture(
            fixtures.MockPatch('nova_powervm.virt.powervm.host.HostCPUStats'))


class VolumeAdapter(fixtures.Fixture):
    """Mock out the VolumeAdapter."""

    def setUp(self):
        super(VolumeAdapter, self).setUp()
        self.std_vol_adpt_fx = self.useFixture(
            fixtures.MockPatch('nova_powervm.virt.powervm.volume.vscsi.'
                               'VscsiVolumeAdapter', __name__='MockVSCSI'))
        self.std_vol_adpt = self.std_vol_adpt_fx.mock
        # We want to mock out the connection_info individually so it gives
        # back a new mock on every call.  That's because the vol id is
        # used for task names and we can't have duplicates.  Here we have
        # just one mock for simplicity of the vol driver but we need
        # mulitiple names.
        self.std_vol_adpt.return_value.connection_info.__getitem__\
            .side_effect = mock.MagicMock
        self.drv = self.std_vol_adpt.return_value


class PowerVMComputeDriver(fixtures.Fixture):
    """Construct a fake compute driver."""

    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage')
    @mock.patch('nova_powervm.virt.powervm.driver.PowerVMDriver._get_adapter')
    @mock.patch('pypowervm.tasks.partition.get_this_partition')
    def _init_host(self, *args):
        ms_http = pvmhttp.load_pvm_resp(
            MS_HTTPRESP_FILE, adapter=mock.Mock()).get_response()
        # Pretend it just returned one host
        ms_http.feed.entries = [ms_http.feed.entries[0]]

        # Mock active vios
        self.get_active_vios = self.useFixture(fixtures.MockPatch(
            'nova_powervm.virt.powervm.vios.get_active_vioses')).mock
        self.get_active_vios.return_value = ['mock_vios']

        self.drv.adapter.read.return_value = ms_http
        self.drv.session = self.drv.adapter.session
        self.drv.init_host('FakeHost')

    def setUp(self):
        super(PowerVMComputeDriver, self).setUp()

        # Set up the mock CPU stats (init_host uses it)
        self.useFixture(HostCPUStats())

        self.drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        self.drv.adapter = self.useFixture(pvm_fx.AdapterFx()).adpt
        self._init_host()
        self.drv.image_api = mock.Mock()

        disk_adpt_fx = self.useFixture(DiskAdapter())
        self.drv.disk_dvr = disk_adpt_fx.std_disk_adpt


class TaskFlow(fixtures.Fixture):
    """Construct a fake TaskFlow.

    This fixture makes it easy to check if tasks were added to a task flow
    without having to mock each task.
    """

    def __init__(self, linear_flow='taskflow.patterns.linear_flow',
                 engines='taskflow.engines'):
        """Create the fixture.

        :param linear_flow: The import path to patch for the linear flow.
        :param engines: The import path to patch for the engines.
        """
        super(TaskFlow, self).__init__()
        self.linear_flow_import = linear_flow
        self.engines_import = engines

    def setUp(self):
        super(TaskFlow, self).setUp()
        self.tasks_added = []
        self.lf_fix = self.useFixture(
            fixtures.MockPatch(self.linear_flow_import))
        self.lf_fix.mock.Flow.return_value.add.side_effect = self._record_tasks

        self.engine_fx = self.useFixture(
            fixtures.MockPatch(self.engines_import))

    def _record_tasks(self, *args, **kwargs):
        self.tasks_added.append(args[0])

    def assert_tasks_added(self, testcase, expected_tasks):
        # Ensure the lists are the same size.
        testcase.assertEqual(len(expected_tasks), len(self.tasks_added),
                             'Expected tasks not added: %s, %s' %
                             (expected_tasks,
                              [t.name for t in self.tasks_added]))

        def compare_tasks(expected, observed):
            if expected.endswith('*'):
                cmplen = len(expected[:-1])
                testcase.assertEqual(expected[:cmplen], observed.name[:cmplen])
            else:
                testcase.assertEqual(expected, observed.name)

        # Compare the list of expected against added.
        for func in map(compare_tasks, expected_tasks, self.tasks_added):
            # Call the function to do the compare
            func


class DriverTaskFlow(TaskFlow):
    """Specific TaskFlow fixture for the main compute driver."""
    def __init__(self):
        super(DriverTaskFlow, self).__init__(
            linear_flow='nova_powervm.virt.powervm.driver.tf_lf',
            engines='nova_powervm.virt.powervm.driver.tf_eng')
