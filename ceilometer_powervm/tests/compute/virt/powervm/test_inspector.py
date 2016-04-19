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
from pypowervm.helpers import log_helper as log_hlp
from pypowervm.helpers import vios_busy as vio_hlp
from pypowervm.tests import test_fixtures as api_fx

from ceilometer_powervm.compute.virt.powervm import inspector as p_inspect
from ceilometer_powervm.tests.compute.virt.powervm import pvm_fixtures


class TestPowerVMInspectorInit(base.BaseTestCase):
    """Tests the initialization of the VM Inspector."""

    @mock.patch('pypowervm.tasks.monitor.util.LparMetricCache')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    @mock.patch('ceilometer_powervm.compute.virt.powervm.inspector.'
                'PowerVMInspector._get_host_uuid')
    @mock.patch('pypowervm.adapter.Adapter')
    @mock.patch('pypowervm.adapter.Session')
    def test_init(self, mock_session, mock_adapter, mock_get_host_uuid,
                  mock_ensure_ltm, mock_cache):
        # Mock up data
        mock_get_host_uuid.return_value = 'host_uuid'

        # Invoke
        inspector = p_inspect.PowerVMInspector()

        # Validate
        mock_session.assert_called_once_with(conn_tries=300)
        mock_adapter.assert_called_once_with(
            mock_session.return_value,
            helpers=[log_hlp.log_helper, vio_hlp.vios_busy_retry_helper])

        mock_get_host_uuid.assert_called_once_with(mock_adapter.return_value)
        mock_ensure_ltm.assert_called_once_with(mock_adapter.return_value,
                                                'host_uuid')
        mock_cache.assert_called_once_with(mock_adapter.return_value,
                                           'host_uuid')
        self.assertEqual(mock_cache.return_value, inspector.vm_metrics)


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

        # Validate that the CPU metrics raise an issue if the previous metric
        # can't be found (perhaps due to a live migration).
        self.mock_metrics.get_latest_metric.return_value = (
            mock.Mock(), mock_metric(7000, 50, 1000, 5000, 10000))
        self.mock_metrics.get_previous_metric.return_value = None, None
        self.assertRaises(virt_inspector.InstanceNotFoundException,
                          self.inspector.inspect_cpu_util, mock.Mock())

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

        # Mock an instance that hasn't been started yet.
        cur = mock_metric(0, 0, 0, 0, 0)
        prev = mock_metric(0, 0, 0, 0, 0)
        self.mock_metrics.get_latest_metric.return_value = mock.Mock(), cur
        self.mock_metrics.get_previous_metric.return_value = mock.Mock(), prev

        # This should have 0% utilization
        resp = self.inspector.inspect_cpu_util(mock.Mock())
        self.assertEqual(0.0, resp.util)

    @staticmethod
    def _mock_vnic_metric(rec_bytes, tx_bytes, rec_pkts, tx_pkts, phys_loc):
        """Helper method to create a specific mock network metric."""
        return mock.Mock(received_bytes=rec_bytes, sent_bytes=tx_bytes,
                         received_packets=rec_pkts, sent_packets=tx_pkts,
                         physical_location=phys_loc)

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

    @staticmethod
    def _mock_stor_metric(num_reads, num_writes, read_bytes, write_bytes,
                          name):
        """Helper method to create a specific mock storage metric."""
        m = mock.Mock(num_reads=num_reads, num_writes=num_writes,
                      read_bytes=read_bytes, write_bytes=write_bytes)
        # Have to do this method as name is a special field.
        m.configure_mock(name=name)
        return m

    def _build_cur_mock_stor_metrics(self):
        """Helper method to create mock storage metrics."""
        vscsi1 = self._mock_stor_metric(1000, 1000, 100000, 100000, 'vscsi1')
        vscsi2 = self._mock_stor_metric(2000, 2000, 200000, 200000, 'vscsi2')
        vfc1 = self._mock_stor_metric(3000, 3000, 300000, 300000, 'vfc1')

        storage = mock.Mock(virt_adpts=[vscsi1, vscsi2], vfc_adpts=[vfc1])
        metric = mock.MagicMock(storage=storage)
        return metric

    def _build_prev_mock_stor_metrics(self):
        """Helper method to create mock storage metrics."""
        vscsi1 = self._mock_stor_metric(500, 500, 50000, 50000, 'vscsi1')
        vfc1 = self._mock_stor_metric(2000, 2000, 20000, 200000, 'vfc1')

        storage = mock.Mock(virt_adpts=[vscsi1], vfc_adpts=[vfc1])
        metric = mock.MagicMock(storage=storage)
        return metric

    def test_inspect_disk_iops(self):
        """Tests the inspect_disk_iops inspector method for PowerVM."""
        # Validate that an error is raised if the instance can't be found in
        # the sample data.
        self.mock_metrics.get_latest_metric.return_value = None, None
        self.mock_metrics.get_previous_metric.return_value = None, None
        self.assertRaises(virt_inspector.InstanceNotFoundException,
                          list, self.inspector.inspect_disk_iops(mock.Mock()))

        # Validate that no data is returned if there is a current metric,
        # just no storage within it.
        mock_empty_st = mock.MagicMock(storage=None)
        self.mock_metrics.get_latest_metric.return_value = None, mock_empty_st
        self.assertEqual([],
                         list(self.inspector.inspect_disk_iops(mock.Mock())))

        # Current metric data
        mock_cur = self._build_cur_mock_stor_metrics()
        cur_date = datetime.datetime.now()
        self.mock_metrics.get_latest_metric.return_value = cur_date, mock_cur

        # Validate that if there is no previous data, get no data back.
        self.assertEqual([],
                         list(self.inspector.inspect_disk_iops(mock.Mock())))

        # Build the previous
        mock_prev = self._build_prev_mock_stor_metrics()
        prev_date = cur_date - datetime.timedelta(seconds=30)
        self.mock_metrics.get_previous_metric.return_value = (prev_date,
                                                              mock_prev)

        # Execute
        resp = list(self.inspector.inspect_disk_iops(mock.Mock()))
        self.assertEqual(3, len(resp))

        # Two vSCSI's
        disk1, stats1 = resp[0]
        self.assertEqual('vscsi1', disk1.device)
        self.assertEqual(33, stats1.iops_count)

        disk2, stats2 = resp[1]
        self.assertEqual('vscsi2', disk2.device)
        self.assertEqual(133, stats2.iops_count)

        # Next is the vFC metric
        disk3, stats3 = resp[2]
        self.assertEqual('vfc1', disk3.device)
        self.assertEqual(66, stats3.iops_count)

    def test_inspect_disks(self):
        """Tests the inspect_disks inspector method for PowerVM."""
        # Validate that an error is raised if the instance can't be found in
        # the sample data.
        self.mock_metrics.get_latest_metric.return_value = None, None
        self.assertRaises(virt_inspector.InstanceNotFoundException,
                          list, self.inspector.inspect_disks(mock.Mock()))

        # Validate that no data is returned if there is a current metric,
        # just no storage within it.
        mock_empty_st = mock.MagicMock(storage=None)
        self.mock_metrics.get_latest_metric.return_value = None, mock_empty_st
        self.assertEqual([],
                         list(self.inspector.inspect_disks(mock.Mock())))

        # Current metric data
        mock_cur = self._build_cur_mock_stor_metrics()
        cur_date = datetime.datetime.now()
        self.mock_metrics.get_latest_metric.return_value = cur_date, mock_cur

        # Execute
        resp = list(self.inspector.inspect_disks(mock.Mock()))
        self.assertEqual(3, len(resp))

        # Two vSCSIs.
        disk1, stats1 = resp[0]
        self.assertEqual('vscsi1', disk1.device)
        self.assertEqual(1000, stats1.read_requests)
        self.assertEqual(100000, stats1.read_bytes)
        self.assertEqual(1000, stats1.write_requests)
        self.assertEqual(100000, stats1.write_bytes)
        self.assertEqual(0, stats1.errors)

        disk2, stats2 = resp[1]
        self.assertEqual('vscsi2', disk2.device)
        self.assertEqual(2000, stats2.read_requests)
        self.assertEqual(200000, stats2.read_bytes)
        self.assertEqual(2000, stats2.write_requests)
        self.assertEqual(200000, stats2.write_bytes)
        self.assertEqual(0, stats1.errors)

        # Next is the vFC metric
        disk3, stats3 = resp[2]
        self.assertEqual('vfc1', disk3.device)
        self.assertEqual(3000, stats3.read_requests)
        self.assertEqual(300000, stats3.read_bytes)
        self.assertEqual(3000, stats3.write_requests)
        self.assertEqual(300000, stats3.write_bytes)
        self.assertEqual(0, stats3.errors)
