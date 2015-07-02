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

import datetime
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
        pvm_adpt_fx = self.useFixture(api_fx.AdapterFx())
        self.adpt = pvm_adpt_fx.adpt
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

    @staticmethod
    def _mock_vnic_metric(rec_bytes, tx_bytes, rec_pkts, tx_pkts, phys_loc):
        """Helper method to create a specific mock network metric."""
        metric = mock.MagicMock()
        metric.received_bytes = rec_bytes
        metric.sent_bytes = tx_bytes
        metric.received_packets = rec_pkts
        metric.sent_packets = tx_pkts
        metric.physical_location = phys_loc
        return metric

    def _build_cur_mock_vnic_metrics(self):
        """Helper method to create mock network metrics."""
        cna1 = self._mock_vnic_metric(1000, 1000, 10, 10, 'a')
        cna2 = self._mock_vnic_metric(2000, 2000, 20, 20, 'b')
        cna3 = self._mock_vnic_metric(3000, 3000, 30, 30, 'c')

        metric = mock.MagicMock()
        metric.network.cnas = [cna1, cna2, cna3]
        return metric

    def _build_prev_mock_vnic_metrics(self):
        """Helper method to create mock network metrics."""
        cna1 = self._mock_vnic_metric(1000, 1000, 10, 10, 'a')
        cna2 = self._mock_vnic_metric(200, 200, 20, 20, 'b')

        metric = mock.MagicMock()
        metric.network.cnas = [cna1, cna2]
        return metric

    @staticmethod
    def _build_mock_cnas():
        """Builds a set of mock client network adapters."""
        cna1 = mock.MagicMock()
        cna1.loc_code, cna1.mac = 'a', 'AABBCCDDEEFF'

        cna2 = mock.MagicMock()
        cna2.loc_code, cna2.mac = 'b', 'AABBCCDDEE11'

        cna3 = mock.MagicMock()
        cna3.loc_code, cna3.mac = 'c', 'AABBCCDDEE22'

        return [cna1, cna2, cna3]

    @mock.patch('pypowervm.wrappers.network.CNA.wrap')
    def test_inspect_vnics(self, mock_wrap):
        """Tests the inspect_vnics inspector method for PowerVM."""
        # Validate that an error is raised if the instance can't be found in
        # the sample data.
        self.mock_metrics.get_latest_metric.return_value = None, None
        self.assertRaises(virt_inspector.InstanceNotFoundException,
                          list, self.inspector.inspect_vnics(mock.Mock()))

        # Validate that no data is returned if there is a current metric,
        # just no network within it.
        mock_empty_net = mock.MagicMock()
        mock_empty_net.network = None
        self.mock_metrics.get_latest_metric.return_value = None, mock_empty_net
        self.assertEqual([], list(self.inspector.inspect_vnics(mock.Mock())))

        # Build a couple CNAs and verify we get the proper list back
        mock_wrap.return_value = self._build_mock_cnas()
        self.adpt.read.return_value = mock.Mock()
        mock_metrics = self._build_cur_mock_vnic_metrics()
        self.mock_metrics.get_latest_metric.return_value = None, mock_metrics

        resp = list(self.inspector.inspect_vnics(mock.Mock()))
        self.assertEqual(3, len(resp))

        interface1, stats1 = resp[0]
        self.assertEqual('aa:bb:cc:dd:ee:ff', interface1.mac)
        self.assertEqual('a', interface1.name)
        self.assertEqual(1000, stats1.rx_bytes)
        self.assertEqual(1000, stats1.tx_bytes)
        self.assertEqual(10, stats1.rx_packets)
        self.assertEqual(10, stats1.tx_packets)

    @mock.patch('pypowervm.wrappers.network.CNA.wrap')
    def test_inspect_vnic_rates(self, mock_wrap):
        """Tests the inspect_vnic_rates inspector method for PowerVM."""
        # Validate that an error is raised if the instance can't be found in
        # the sample data.
        self.mock_metrics.get_latest_metric.return_value = None, None
        self.mock_metrics.get_previous_metric.return_value = None, None
        self.assertRaises(virt_inspector.InstanceNotFoundException,
                          list, self.inspector.inspect_vnic_rates(mock.Mock()))

        # Validate that no data is returned if there is a current metric,
        # just no network within it.
        mock_empty_net = mock.MagicMock()
        mock_empty_net.network = None
        self.mock_metrics.get_latest_metric.return_value = None, mock_empty_net
        self.assertEqual([],
                         list(self.inspector.inspect_vnic_rates(mock.Mock())))

        # Build the response LPAR data
        mock_wrap.return_value = self._build_mock_cnas()
        self.adpt.read.return_value = mock.Mock()

        # Current metric data
        mock_cur = self._build_cur_mock_vnic_metrics()
        cur_date = datetime.datetime.now()
        self.mock_metrics.get_latest_metric.return_value = cur_date, mock_cur

        # Build the previous
        mock_prev = self._build_prev_mock_vnic_metrics()
        prev_date = cur_date - datetime.timedelta(seconds=30)
        self.mock_metrics.get_previous_metric.return_value = (prev_date,
                                                              mock_prev)

        # Execute
        resp = list(self.inspector.inspect_vnic_rates(mock.Mock()))
        self.assertEqual(3, len(resp))

        # First metric.  No delta
        interface1, stats1 = resp[0]
        self.assertEqual('aa:bb:cc:dd:ee:ff', interface1.mac)
        self.assertEqual('a', interface1.name)
        self.assertEqual(0, stats1.rx_bytes_rate)
        self.assertEqual(0, stats1.tx_bytes_rate)

        # Second metric
        interface2, stats2 = resp[1]
        self.assertEqual('aa:bb:cc:dd:ee:11', interface2.mac)
        self.assertEqual('b', interface2.name)
        self.assertEqual(60.0, stats2.rx_bytes_rate)
        self.assertEqual(60.0, stats2.tx_bytes_rate)

        # Third metric had no previous.
        interface3, stats3 = resp[2]
        self.assertEqual('aa:bb:cc:dd:ee:22', interface3.mac)
        self.assertEqual('c', interface3.name)
        self.assertEqual(100.0, stats3.rx_bytes_rate)
        self.assertEqual(100.0, stats3.tx_bytes_rate)
