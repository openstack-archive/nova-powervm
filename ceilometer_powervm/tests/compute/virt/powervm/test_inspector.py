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

from ceilometer.compute.virt import inspector as virt_inspector
from oslotest import base
from pypowervm.tests import test_fixtures as api_fx

from ceilometer_powervm.compute.virt.powervm import inspector as p_inspect
from ceilometer_powervm.tests.compute.virt.powervm import pvm_fixtures


class TestPowerVMInspector(base.BaseTestCase):

    def setUp(self):
        super(TestPowerVMInspector, self).setUp()

        # These fixtures allow for stand up of the unit tests that use
        # pypowervm.
        self.useFixture(api_fx.AdapterFx())
        pvm_mon_fx = self.useFixture(pvm_fixtures.PyPowerVMMetrics())

        # Individual test cases will set return values on the metrics that
        # come back from pypowervm.
        self.mock_metrics = pvm_mon_fx.vm_metrics

        with mock.patch('ceilometer_powervm.compute.virt.powervm.inspector.'
                        'PowerVMInspector._get_host_uuid'):
            # Create the inspector
            self.inspector = p_inspect.PowerVMInspector()
            self.inspector.vm_metrics = self.mock_metrics

    def test_inspect_cpus(self):
        """Validates PowerVM's inspect_cpus method."""
        # Validate that an error is raised if the instance can't be found
        # in the sample
        self.mock_metrics.get_latest_metric.return_value = None, None
        self.assertRaises(virt_inspector.InstanceNotFoundException,
                          self.inspector.inspect_cpus, mock.Mock())

        # Build a response from the metric cache.
        metric = mock.MagicMock()
        metric.processor.util_cap_proc_cycles = 5
        metric.processor.util_uncap_proc_cycles = 6
        metric.processor.virt_procs = 12
        self.mock_metrics.get_latest_metric.return_value = mock.Mock(), metric

        # Invoke with the test data
        resp = self.inspector.inspect_cpus(mock.Mock())

        # Validate the metrics that came back
        self.assertEqual(11, resp.time)
        self.assertEqual(12, resp.number)

    def test_inspect_cpu_util(self):
        """Validates PowerVM's inspect_cpu_util method."""
        # Validate that an error is raised if the instance can't be found in
        # the sample data.
        self.mock_metrics.get_latest_metric.return_value = None, None
        self.mock_metrics.get_previous_metric.return_value = None, None
        self.assertRaises(virt_inspector.InstanceNotFoundException,
                          self.inspector.inspect_cpu_util,
                          mock.Mock(), duration=30)

        def mock_metric(util_cap, util_uncap, idle, donated, entitled):
            """Helper method to create mock proc metrics."""
            metric = mock.MagicMock()
            metric.processor.util_cap_proc_cycles = util_cap
            metric.processor.util_uncap_proc_cycles = util_uncap
            metric.processor.idle_proc_cycles = idle
            metric.processor.donated_proc_cycles = donated
            metric.processor.entitled_proc_cycles = entitled
            return metric

        # Validate that we get CPU utilization if current metrics are found,
        # but the previous are None
        self.mock_metrics.get_latest_metric.return_value = (
            mock.Mock(), mock_metric(7000, 50, 1000, 5000, 10000))
        self.mock_metrics.get_previous_metric.return_value = None, None
        resp = self.inspector.inspect_cpu_util(mock.Mock())
        self.assertEqual(10.5, resp.util)

        # Mock up a mixed use environment.
        cur = mock_metric(7000, 50, 1000, 5000, 10000)
        prev = mock_metric(4000, 25, 500, 2500, 5000)
        self.mock_metrics.get_latest_metric.return_value = mock.Mock(), cur
        self.mock_metrics.get_previous_metric.return_value = mock.Mock(), prev

        # Execute and validate
        resp = self.inspector.inspect_cpu_util(mock.Mock())
        self.assertEqual(.5, resp.util)

        # Mock an instance with a dedicated processor, but idling and donating
        # cycles to others.  In these scenarios, util_cap shows full use, but
        # the idle and donated get subtracted out.
        cur = mock_metric(10000, 0, 100, 4900, 10000)
        prev = mock_metric(5000, 0, 50, 2500, 5000)
        self.mock_metrics.get_latest_metric.return_value = mock.Mock(), cur
        self.mock_metrics.get_previous_metric.return_value = mock.Mock(), prev

        # Execute and validate
        resp = self.inspector.inspect_cpu_util(mock.Mock())
        self.assertEqual(51.0, resp.util)

        # Mock an instance with a shared processor.  By nature, this doesn't
        # idle or donate.  If it is 'idling' it is simply giving the cycles
        # out.  Show a low use one without needing extra cycles
        cur = mock_metric(9000, 0, 0, 0, 10000)
        prev = mock_metric(5000, 0, 0, 0, 5000)
        self.mock_metrics.get_latest_metric.return_value = mock.Mock(), cur
        self.mock_metrics.get_previous_metric.return_value = mock.Mock(), prev

        # Execute and validate
        resp = self.inspector.inspect_cpu_util(mock.Mock())
        self.assertEqual(80.0, resp.util)

        # Mock an instance with a shared processor - but using cycles from
        # the uncap pool.  This means it is using extra cycles from other
        # VMs that are currently not requiring the CPU.
        cur = mock_metric(10000, 10000, 0, 0, 10000)
        prev = mock_metric(5000, 0, 0, 0, 5000)
        self.mock_metrics.get_latest_metric.return_value = mock.Mock(), cur
        self.mock_metrics.get_previous_metric.return_value = mock.Mock(), prev

        # Execute and validate.  This should be running at 300% CPU
        # utilization.  Fast!
        resp = self.inspector.inspect_cpu_util(mock.Mock())
        self.assertEqual(300.0, resp.util)
