# Copyright 2016 IBM Corp.
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

from nova import exception
from nova import test
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.wrappers import managed_system as pvm_ms
from pypowervm.wrappers import network as pvm_net

from nova_powervm.virt.powervm import vif


def cna(mac):
    """Builds a mock Client Network Adapter for unit tests."""
    nic = mock.MagicMock()
    nic.mac = mac
    nic.vswitch_uri = 'fake_href'
    return nic


class TestVifFunctions(test.TestCase):

    def setUp(self):
        super(TestVifFunctions, self).setUp()

        self.adpt = self.useFixture(pvm_fx.AdapterFx(
            traits=pvm_fx.LocalPVMTraits)).adpt

    @mock.patch('pypowervm.wrappers.network.VSwitch.search')
    def test_get_secure_rmc_vswitch(self, mock_search):
        # Test no data coming back gets none
        mock_search.return_value = []
        resp = vif.get_secure_rmc_vswitch(self.adpt, 'host_uuid')
        self.assertIsNone(resp)

        # Mock that a couple vswitches get returned, but only the correct
        # MGMT Switch gets returned
        mock_vs = mock.MagicMock()
        mock_vs.name = 'MGMTSWITCH'
        mock_search.return_value = [mock_vs]
        self.assertEqual(mock_vs,
                         vif.get_secure_rmc_vswitch(self.adpt, 'host_uuid'))
        mock_search.assert_called_with(
            self.adpt, parent_type=pvm_ms.System.schema_type,
            parent_uuid='host_uuid', name=vif.SECURE_RMC_VSWITCH)

    @mock.patch('pypowervm.tasks.cna.crt_cna')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    def test_plug_secure_rmc_vif(self, mock_pvm_uuid, mock_crt):
        mock_pvm_uuid.return_value = 'lpar_uuid'
        vif.plug_secure_rmc_vif(self.adpt, 'instance', 'host_uuid')
        mock_crt.assert_called_once_with(
            self.adpt, 'host_uuid', 'lpar_uuid', 4094, vswitch='MGMTSWITCH',
            crt_vswitch=True)

    def test_build_vif_driver(self):
        # Test the Shared Ethernet Adapter type VIF
        mock_inst = mock.MagicMock()
        mock_inst.name = 'instance'
        self.assertIsInstance(
            vif._build_vif_driver(self.adpt, 'host_uuid', mock_inst,
                                  {'type': 'pvm_sea'}),
            vif.PvmSeaVifDriver)

        # Test raises exception for no type
        self.assertRaises(exception.VirtualInterfacePlugException,
                          vif._build_vif_driver, self.adpt, 'host_uuid',
                          mock_inst, {})

        # Test an invalid vif type
        self.assertRaises(exception.VirtualInterfacePlugException,
                          vif._build_vif_driver, self.adpt, 'host_uuid',
                          mock_inst, {'type': 'ovs'})


class TestVifSeaDriver(test.TestCase):

    def setUp(self):
        super(TestVifSeaDriver, self).setUp()

        self.adpt = self.useFixture(pvm_fx.AdapterFx(
            traits=pvm_fx.LocalPVMTraits)).adpt
        self.inst = mock.MagicMock()
        self.drv = vif.PvmSeaVifDriver(self.adpt, 'host_uuid', self.inst)

    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('pypowervm.tasks.cna.crt_cna')
    def test_plug(self, mock_crt_cna, mock_pvm_uuid):
        """Tests that a VIF can be created."""

        # Set up the mocks
        fake_vif = {'network': {'meta': {'vlan': 5}},
                    'address': 'aabbccddeeff'}

        def validate_crt(adpt, host_uuid, lpar_uuid, vlan, mac_addr=None):
            self.assertEqual('host_uuid', host_uuid)
            self.assertEqual(5, vlan)
            self.assertEqual('aabbccddeeff', mac_addr)
            return pvm_net.CNA.bld(self.adpt, 5, host_uuid)
        mock_crt_cna.side_effect = validate_crt

        # Invoke
        resp = self.drv.plug(fake_vif)

        # Validate (along with validate method above)
        self.assertEqual(1, mock_crt_cna.call_count)
        self.assertIsNotNone(resp)
        self.assertIsInstance(resp, pvm_net.CNA)

    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_unplug_vifs(self, mock_vm_get):
        """Tests that a delete of the vif can be done."""
        # Mock up the CNA response.  Two should already exist, the other
        # should not.
        cnas = [cna('AABBCCDDEEFF'), cna('AABBCCDDEE11'), cna('AABBCCDDEE22')]
        mock_vm_get.return_value = cnas

        # Run method.  The AABBCCDDEE11 wont' be unplugged (wasn't invoked
        # below) and the last unplug will also just no-op because its not on
        # the VM.
        self.drv.unplug({'address': 'aa:bb:cc:dd:ee:ff'})
        self.drv.unplug({'address': 'aa:bb:cc:dd:ee:22'})
        self.drv.unplug({'address': 'aa:bb:cc:dd:ee:33'})

        # The delete should have only been called once.  The second CNA didn't
        # have a matching mac...so it should be skipped.
        self.assertEqual(1, cnas[0].delete.call_count)
        self.assertEqual(0, cnas[1].delete.call_count)
        self.assertEqual(1, cnas[2].delete.call_count)
