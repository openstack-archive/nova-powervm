# Copyright 2015, 2016 IBM Corp.
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
from pypowervm.tests import test_fixtures as pvm_fx

from nova_powervm.tests.virt import powervm
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
        self.flags(host='host1')
        self.apt = self.useFixture(pvm_fx.AdapterFx()).adpt

        self.mock_lpar_wrap = mock.MagicMock()
        self.mock_lpar_wrap.can_modify_io.return_value = True, None

    @mock.patch('nova_powervm.virt.powervm.vif.unplug')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_unplug_vifs(self, mock_vm_get, mock_unplug):
        """Tests that a delete of the vif can be done."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the CNA responses.
        cnas = [cna('AABBCCDDEEFF'), cna('AABBCCDDEE11'), cna('AABBCCDDEE22')]
        mock_vm_get.return_value = cnas

        # Mock up the network info.  This also validates that they will be
        # sanitized to upper case.
        net_info = [
            {'address': 'aa:bb:cc:dd:ee:ff'}, {'address': 'aa:bb:cc:dd:ee:22'},
            {'address': 'aa:bb:cc:dd:ee:33'}
        ]

        # Mock out the vif driver
        def validate_unplug(adapter, host_uuid, instance, vif,
                            slot_mgr, cna_w_list=None):
            self.assertEqual(adapter, self.apt)
            self.assertEqual('host_uuid', host_uuid)
            self.assertEqual(instance, inst)
            self.assertIn(vif, net_info)
            self.assertEqual('slot_mgr', slot_mgr)
            self.assertEqual(cna_w_list, cnas)

        mock_unplug.side_effect = validate_unplug

        # Run method
        p_vifs = tf_net.UnplugVifs(self.apt, inst, net_info, 'host_uuid',
                                   'slot_mgr')
        p_vifs.execute(self.mock_lpar_wrap)

        # Make sure the unplug was invoked, so that we know that the validation
        # code was called
        self.assertEqual(3, mock_unplug.call_count)

    def test_unplug_vifs_invalid_state(self):
        """Tests that the delete raises an exception if bad VM state."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock that the state is incorrect
        self.mock_lpar_wrap.can_modify_io.return_value = False, 'bad'

        # Run method
        p_vifs = tf_net.UnplugVifs(self.apt, inst, mock.Mock(), 'host_uuid',
                                   'slot_mgr')
        self.assertRaises(tf_net.VirtualInterfaceUnplugException,
                          p_vifs.execute, self.mock_lpar_wrap)

    @mock.patch('nova_powervm.virt.powervm.vif.plug')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_plug_vifs_rmc(self, mock_vm_get, mock_plug):
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

        # Run method
        p_vifs = tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                                 'host_uuid', 'slot_mgr')
        p_vifs.execute(self.mock_lpar_wrap)

        # The create should have only been called once.
        self.assertEqual(2, mock_plug.call_count)

    @mock.patch('nova_powervm.virt.powervm.vif.plug')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_plug_vifs_rmc_no_create(self, mock_vm_get, mock_plug):
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
                                 'host_uuid', 'slot_mgr')
        resp = p_vifs.execute(self.mock_lpar_wrap)

        # The create should not have been called.  The response should have
        # been empty.
        self.assertEqual(0, mock_plug.call_count)
        self.assertEqual([], resp)

        # State check shouldn't have even been invoked as no creates were
        # required
        self.assertEqual(0, self.mock_lpar_wrap.can_modify_io.call_count)

    @mock.patch('nova_powervm.virt.powervm.vif.plug')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_plug_vifs_invalid_state(self, mock_vm_get, mock_plug):
        """Tests that a crt_vif fails when the LPAR state is bad."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the CNA response.  Only doing one for simplicity
        mock_vm_get.return_value = []
        net_info = [{'address': 'aa:bb:cc:dd:ee:ff'}]

        # Mock that the state is incorrect
        self.mock_lpar_wrap.can_modify_io.return_value = False, 'bad'

        # Run method
        p_vifs = tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                                 'host_uuid', 'slot_mgr')
        self.assertRaises(exception.VirtualInterfaceCreateException,
                          p_vifs.execute, self.mock_lpar_wrap)

        # The create should not have been invoked
        self.assertEqual(0, mock_plug.call_count)

    @mock.patch('nova_powervm.virt.powervm.vif.plug')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_plug_vifs_timeout(self, mock_vm_get, mock_plug):
        """Tests that crt vif failure via loss of neutron callback."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the CNA response.  Only doing one for simplicity
        mock_vm_get.return_value = [cna('AABBCCDDEE11')]

        # Mock up the network info.
        net_info = [{'address': 'aa:bb:cc:dd:ee:ff'}]

        # Ensure that an exception is raised by a timeout.
        mock_plug.side_effect = eventlet.timeout.Timeout()

        # Run method
        p_vifs = tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                                 'host_uuid', 'slot_mgr')
        self.assertRaises(exception.VirtualInterfaceCreateException,
                          p_vifs.execute, self.mock_lpar_wrap)

        # The create should have only been called once.
        self.assertEqual(1, mock_plug.call_count)

    @mock.patch('nova_powervm.virt.powervm.vif.plug')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_plug_vifs_diff_host(self, mock_vm_get, mock_plug):
        """Tests that crt vif handles bad inst.host value."""
        inst = powervm.TEST_INST1

        # Set this up as a different host from the inst.host
        self.flags(host='host2')

        # Mock up the CNA response.  Only doing one for simplicity
        mock_vm_get.return_value = [cna('AABBCCDDEE11')]

        # Mock up the network info.
        net_info = [{'address': 'aa:bb:cc:dd:ee:ff'}]

        # Run method
        p_vifs = tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                                 'host_uuid', 'slot_mgr')
        with mock.patch.object(inst, 'save') as mock_inst_save:
            p_vifs.execute(self.mock_lpar_wrap)

        # The create should have only been called once.
        self.assertEqual(1, mock_plug.call_count)
        # Should have called save to save the new host and then changed it back
        self.assertEqual(2, mock_inst_save.call_count)
        self.assertEqual('host1', inst.host)

    @mock.patch('nova_powervm.virt.powervm.vif.plug')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_plug_vifs_diff_host_except(self, mock_vm_get, mock_plug):
        """Tests that crt vif handles bad inst.host value.

        This test ensures that if we get a timeout exception we still reset
        the inst.host value back to the original value
        """
        inst = powervm.TEST_INST1

        # Set this up as a different host from the inst.host
        self.flags(host='host2')

        # Mock up the CNA response.  Only doing one for simplicity
        mock_vm_get.return_value = [cna('AABBCCDDEE11')]

        # Mock up the network info.
        net_info = [{'address': 'aa:bb:cc:dd:ee:ff'}]

        # Ensure that an exception is raised by a timeout.
        mock_plug.side_effect = eventlet.timeout.Timeout()

        # Run method
        p_vifs = tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                                 'host_uuid', 'slot_mgr')
        with mock.patch.object(inst, 'save') as mock_inst_save:
            self.assertRaises(exception.VirtualInterfaceCreateException,
                              p_vifs.execute, self.mock_lpar_wrap)

        # The create should have only been called once.
        self.assertEqual(1, mock_plug.call_count)
        # Should have called save to save the new host and then changed it back
        self.assertEqual(2, mock_inst_save.call_count)
        self.assertEqual('host1', inst.host)

    @mock.patch('nova_powervm.virt.powervm.vif.unplug')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_plug_vifs_revert(self, mock_vm_get, mock_unplug):
        """Tests that the revert flow works properly."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Make a fake CNA list.  No real data needed, as the thing it calls
        # into is mocked.
        cna_list = []
        mock_vm_get.return_value = cna_list

        # Mock up the network info.  Three roll backs.
        net_info = [
            {'address': 'aa:bb:cc:dd:ee:ff'}, {'address': 'aa:bb:cc:dd:ee:22'},
            {'address': 'aa:bb:cc:dd:ee:33'}
        ]

        # Make sure we test raising an exception
        mock_unplug.side_effect = [None, exception.NovaException(), None]

        # Run method
        p_vifs = tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                                 'host_uuid', 'slot_mgr')
        p_vifs.revert(self.mock_lpar_wrap, mock.Mock(), mock.Mock())

        # The unplug should be called three times.  The exception shouldn't
        # stop the other calls.
        self.assertEqual(3, mock_unplug.call_count)

        # Make sure each call is invoked correctly.
        c1 = mock.call(self.apt, 'host_uuid', inst, net_info[0],
                       'slot_mgr', cna_w_list=cna_list)
        c2 = mock.call(self.apt, 'host_uuid', inst, net_info[1],
                       'slot_mgr', cna_w_list=cna_list)
        c3 = mock.call(self.apt, 'host_uuid', inst, net_info[2],
                       'slot_mgr', cna_w_list=cna_list)
        mock_unplug.assert_has_calls([c1, c2, c3])

    @mock.patch('nova_powervm.virt.powervm.vif.plug_secure_rmc_vif')
    @mock.patch('nova_powervm.virt.powervm.vif.get_secure_rmc_vswitch')
    @mock.patch('nova_powervm.virt.powervm.vif.plug')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_plug_mgmt_vif(self, mock_vm_get, mock_plug,
                           mock_get_rmc_vswitch, mock_plug_rmc_vif):
        """Tests that a mgmt vif can be created."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the rmc vswitch
        vswitch_w = mock.MagicMock()
        vswitch_w.href = 'fake_mgmt_uri'
        mock_get_rmc_vswitch.return_value = vswitch_w

        # Run method
        p_vifs = tf_net.PlugMgmtVif(self.apt, inst, 'host_uuid', 'slot_mgr')
        p_vifs.execute([])

        # The create should have only been called once.
        self.assertEqual(1, mock_plug_rmc_vif.call_count)

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
                                 'host_uuid', 'slot_mgr')

        # Mock that neutron is off.
        mock_is_neutron.return_value = False
        self.assertEqual([], p_vifs._get_vif_events())

        # Turn neutron on.
        mock_is_neutron.return_value = True
        resp = p_vifs._get_vif_events()

        # Only one should be returned since only one was active.
        self.assertEqual(1, len(resp))
