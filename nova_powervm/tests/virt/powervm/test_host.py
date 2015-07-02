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
import os
from pypowervm.tests.wrappers.util import pvmhttp
import pypowervm.wrappers.managed_system as pvm_ms
from pypowervm.wrappers.pcm import phyp as pvm_phyp

from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm import host as pvm_host

MS_HTTPRESP_FILE = "managedsystem.txt"
MS_NAME = 'HV4'

LOG = logging.getLogger(__name__)
logging.basicConfig()


class TestPowerVMHost(test.TestCase):
    def setUp(self):
        super(TestPowerVMHost, self).setUp()

        ms_http = pvmhttp.load_pvm_resp(MS_HTTPRESP_FILE)
        self.assertNotEqual(ms_http, None,
                            "Could not load %s " %
                            MS_HTTPRESP_FILE)

        entries = ms_http.response.feed.findentries(pvm_ms._SYSTEM_NAME,
                                                    MS_NAME)

        self.assertNotEqual(entries, None,
                            "Could not find %s in %s" %
                            (MS_NAME, MS_HTTPRESP_FILE))

        self.ms_entry = entries[0]
        self.wrapper = pvm_ms.System.wrap(self.ms_entry)

    def test_host_resources(self):
        stats = pvm_host.build_host_resource_from_ms(self.wrapper)
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
        self.pypvm = self.useFixture(fx.PyPowerVM())
        self.adpt = self.pypvm.apt

        # Test data
        dirname = os.path.dirname(__file__)
        file_name = os.path.join(dirname, 'data', 'phyp_pcm_data.txt')
        self.cur_json_resp = pvmhttp.PVMFile(file_name)
        file_name = os.path.join(dirname, 'data', 'phyp_2_pcm_data.txt')
        self.prev_json_resp = pvmhttp.PVMFile(file_name)

        # Put in the samples.
        self.phyp = pvm_phyp.PhypInfo(self.cur_json_resp.body)
        self.prev_phyp = pvm_phyp.PhypInfo(self.prev_json_resp.body)

    def _get_sample(self, lpar_id, sample):
        for lpar in sample.lpars:
            if lpar.id == lpar_id:
                return lpar
        return None

    @mock.patch('nova_powervm.virt.powervm.host.HostCPUStats._get_cpu_freq')
    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_update_internal_metric(self, mock_ensure_ltm, mock_refresh,
                                    mock_cpu_freq):
        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')
        mock_cpu_freq.return_value = 4116.0

        # Make sure None is returned if there is no data.
        host_stats.cur_phyp = None
        host_stats._update_internal_metric()
        self.assertEqual(None, host_stats.cur_data)

        # Make the 'prev' the current...for the first pass
        host_stats.cur_phyp = self.prev_phyp
        host_stats.prev_phyp = None
        host_stats._update_internal_metric()

        # Validate the dictionary...
        expect = {'iowait': 0, 'idle': 1.6125096675799704e+16,
                  'kernel': 58599310268, 'user': 789903553028,
                  'frequency': 4116.0}
        self.assertEqual(expect, host_stats.cur_data)

        # Now 'increment' it with a new current/previous
        host_stats.cur_phyp = self.phyp
        host_stats.prev_phyp = self.prev_phyp
        host_stats._update_internal_metric()

        # Validate this dictionary.  Note that the values are still higher
        # overall, even though we add the 'deltas' from each VM.
        expect = {'iowait': 0, 'idle': 1.6125066665694504e+16,
                  'kernel': 58599310268, 'user': 819913658228,
                  'frequency': 4116.0}
        self.assertEqual(expect, host_stats.cur_data)

    @mock.patch('subprocess.check_output')
    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_get_cpu_freq(self, mock_ensure_ltm, mock_refresh, mock_cmd):
        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')
        mock_cmd.return_value = '4116.000000MHz\n'
        self.assertEqual(4116.0, host_stats._get_cpu_freq())

    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_gather_user_cycles(self, mock_ensure_ltm, mock_refresh):
        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')

        # Test that we can run with previous samples and then without.
        host_stats.cur_phyp = self.phyp
        host_stats.prev_phyp = self.prev_phyp
        resp = host_stats._gather_user_cycles()
        self.assertEqual(30010105200, resp)

        # Last, test to make sure the previous data is used.
        host_stats.prev_data = {'user': 1000000}
        resp = host_stats._gather_user_cycles()
        self.assertEqual(30011105200, resp)

        # Now test if there is no previous sample.
        host_stats.prev_phyp = None
        resp = host_stats._gather_user_cycles()
        self.assertEqual(819914643228, resp)

    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_delta_proc_cycles(self, mock_ensure_ltm, mock_refresh):
        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')

        # Test that a previous sample allows us to gather the delta across all
        # of the VMs.  This should take into account the scenario where a LPAR
        # is deleted and a new one takes its place (LPAR ID 6)
        delta = host_stats._delta_proc_cycles(self.phyp.sample.lpars,
                                              self.prev_phyp.sample.lpars)
        self.assertEqual(10010105200, delta)

        # Now test as if there is no previous data.  Should result in higher
        # numbers.
        delta2 = host_stats._delta_proc_cycles(self.phyp.sample.lpars, None)
        self.assertEqual(265260844203, delta2)
        self.assertTrue(delta2 > delta)

        # Test that we can do this with the VIOSes as well.
        delta = host_stats._delta_proc_cycles(self.phyp.sample.vioses,
                                              self.prev_phyp.sample.vioses)
        self.assertEqual(20000000000, delta)

    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_delta_user_cycles(self, mock_ensure_ltm, mock_refresh):
        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')

        # Test that a previous sample allows us to gather just the delta.
        new_elem = self._get_sample(4, self.phyp.sample)
        old_elem = self._get_sample(4, self.prev_phyp.sample)
        delta = host_stats._delta_user_cycles(new_elem, old_elem)
        self.assertEqual(45000, delta)

        # Validate the scenario where we don't have a previous
        delta = host_stats._delta_user_cycles(new_elem, None)
        self.assertEqual(60000, delta)

    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_find_prev_sample(self, mock_ensure_ltm, mock_refresh):
        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')

        # Sample 6 in the current shouldn't match the previous.  It has the
        # same LPAR ID, but a different name.  This is considered different
        new_elem = self._get_sample(6, self.phyp.sample)
        prev = host_stats._find_prev_sample(new_elem,
                                            self.prev_phyp.sample.lpars)
        self.assertIsNone(prev)

        # Lpar 4 should be in the old one.  Match that up.
        new_elem = self._get_sample(4, self.phyp.sample)
        prev = host_stats._find_prev_sample(new_elem,
                                            self.prev_phyp.sample.lpars)
        self.assertIsNotNone(prev)
        self.assertEqual(500000, prev.processor.entitled_proc_cycles)

        # Test that we get None back if there are no previous samples
        prev = host_stats._find_prev_sample(new_elem, None)
        self.assertIsNone(prev)

    @mock.patch('pypowervm.tasks.monitor.util.MetricCache._refresh_if_needed')
    @mock.patch('pypowervm.tasks.monitor.util.ensure_ltm_monitors')
    def test_get_total_cycles(self, mock_ensure_ltm, mock_refresh):
        host_stats = pvm_host.HostCPUStats(self.adpt, 'host_uuid')
        host_stats.cur_phyp = self.phyp

        # Make sure we get the full system cycles.
        max_cycles = host_stats._get_total_cycles()
        self.assertEqual(1.6125945178663e+16, max_cycles)
