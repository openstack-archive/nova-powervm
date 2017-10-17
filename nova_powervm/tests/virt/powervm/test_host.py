# Copyright 2014, 2017 IBM Corp.
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
from oslo_serialization import jsonutils
from pypowervm.wrappers import iocard as pvm_card
from pypowervm.wrappers import managed_system as pvm_ms

from nova_powervm.virt.powervm import host as pvm_host

LOG = logging.getLogger(__name__)
logging.basicConfig()


def mock_sriov(adap_id, pports):
    sriov = mock.create_autospec(pvm_card.SRIOVAdapter, spec_set=True)
    sriov.configure_mock(sriov_adap_id=adap_id, phys_ports=pports)
    return sriov


def mock_pport(port_id, label, maxlps):
    port = mock.create_autospec(pvm_card.SRIOVEthPPort, spec_set=True)
    port.configure_mock(port_id=port_id, label=label, supp_max_lps=maxlps)
    return port


class TestPowerVMHost(test.NoDBTestCase):
    def test_host_resources(self):
        # Create objects to test with
        sriov_adaps = [
            mock_sriov(1, [mock_pport(2, 'foo', 1), mock_pport(3, '', 2)]),
            mock_sriov(4, [mock_pport(5, 'bar', 3)])]
        ms_wrapper = mock.create_autospec(pvm_ms.System, spec_set=True)
        asio = mock.create_autospec(pvm_ms.ASIOConfig, spec_set=True)
        asio.configure_mock(sriov_adapters=sriov_adaps)
        ms_wrapper.configure_mock(
            proc_units_configurable=500,
            proc_units_avail=500,
            memory_configurable=5242880,
            memory_free=5242752,
            memory_region_size='big',
            asio_config=asio)
        self.flags(host='the_hostname')

        # Run the actual test
        stats = pvm_host.build_host_resource_from_ms(ms_wrapper)
        self.assertIsNotNone(stats)

        # Check for the presence of fields
        fields = (('vcpus', 500), ('vcpus_used', 0),
                  ('memory_mb', 5242880), ('memory_mb_used', 128),
                  'hypervisor_type', 'hypervisor_version',
                  ('hypervisor_hostname', 'the_hostname'), 'cpu_info',
                  'supported_instances', 'stats', 'pci_passthrough_devices')
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
        # pci_passthrough_devices.  Parse json - entries can be in any order.
        ppdstr = stats['pci_passthrough_devices']
        ppdlist = jsonutils.loads(ppdstr)
        self.assertEqual({'foo', 'bar', 'default'}, {ppd['physical_network']
                                                     for ppd in ppdlist})
        self.assertEqual({'foo', 'bar', 'default'}, {ppd['label']
                                                     for ppd in ppdlist})
        self.assertEqual({'*:1:2.0', '*:1:3.0', '*:1:3.1', '*:4:5.0',
                          '*:4:5.1', '*:4:5.2'},
                         {ppd['address'] for ppd in ppdlist})
        for ppd in ppdlist:
            self.assertEqual('type-VF', ppd['dev_type'])
            self.assertEqual('*:*:*.*', ppd['parent_addr'])
            self.assertEqual('*', ppd['vendor_id'])
            self.assertEqual('*', ppd['product_id'])
            self.assertEqual(1, ppd['numa_node'])
