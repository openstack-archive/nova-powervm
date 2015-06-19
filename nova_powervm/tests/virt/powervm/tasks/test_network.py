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

import eventlet
import mock

from nova import exception
from nova import objects
from nova import test
from pypowervm.wrappers import base_partition as pvm_bp

from nova_powervm.tests.virt import powervm
from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm.tasks import network as tf_net


def cna(mac):
    """Builds a mock Client Network Adapter for unit tests."""
    nic = mock.MagicMock()
    nic.mac = mac
    nic.vswitch_uri = 'fake_href'
    return nic


class TestNetwork(test.TestCase):
    def setUp(self):
        super(TestNetwork, self).setUp()
        self.pypvm = self.useFixture(fx.PyPowerVM())
        self.apt = self.pypvm.apt

    def test_state_ok_for_plug(self):
        # Happy paths
        mock_lpar = mock.MagicMock()
        mock_lpar.state = pvm_bp.LPARState.NOT_ACTIVATED
        self.assertTrue(tf_net.state_ok_for_plug(mock_lpar))

        mock_lpar.state = pvm_bp.LPARState.RUNNING
        mock_lpar.rmc_state = pvm_bp.RMCState.ACTIVE
        self.assertTrue(tf_net.state_ok_for_plug(mock_lpar))

        # Invalid paths
        mock_lpar.state = pvm_bp.LPARState.RUNNING
        mock_lpar.rmc_state = pvm_bp.RMCState.INACTIVE
        self.assertFalse(tf_net.state_ok_for_plug(mock_lpar))

        mock_lpar.state = pvm_bp.LPARState.MIGRATING_NOT_ACTIVE
        mock_lpar.rmc_state = pvm_bp.RMCState.ACTIVE
        self.assertFalse(tf_net.state_ok_for_plug(mock_lpar))

    @mock.patch('nova_powervm.virt.powervm.tasks.network.state_ok_for_plug')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_unplug_vifs(self, mock_vm_get, mock_state):
        """Tests that a delete of the vif can be done."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the CNA response.  One should already exist, the other
        # should not.
        cnas = [cna('AABBCCDDEEFF'), cna('AABBCCDDEE11'), cna('AABBCCDDEE22')]
        mock_vm_get.return_value = cnas

        # Mock up the network info.  This also validates that they will be
        # sanitized to upper case.
        net_info = [
            {'address': 'aa:bb:cc:dd:ee:ff'}, {'address': 'aa:bb:cc:dd:ee:22'},
            {'address': 'aa:bb:cc:dd:ee:33'}
        ]

        mock_state.return_value = True

        # Run method
        p_vifs = tf_net.UnplugVifs(self.apt, inst, net_info, 'host_uuid')
        p_vifs.execute(mock.Mock())

        # The delete should have only been called once.  The second CNA didn't
        # have a matching mac...so it should be skipped.
        self.assertEqual(1, cnas[0].delete.call_count)
        self.assertEqual(0, cnas[1].delete.call_count)
        self.assertEqual(1, cnas[2].delete.call_count)

    @mock.patch('nova_powervm.virt.powervm.tasks.network.state_ok_for_plug')
    def test_unplug_vifs_invalid_state(self, mock_state):
        """Tests that the delete raises an exception if bad VM state."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # State that the VM is not in a valid state for VIF unplug
        mock_state.return_value = False

        # Run method
        p_vifs = tf_net.UnplugVifs(self.apt, inst, mock.Mock(), 'host_uuid')
        self.assertRaises(tf_net.VirtualInterfaceUnplugException,
                          p_vifs.execute, mock.Mock)

    @mock.patch('nova_powervm.virt.powervm.tasks.network.state_ok_for_plug')
    @mock.patch('nova_powervm.virt.powervm.vm.crt_vif')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_plug_vifs_rmc(self, mock_vm_get, mock_vm_crt, mock_state):
        """Tests that a crt vif can be done with secure RMC."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the CNA response.  One should already exist, the other
        # should not.
        mock_vm_get.return_value = [cna('AABBCCDDEEFF'), cna('AABBCCDDEE11')]

        # Mock up the network info.  This also validates that they will be
        # sanitized to upper case.
        net_info = [
            {'address': 'aa:bb:cc:dd:ee:ff'}, {'address': 'aa:bb:cc:dd:ee:22'},
            {'address': 'aa:bb:cc:dd:ee:33'}
        ]

        mock_state.return_value = True

        # Run method
        p_vifs = tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                                 'host_uuid')
        p_vifs.execute(mock.Mock())

        # The create should have only been called once.
        self.assertEqual(2, mock_vm_crt.call_count)

    @mock.patch('nova_powervm.virt.powervm.tasks.network.state_ok_for_plug')
    @mock.patch('nova_powervm.virt.powervm.vm.crt_vif')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_plug_vifs_rmc_no_create(self, mock_vm_get, mock_vm_crt,
                                     mock_state):
        """Verifies if no creates are needed, none are done."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the CNA response.  Both should already exist.
        mock_vm_get.return_value = [cna('AABBCCDDEEFF'), cna('AABBCCDDEE11')]

        # Mock up the network info.  This also validates that they will be
        # sanitized to upper case.
        net_info = [
            {'address': 'aa:bb:cc:dd:ee:ff'}, {'address': 'aa:bb:cc:dd:ee:11'}
        ]

        # Run method
        p_vifs = tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                                 'host_uuid')
        resp = p_vifs.execute(mock.Mock())

        # The create should not have been called.  The response should have
        # been empty.
        self.assertEqual(0, mock_vm_crt.call_count)
        self.assertEqual([], resp)

        # State check shouldn't have even been invoked as no creates were
        # required
        self.assertEqual(0, mock_state.call_count)

    @mock.patch('nova_powervm.virt.powervm.tasks.network.state_ok_for_plug')
    @mock.patch('nova_powervm.virt.powervm.vm.crt_vif')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_plug_vifs_invalid_state(self, mock_vm_get, mock_vm_crt,
                                     mock_state):
        """Tests that a crt_vif fails when the LPAR state is bad."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the CNA response.  Only doing one for simplicity
        mock_vm_get.return_value = []
        net_info = [{'address': 'aa:bb:cc:dd:ee:ff'}]

        # Mock that the state is incorrect
        mock_state.return_value = False

        # Run method
        p_vifs = tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                                 'host_uuid')
        self.assertRaises(exception.VirtualInterfaceCreateException,
                          p_vifs.execute, mock.Mock())

        # The create should not have been invoked
        self.assertEqual(0, mock_vm_crt.call_count)

    @mock.patch('nova_powervm.virt.powervm.tasks.network.state_ok_for_plug')
    @mock.patch('nova_powervm.virt.powervm.vm.crt_vif')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_plug_vifs_timeout(self, mock_vm_get, mock_vm_crt, mock_state):
        """Tests that crt vif failure via loss of neutron callback."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the CNA response.  Only doing one for simplicity
        mock_vm_get.return_value = [cna('AABBCCDDEE11')]

        # Mock up the network info.
        net_info = [{'address': 'aa:bb:cc:dd:ee:ff'}]

        mock_state.return_value = True

        # Ensure that an exception is raised by a timeout.
        mock_vm_crt.side_effect = eventlet.timeout.Timeout()

        # Run method
        p_vifs = tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                                 'host_uuid')
        self.assertRaises(exception.VirtualInterfaceCreateException,
                          p_vifs.execute, mock.Mock())

        # The create should have only been called once.
        self.assertEqual(1, mock_vm_crt.call_count)

    @mock.patch('nova_powervm.virt.powervm.vm.crt_secure_rmc_vif')
    @mock.patch('nova_powervm.virt.powervm.vm.get_secure_rmc_vswitch')
    @mock.patch('nova_powervm.virt.powervm.vm.crt_vif')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_plug_mgmt_vif(self, mock_vm_get, mock_vm_crt,
                           mock_get_rmc_vswitch, mock_crt_rmc_vif):
        """Tests that a mgmt vif can be created."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the rmc vswitch
        vswitch_w = mock.MagicMock()
        vswitch_w.href = 'fake_mgmt_uri'
        mock_get_rmc_vswitch.return_value = vswitch_w

        # Run method
        p_vifs = tf_net.PlugMgmtVif(self.apt, inst, 'host_uuid')
        p_vifs.execute([])

        # The create should have only been called once.
        self.assertEqual(1, mock_crt_rmc_vif.call_count)

    @mock.patch('nova.utils.is_neutron')
    def test_get_vif_events(self, mock_is_neutron):
        # Set up common mocks.
        inst = objects.Instance(**powervm.TEST_INSTANCE)
        net_info = [mock.MagicMock(), mock.MagicMock()]
        net_info[0]['id'] = 'a'
        net_info[0].get.return_value = False
        net_info[1]['id'] = 'b'
        net_info[1].get.return_value = True

        # Set up the runner.
        p_vifs = tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                                 'host_uuid')

        # Mock that neutron is off.
        mock_is_neutron.return_value = False
        self.assertEqual([], p_vifs._get_vif_events())

        # Turn neutron on.
        mock_is_neutron.return_value = True
        resp = p_vifs._get_vif_events()

        # Only one should be returned since only one was active.
        self.assertEqual(1, len(resp))
