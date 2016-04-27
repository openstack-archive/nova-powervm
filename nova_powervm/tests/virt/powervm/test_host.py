# Copyright 2014, 2015 IBM Corp.
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

import mock

import logging
from nova import test
import pypowervm.tests.test_fixtures as pvm_fx

from nova_powervm.virt.powervm import host as pvm_host

LOG = logging.getLogger(__name__)
logging.basicConfig()


class TestPowerVMHost(test.TestCase):
    def test_host_resources(self):
        # Create objects to test with
        ms_wrapper = mock.MagicMock(
            proc_units_configurable=500,
            proc_units_avail=500,
            memory_configurable=5242880,
            memory_free=5242752,
            mtms=mock.MagicMock(mtms_str='8484923A123456'),
            memory_region_size='big')

        # Run the actual test
        stats = pvm_host.build_host_resource_from_ms(ms_wrapper)
        self.assertIsNotNone(stats)

        # Check for the presence of fields
        fields = (('vcpus', 500), ('vcpus_used', 0),
                  ('memory_mb', 5242880), ('memory_mb_used', 128),
                  'hypervisor_type', 'hypervisor_version',
                  'hypervisor_hostname', 'cpu_info',
                  'supported_instances', 'stats')
        for fld in fields:
            if isinstance(fld, tuple):
                value = stats.get(fld[0], None)
                self.assertEqual(value, fld[1])
            else:
                value = stats.get(fld, None)
                self.assertIsNotNone(value)
        # Check for individual stats
        hstats = (('proc_units', '500.00'), ('proc_units_used', '0.00'))
        for stat in hstats:
            if isinstance(stat, tuple):
                value = stats['stats'].get(stat[0], None)
                self.assertEqual(value, stat[1])
            else:
                value = stats['stats'].get(stat, None)
                self.assertIsNotNone(value)


class TestHostCPUStats(test.TestCase):

    def setUp(self):
        super(TestHostCPUStats, self).setUp()

        # Fixture for the adapter
        self.adpt = self.useFixture(pvm_fx.AdapterFx()).adpt

    def _get_sample(self, lpar_id, sample):
        for lpar in sample.lpars:
            if lpar.id == lpar_id:
                return lpar
        return None

    @mock.patch('nova_powervm.virt.powervm.host.HostCPUStats.'
                '_get_fw_cycles_delta')
    @mock.patch('nova_powervm.virt.powervm.host.HostCPUStats._get_cpu_freq')
    @mock.patch('nova_powervm.virt.powervm.host.HostCPUStats.'
                '_get_total_cycles_delta')
    @mock.patch('nova_powervm.virt.powervm.host.HostCPUStats.'
                '_gather_user_cycles_delta')
    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_update_internal_metric(
        self, mock_ensure_ltm, mock_refresh, mock_user_cycles,
        mock_total_cycles, mock_cpu_freq, mock_fw_cycles):

        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')
        mock_cpu_freq.return_value = 4116

        # Make sure None is returned if there is no data.
        host_stats.cur_phyp = None
        host_stats._update_internal_metric()
        expect = {'iowait': 0, 'idle': 0, 'kernel': 0, 'user': 0,
                  'frequency': 0}
        self.assertEqual(expect, host_stats.tot_data)

        # Create mock phyp objects to test with
        mock_phyp = mock.MagicMock()
        mock_fw_cycles.return_value = 58599310268
        mock_prev_phyp = mock.MagicMock()

        # Mock methods not currently under test
        mock_user_cycles.return_value = 50
        mock_total_cycles.return_value = 1.6125945178663e+16

        # Make the 'prev' the current...for the first pass
        host_stats.cur_phyp = mock_prev_phyp
        host_stats.prev_phyp = None
        host_stats._update_internal_metric()

        # Validate the dictionary...  No user cycles because all of the
        # previous data is empty.
        expect = {'iowait': 0, 'idle': 1.6125886579352682e+16,
                  'kernel': 58599310268, 'user': 50,
                  'frequency': 4116}
        self.assertEqual(expect, host_stats.tot_data)

        # Mock methods not currently under test
        mock_user_cycles.return_value = 30010090000
        mock_total_cycles.return_value = 1.6125945178663e+16

        # Now 'increment' it with a new current/previous
        host_stats.cur_phyp = mock_phyp
        host_stats.prev_phyp = mock_prev_phyp
        mock_user_cycles.return_value = 100000
        host_stats._update_internal_metric()

        # Validate this dictionary.  Note that these values are 'higher'
        # because this is a running total.
        new_kern = 58599310268 * 2
        expect = {'iowait': 0, 'idle': 3.2251773158605416e+16,
                  'kernel': new_kern, 'user': 100050,
                  'frequency': 4116}
        self.assertEqual(expect, host_stats.tot_data)

    @mock.patch('nova_powervm.virt.powervm.host.HostCPUStats.'
                '_get_fw_cycles_delta')
    @mock.patch('nova_powervm.virt.powervm.host.HostCPUStats.'
                '_get_total_cycles_delta')
    @mock.patch('nova_powervm.virt.powervm.host.HostCPUStats.'
                '_gather_user_cycles_delta')
    @mock.patch('nova_powervm.virt.powervm.host.HostCPUStats._get_cpu_freq')
    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_update_internal_metric_bad_total(
            self, mock_ensure_ltm, mock_refresh, mock_cpu_freq,
            mock_user_cycles, mock_tot_cycles, mock_fw_cycles):
        """Validates that if the total cycles are off, we handle."""
        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')
        mock_cpu_freq.return_value = 4116
        mock_user_cycles.return_value = 30010090000
        mock_fw_cycles.return_value = 58599310268

        # Mock the total cycles to some really low number.
        mock_tot_cycles.return_value = 5

        # Create mock phyp objects to test with
        mock_phyp = mock.MagicMock()
        mock_prev_phyp = mock.MagicMock()
        mock_phyp.sample.system_firmware.utilized_proc_cycles = 58599310268

        # Run the actual test - 'increment' it with a new current/previous
        host_stats.cur_phyp = mock_phyp
        host_stats.prev_phyp = mock_prev_phyp
        host_stats._update_internal_metric()

        # Validate this dictionary.  Note that the idle is now 0...not a
        # negative number.
        expect = {'iowait': 0, 'idle': 0, 'kernel': 58599310268,
                  'user': 30010090000, 'frequency': 4116}
        self.assertEqual(expect, host_stats.tot_data)

    @mock.patch('subprocess.check_output')
    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_get_cpu_freq(self, mock_ensure_ltm, mock_refresh, mock_cmd):
        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')
        mock_cmd.return_value = '4116.000000MHz\n'
        self.assertEqual(4116, host_stats._get_cpu_freq())
        self.assertEqual(int, type(host_stats._get_cpu_freq()))

    @mock.patch('nova_powervm.virt.powervm.host.HostCPUStats.'
                '_delta_proc_cycles')
    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_gather_user_cycles_delta(self, mock_ensure_ltm, mock_refresh,
                                      mock_cycles):
        # Crete objects to test with
        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')
        mock_phyp = mock.MagicMock()
        mock_prev_phyp = mock.MagicMock()

        # Mock methods not currently under test
        mock_cycles.return_value = 15005045000

        # Test that we can run with previous samples and then without.
        host_stats.cur_phyp = mock_phyp
        host_stats.prev_phyp = mock_prev_phyp
        resp = host_stats._gather_user_cycles_delta()
        self.assertEqual(30010090000, resp)

        # Now test if there is no previous sample.  Since there are no previous
        # samples, it will be 0.
        host_stats.prev_phyp = None
        mock_cycles.return_value = 0
        resp = host_stats._gather_user_cycles_delta()
        self.assertEqual(0, resp)

    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_delta_proc_cycles(self, mock_ensure_ltm, mock_refresh):
        # Create objects to test with
        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')
        mock_phyp, mock_prev_phyp = self._get_mock_phyps()

        # Test that a previous sample allows us to gather the delta across all
        # of the VMs.  This should take into account the scenario where a LPAR
        # is deleted and a new one takes its place (LPAR ID 6)
        delta = host_stats._delta_proc_cycles(mock_phyp.sample.lpars,
                                              mock_prev_phyp.sample.lpars)
        self.assertEqual(10010000000, delta)

        # Now test as if there is no previous data.  This results in 0 as they
        # could have all been LPMs with months of cycles (rather than 30
        # seconds delta).
        delta2 = host_stats._delta_proc_cycles(mock_phyp.sample.lpars, None)
        self.assertEqual(0, delta2)
        self.assertNotEqual(delta2, delta)

        # Test that if previous sample had 0 values, the sample is not
        # considered for evaluation, and resultant delta cycles is 0.
        prev_lpar_sample = mock_prev_phyp.sample.lpars[0].processor
        prev_lpar_sample.util_cap_proc_cycles = 0
        prev_lpar_sample.util_uncap_proc_cycles = 0
        prev_lpar_sample.donated_proc_cycles = 0
        delta3 = host_stats._delta_proc_cycles(mock_phyp.sample.lpars,
                                               mock_prev_phyp.sample.lpars)
        self.assertEqual(0, delta3)

    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_delta_user_cycles(self, mock_ensure_ltm, mock_refresh):
        # Create objects to test with
        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')
        mock_phyp, mock_prev_phyp = self._get_mock_phyps()
        mock_phyp.sample.lpars[0].processor.util_cap_proc_cycles = 250000
        mock_phyp.sample.lpars[0].processor.util_uncap_proc_cycles = 250000
        mock_phyp.sample.lpars[0].processor.donated_proc_cycles = 500
        mock_prev_phyp.sample.lpars[0].processor.util_cap_proc_cycles = 0
        num = 455000
        mock_prev_phyp.sample.lpars[0].processor.util_uncap_proc_cycles = num
        mock_prev_phyp.sample.lpars[0].processor.donated_proc_cycles = 1000

        # Test that a previous sample allows us to gather just the delta.
        new_elem = self._get_sample(4, mock_phyp.sample)
        old_elem = self._get_sample(4, mock_prev_phyp.sample)
        delta = host_stats._delta_user_cycles(new_elem, old_elem)
        self.assertEqual(45500, delta)

        # Validate the scenario where we don't have a previous.  Should default
        # to 0, given no context of why the previous sample did not have the
        # data.
        delta = host_stats._delta_user_cycles(new_elem, None)
        self.assertEqual(0, delta)

    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_find_prev_sample(self, mock_ensure_ltm, mock_refresh):
        # Create objects to test with
        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')
        mock_lpar_4A = mock.Mock()
        mock_lpar_4A.configure_mock(id=4, name='A')
        mock_lpar_4A.processor = mock.MagicMock(
            entitled_proc_cycles=500000)
        mock_lpar_6A = mock.Mock()
        mock_lpar_6A.configure_mock(id=6, name='A')
        mock_lpar_6B = mock.Mock()
        mock_lpar_6A.configure_mock(id=6, name='B')
        mock_phyp = mock.MagicMock(sample=mock.MagicMock(lpars=[mock_lpar_4A,
                                                                mock_lpar_6A]))
        mock_prev_phyp = mock.MagicMock(sample=mock.MagicMock(
            lpars=[mock_lpar_4A, mock_lpar_6B]))

        # Sample 6 in the current shouldn't match the previous.  It has the
        # same LPAR ID, but a different name.  This is considered different
        new_elem = self._get_sample(6, mock_phyp.sample)
        prev = host_stats._find_prev_sample(new_elem,
                                            mock_prev_phyp.sample.lpars)
        self.assertIsNone(prev)

        # Lpar 4 should be in the old one.  Match that up.
        new_elem = self._get_sample(4, mock_phyp.sample)
        prev = host_stats._find_prev_sample(new_elem,
                                            mock_prev_phyp.sample.lpars)
        self.assertIsNotNone(prev)
        self.assertEqual(500000, prev.processor.entitled_proc_cycles)

        # Test that we get None back if there are no previous samples
        prev = host_stats._find_prev_sample(new_elem, None)
        self.assertIsNone(prev)

    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_get_total_cycles(self, mock_ensure_ltm, mock_refresh):
        # Mock objects to test with
        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')
        mock_phyp = mock.MagicMock()
        mock_phyp.sample = mock.MagicMock()
        mock_phyp.sample.processor.configurable_proc_units = 5
        mock_phyp.sample.time_based_cycles = 500
        host_stats.cur_phyp = mock_phyp

        # Make sure we get the full system cycles.
        max_cycles = host_stats._get_total_cycles_delta()
        self.assertEqual(2500, max_cycles)

    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_get_total_cycles_diff_cores(self, mock_ensure_ltm, mock_refresh):
        # Mock objects to test with
        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')

        # Latest Sample
        mock_phyp = mock.MagicMock(sample=mock.MagicMock())
        mock_phyp.sample.processor.configurable_proc_units = 48
        mock_phyp.sample.time_based_cycles = 1000
        host_stats.cur_phyp = mock_phyp

        # Earlier sample.  Use a higher proc unit sample
        mock_phyp = mock.MagicMock(sample=mock.MagicMock())
        mock_phyp.sample.processor.configurable_proc_units = 1
        mock_phyp.sample.time_based_cycles = 500
        host_stats.prev_phyp = mock_phyp

        # Make sure we get the full system cycles.
        max_cycles = host_stats._get_total_cycles_delta()
        self.assertEqual(24000, max_cycles)

    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_get_firmware_cycles(self, mock_ensure_ltm, mock_refresh):
        # Mock objects to test with
        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')

        # Latest Sample
        mock_phyp = mock.MagicMock(sample=mock.MagicMock())
        mock_phyp.sample.system_firmware.utilized_proc_cycles = 2000
        # Previous Sample
        prev_phyp = mock.MagicMock(sample=mock.MagicMock())
        prev_phyp.sample.system_firmware.utilized_proc_cycles = 1000

        host_stats.cur_phyp = mock_phyp
        host_stats.prev_phyp = prev_phyp
        # Get delta
        delta_firmware_cycles = host_stats._get_fw_cycles_delta()
        self.assertEqual(1000, delta_firmware_cycles)

    def _get_mock_phyps(self):
        """Helper method to return cur_phyp and prev_phyp."""
        mock_lpar_4A = mock.Mock()
        mock_lpar_4A.configure_mock(id=4, name='A')
        mock_lpar_4A.processor = mock.MagicMock(
            util_cap_proc_cycles=5005045000,
            util_uncap_proc_cycles=5005045000,
            donated_proc_cycles=10000)
        mock_lpar_4A_prev = mock.Mock()
        mock_lpar_4A_prev.configure_mock(id=4, name='A')
        mock_lpar_4A_prev.processor = mock.MagicMock(
            util_cap_proc_cycles=40000,
            util_uncap_proc_cycles=40000,
            donated_proc_cycles=0)
        mock_phyp = mock.MagicMock(sample=mock.MagicMock(lpars=[mock_lpar_4A]))
        mock_prev_phyp = mock.MagicMock(
            sample=mock.MagicMock(lpars=[mock_lpar_4A_prev]))
        return mock_phyp, mock_prev_phyp
