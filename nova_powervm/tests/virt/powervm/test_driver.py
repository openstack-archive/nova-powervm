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

import logging

import mock

from nova import test
from nova.virt import fake
from pypowervm.tests.wrappers.util import pvmhttp
from pypowervm.wrappers import constants as wpr_consts
import pypowervm.wrappers.managed_system as msentry_wrapper


from nova_powervm.virt.powervm import driver
from nova_powervm.virt.powervm import host as pvm_host

MS_HTTPRESP_FILE = "managedsystem.txt"
MC_HTTPRESP_FILE = "managementconsole.txt"
MS_NAME = 'HV4'

LOG = logging.getLogger(__name__)
logging.basicConfig()


class TestPowerVMDriver(test.TestCase):
    def setUp(self):
        super(TestPowerVMDriver, self).setUp()

        ms_http = pvmhttp.load_pvm_resp(MS_HTTPRESP_FILE)
        self.assertNotEqual(ms_http, None,
                            "Could not load %s " %
                            MS_HTTPRESP_FILE)

        entries = ms_http.response.feed.findentries(
            wpr_consts.SYSTEM_NAME, MS_NAME)

        self.assertNotEqual(entries, None,
                            "Could not find %s in %s" %
                            (MS_NAME, MS_HTTPRESP_FILE))

        self.myentry = entries[0]
        self.wrapper = msentry_wrapper.ManagedSystem(self.myentry)

    def test_driver_create(self):
        """Validates that a driver of the PowerVM type can just be
        initialized.
        """
        test_drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        self.assertIsNotNone(test_drv)

    @mock.patch('pypowervm.adapter.Session')
    @mock.patch('pypowervm.adapter.Adapter')
    @mock.patch('nova_powervm.virt.powervm.host.find_entry_by_mtm_serial')
    def test_driver_init(self, mock_find, mock_apt, mock_sess):
        """Validates the PowerVM driver can be initialized for the host."""
        drv = driver.PowerVMDriver(fake.FakeVirtAPI())
        drv.init_host('FakeHost')
        # Nothing to really check here specific to the host.
        self.assertIsNotNone(drv)

    def test_host_resources(self):
        stats = pvm_host.build_host_resource_from_entry(self.wrapper)
        self.assertIsNotNone(stats)

        # Check for the presence of fields
        fields = (('vcpus', 500), ('vcpus_used', 0),
                  ('memory_mb', 5242880), ('memory_mb_used', 128),
                  'local_gb', 'local_gb_used', 'hypervisor_type',
                  'hypervisor_version', 'hypervisor_hostname', 'cpu_info',
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
