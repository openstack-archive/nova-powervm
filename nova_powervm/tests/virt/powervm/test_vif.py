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
import netifaces

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
        self.slot_mgr = mock.Mock()

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
        # Mock up the data
        mock_pvm_uuid.return_value = 'lpar_uuid'
        mock_crt.return_value = mock.Mock()
        self.slot_mgr.build_map.get_mgmt_vea_slot = mock.Mock(
            return_value=(None, None))

        # Run the method
        vif.plug_secure_rmc_vif(self.adpt, 'instance', 'host_uuid',
                                self.slot_mgr)

        # Validate responses
        mock_crt.assert_called_once_with(
            self.adpt, 'host_uuid', 'lpar_uuid', 4094, vswitch='MGMTSWITCH',
            crt_vswitch=True, slot_num=None, mac_addr=None)
        self.slot_mgr.register_cna.assert_called_once_with(
            mock_crt.return_value)

    @mock.patch('pypowervm.tasks.cna.crt_cna')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    def test_plug_secure_rmc_vif_with_slot(self, mock_pvm_uuid, mock_crt):
        # Mock up the data
        mock_pvm_uuid.return_value = 'lpar_uuid'
        mock_crt.return_value = mock.Mock()
        self.slot_mgr.build_map.get_mgmt_vea_slot = mock.Mock(
            return_value=('mac_addr', 5))

        # Run the method
        vif.plug_secure_rmc_vif(self.adpt, 'instance', 'host_uuid',
                                self.slot_mgr)

        # Validate responses
        mock_crt.assert_called_once_with(
            self.adpt, 'host_uuid', 'lpar_uuid', 4094, vswitch='MGMTSWITCH',
            crt_vswitch=True, slot_num=5, mac_addr='mac_addr')
        self.assertFalse(self.slot_mgr.called)

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
                          mock_inst, {'type': 'bad'})


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
        fake_slot_num = 5

        def validate_crt(adpt, host_uuid, lpar_uuid, vlan, mac_addr=None,
                         slot_num=None):
            self.assertEqual('host_uuid', host_uuid)
            self.assertEqual(5, vlan)
            self.assertEqual('aabbccddeeff', mac_addr)
            self.assertEqual(5, slot_num)
            return pvm_net.CNA.bld(self.adpt, 5, host_uuid, slot_num=slot_num,
                                   mac_addr=mac_addr)
        mock_crt_cna.side_effect = validate_crt

        # Invoke
        resp = self.drv.plug(fake_vif, fake_slot_num)

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


class TestVifOvsDriver(test.TestCase):

    def setUp(self):
        super(TestVifOvsDriver, self).setUp()

        self.adpt = self.useFixture(pvm_fx.AdapterFx(
            traits=pvm_fx.LocalPVMTraits)).adpt
        self.inst = mock.MagicMock(uuid='inst_uuid')
        self.drv = vif.PvmOvsVifDriver(self.adpt, 'host_uuid', self.inst)

    @mock.patch('nova.utils.execute')
    @mock.patch('nova.network.linux_net.create_ovs_vif_port')
    @mock.patch('nova_powervm.virt.powervm.vif.PvmOvsVifDriver.'
                'get_trunk_dev_name')
    @mock.patch('pypowervm.tasks.cna.crt_p2p_cna')
    @mock.patch('pypowervm.tasks.partition.get_this_partition')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    def test_plug(self, mock_pvm_uuid, mock_mgmt_lpar, mock_p2p_cna,
                  mock_trunk_dev_name, mock_crt_ovs_vif_port, mock_exec):
        # Mock the data
        mock_pvm_uuid.return_value = 'lpar_uuid'
        mock_mgmt_lpar.return_value = mock.Mock(uuid='mgmt_uuid')
        mock_trunk_dev_name.return_value = 'device'

        cna_w, trunk_wraps = mock.MagicMock(), [mock.MagicMock()]
        mock_p2p_cna.return_value = cna_w, trunk_wraps

        # Run the plug
        vif = {'network': {'bridge': 'br0'}, 'address': 'aa:bb:cc:dd:ee:ff',
               'id': 'vif_id'}
        slot_num = 5
        self.drv.plug(vif, slot_num)

        # Validate the calls
        mock_crt_ovs_vif_port.assert_called_once_with(
            'br0', 'device', 'vif_id', 'aa:bb:cc:dd:ee:ff', 'inst_uuid')
        mock_p2p_cna.assert_called_once_with(
            self.adpt, 'host_uuid', 'lpar_uuid', ['mgmt_uuid'], 'OpenStackOVS',
            crt_vswitch=True, mac_addr='aa:bb:cc:dd:ee:ff', slot_num=slot_num)
        mock_exec.assert_called_once_with('ip', 'link', 'set', 'device', 'up',
                                          run_as_root=True)

    @mock.patch('netifaces.ifaddresses')
    @mock.patch('netifaces.interfaces')
    def test_get_trunk_dev_name(self, mock_interfaces, mock_ifaddresses):
        trunk_w = mock.Mock(mac='01234567890A')

        mock_link_addrs1 = {
            netifaces.AF_LINK: [{'addr': '00:11:22:33:44:55'},
                                {'addr': '00:11:22:33:44:66'}]}
        mock_link_addrs2 = {
            netifaces.AF_LINK: [{'addr': '00:11:22:33:44:77'},
                                {'addr': '01:23:45:67:89:0a'}]}

        mock_interfaces.return_value = ['a', 'b']
        mock_ifaddresses.side_effect = [mock_link_addrs1, mock_link_addrs2]

        # The mock_link_addrs2 (or interface b) should be the match
        self.assertEqual('b', self.drv.get_trunk_dev_name(trunk_w))

        # If you take out the correct adapter, make sure it fails.
        mock_interfaces.return_value = ['a']
        mock_ifaddresses.side_effect = [mock_link_addrs1]
        self.assertRaises(exception.VirtualInterfacePlugException,
                          self.drv.get_trunk_dev_name, trunk_w)

    @mock.patch('pypowervm.tasks.cna.find_trunks')
    @mock.patch('nova.network.linux_net.delete_ovs_vif_port')
    @mock.patch('nova_powervm.virt.powervm.vif.PvmOvsVifDriver.'
                'get_trunk_dev_name')
    @mock.patch('nova_powervm.virt.powervm.vif.PvmOvsVifDriver.'
                '_find_cna_for_vif')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_unplug(self, mock_get_cnas, mock_find_cna, mock_trunk_dev_name,
                    mock_del_ovs_port, mock_find_trunks):
        # Set up the mocks
        mock_cna = mock.Mock()
        mock_get_cnas.return_value = [mock_cna, mock.Mock()]
        mock_find_cna.return_value = mock_cna

        t1, t2 = mock.MagicMock(), mock.MagicMock()
        mock_find_trunks.return_value = [t1, t2]

        mock_trunk_dev_name.return_value = 'fake_dev'

        # Call the unplug
        vif = {'address': 'aa:bb:cc:dd:ee:ff', 'network': {'bridge': 'br-int'}}
        self.drv.unplug(vif)

        # The trunks and the cna should have been deleted
        self.assertTrue(t1.delete.called)
        self.assertTrue(t2.delete.called)
        self.assertTrue(mock_cna.delete.called)

        # Validate the OVS port delete call was made
        mock_del_ovs_port.assert_called_with('br-int', 'fake_dev')
