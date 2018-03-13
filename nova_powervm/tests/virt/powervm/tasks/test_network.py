# Copyright 2015, 2018 IBM Corp.
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

import copy
import eventlet
import mock

from nova import exception
from nova import objects
from nova import test
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.wrappers import iocard as pvm_card
from pypowervm.wrappers import network as pvm_net

from nova_powervm.tests.virt import powervm
from nova_powervm.virt.powervm.tasks import network as tf_net


def cna(mac):
    """Builds a mock Client Network Adapter (or VNIC) for unit tests."""
    nic = mock.MagicMock()
    nic.mac = mac
    nic.vswitch_uri = 'fake_href'
    return nic


class TestNetwork(test.NoDBTestCase):
    def setUp(self):
        super(TestNetwork, self).setUp()
        self.flags(host='host1')
        self.apt = self.useFixture(pvm_fx.AdapterFx()).adpt

        self.mock_lpar_wrap = mock.MagicMock()
        self.mock_lpar_wrap.can_modify_io.return_value = True, None

    @mock.patch('nova_powervm.virt.powervm.vif.unplug', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas', autospec=True)
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

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_net.UnplugVifs(self.apt, inst, net_info, 'host_uuid',
                              'slot_mgr')
        tf.assert_called_once_with(name='unplug_vifs', requires=['lpar_wrap'])

    def test_unplug_vifs_invalid_state(self):
        """Tests that the delete raises an exception if bad VM state."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock that the state is incorrect
        self.mock_lpar_wrap.can_modify_io.return_value = False, 'bad'

        # Run method
        p_vifs = tf_net.UnplugVifs(self.apt, inst, mock.Mock(), 'host_uuid',
                                   'slot_mgr')
        self.assertRaises(exception.VirtualInterfaceUnplugException,
                          p_vifs.execute, self.mock_lpar_wrap)

    @mock.patch('nova_powervm.virt.powervm.vif.plug', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_vnics', autospec=True)
    def test_plug_vifs_rmc(self, mock_vnic_get, mock_cna_get, mock_plug):
        """Tests that a crt vif can be done with secure RMC."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the CNA response.  One should already exist, the other
        # should not.
        pre_cnas = [cna('AABBCCDDEEFF'), cna('AABBCCDDEE11')]
        mock_cna_get.return_value = copy.deepcopy(pre_cnas)
        # Ditto VNIC response.
        mock_vnic_get.return_value = [cna('AABBCCDDEE33'), cna('AABBCCDDEE44')]

        # Mock up the network info.  This also validates that they will be
        # sanitized to upper case.
        net_info = [
            {'address': 'aa:bb:cc:dd:ee:ff', 'vnic_type': 'normal'},
            {'address': 'aa:bb:cc:dd:ee:22', 'vnic_type': 'normal'},
            {'address': 'aa:bb:cc:dd:ee:33', 'vnic_type': 'direct'},
            {'address': 'aa:bb:cc:dd:ee:55', 'vnic_type': 'direct'}
        ]

        # Both updates run first (one CNA, one VNIC); then the CNA create, then
        # the VNIC create.
        mock_new_cna = mock.Mock(spec=pvm_net.CNA)
        mock_new_vnic = mock.Mock(spec=pvm_card.VNIC)
        mock_plug.side_effect = ['upd_cna', 'upd_vnic',
                                 mock_new_cna, mock_new_vnic]

        # Run method
        p_vifs = tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                                 'host_uuid', 'slot_mgr')

        all_cnas = p_vifs.execute(self.mock_lpar_wrap)

        # new vif should be created twice.
        mock_plug.assert_any_call(self.apt, 'host_uuid', inst, net_info[0],
                                  'slot_mgr', new_vif=False)
        mock_plug.assert_any_call(self.apt, 'host_uuid', inst, net_info[1],
                                  'slot_mgr', new_vif=True)
        mock_plug.assert_any_call(self.apt, 'host_uuid', inst, net_info[2],
                                  'slot_mgr', new_vif=False)
        mock_plug.assert_any_call(self.apt, 'host_uuid', inst, net_info[3],
                                  'slot_mgr', new_vif=True)

        # The Task provides the list of original CNAs plus only CNAs that were
        # created.
        self.assertEqual(pre_cnas + [mock_new_cna], all_cnas)

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                            'host_uuid', 'slot_mgr')
        tf.assert_called_once_with(name='plug_vifs', provides='vm_cnas',
                                   requires=['lpar_wrap'])

    @mock.patch('nova_powervm.virt.powervm.vif.plug', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas', autospec=True)
    def test_plug_vifs_rmc_no_create(self, mock_vm_get, mock_plug):
        """Verifies if no creates are needed, none are done."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the CNA response.  Both should already exist.
        mock_vm_get.return_value = [cna('AABBCCDDEEFF'), cna('AABBCCDDEE11')]

        # Mock up the network info.  This also validates that they will be
        # sanitized to upper case.  This also validates that we don't call
        # get_vnics if no nets have vnic_type 'direct'.
        net_info = [
            {'address': 'aa:bb:cc:dd:ee:ff', 'vnic_type': 'normal'},
            {'address': 'aa:bb:cc:dd:ee:11', 'vnic_type': 'normal'}
        ]

        # Run method
        p_vifs = tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                                 'host_uuid', 'slot_mgr')
        p_vifs.execute(self.mock_lpar_wrap)

        # The create should have been called with new_vif as False.
        mock_plug.assert_called_with(
            self.apt, 'host_uuid', inst, net_info[1],
            'slot_mgr', new_vif=False)

    @mock.patch('nova_powervm.virt.powervm.vif.plug', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas', autospec=True)
    def test_plug_vifs_invalid_state(self, mock_vm_get, mock_plug):
        """Tests that a crt_vif fails when the LPAR state is bad."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the CNA response.  Only doing one for simplicity
        mock_vm_get.return_value = []
        net_info = [{'address': 'aa:bb:cc:dd:ee:ff', 'vnic_type': 'normal'}]

        # Mock that the state is incorrect
        self.mock_lpar_wrap.can_modify_io.return_value = False, 'bad'

        # Run method
        p_vifs = tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                                 'host_uuid', 'slot_mgr')
        self.assertRaises(exception.VirtualInterfaceCreateException,
                          p_vifs.execute, self.mock_lpar_wrap)

        # The create should not have been invoked
        self.assertEqual(0, mock_plug.call_count)

    @mock.patch('nova_powervm.virt.powervm.vif.plug', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas', autospec=True)
    def test_plug_vifs_timeout(self, mock_vm_get, mock_plug):
        """Tests that crt vif failure via loss of neutron callback."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the CNA response.  Only doing one for simplicity
        mock_vm_get.return_value = [cna('AABBCCDDEE11')]

        # Mock up the network info.
        net_info = [{'address': 'aa:bb:cc:dd:ee:ff', 'vnic_type': 'normal'}]

        # Ensure that an exception is raised by a timeout.
        mock_plug.side_effect = eventlet.timeout.Timeout()

        # Run method
        p_vifs = tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                                 'host_uuid', 'slot_mgr')
        self.assertRaises(exception.VirtualInterfaceCreateException,
                          p_vifs.execute, self.mock_lpar_wrap)

        # The create should have only been called once.
        self.assertEqual(1, mock_plug.call_count)

    @mock.patch('nova_powervm.virt.powervm.vif.plug', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas', autospec=True)
    def test_plug_vifs_diff_host(self, mock_vm_get, mock_plug):
        """Tests that crt vif handles bad inst.host value."""
        inst = powervm.TEST_INST1

        # Set this up as a different host from the inst.host
        self.flags(host='host2')

        # Mock up the CNA response.  Only doing one for simplicity
        mock_vm_get.return_value = [cna('AABBCCDDEE11')]

        # Mock up the network info.
        net_info = [{'address': 'aa:bb:cc:dd:ee:ff', 'vnic_type': 'normal'}]

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

    @mock.patch('nova_powervm.virt.powervm.vif.plug', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas', autospec=True)
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
        net_info = [{'address': 'aa:bb:cc:dd:ee:ff', 'vnic_type': 'normal'}]

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

    @mock.patch('nova_powervm.virt.powervm.vif.unplug', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vif.plug', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas', autospec=True)
    def test_plug_vifs_revert(self, mock_vm_get, mock_plug, mock_unplug):
        """Tests that the revert flow works properly."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Fake CNA list.  The one pre-existing VIF should *not* get reverted.
        cna_list = [cna('AABBCCDDEEFF'), cna('FFEEDDCCBBAA')]
        mock_vm_get.return_value = cna_list

        # Mock up the network info.  Three roll backs.
        net_info = [
            {'address': 'aa:bb:cc:dd:ee:ff', 'vnic_type': 'normal'},
            {'address': 'aa:bb:cc:dd:ee:22', 'vnic_type': 'normal'},
            {'address': 'aa:bb:cc:dd:ee:33', 'vnic_type': 'normal'}
        ]

        # Make sure we test raising an exception
        mock_unplug.side_effect = [exception.NovaException(), None]

        # Run method
        p_vifs = tf_net.PlugVifs(mock.MagicMock(), self.apt, inst, net_info,
                                 'host_uuid', 'slot_mgr')
        p_vifs.execute(self.mock_lpar_wrap)
        p_vifs.revert(self.mock_lpar_wrap, mock.Mock(), mock.Mock())

        # The unplug should be called twice.  The exception shouldn't stop the
        # second call.
        self.assertEqual(2, mock_unplug.call_count)

        # Make sure each call is invoked correctly.  The first plug was not a
        # new vif, so it should not be reverted.
        c2 = mock.call(self.apt, 'host_uuid', inst, net_info[1],
                       'slot_mgr', cna_w_list=cna_list)
        c3 = mock.call(self.apt, 'host_uuid', inst, net_info[2],
                       'slot_mgr', cna_w_list=cna_list)
        mock_unplug.assert_has_calls([c2, c3])

    @mock.patch('nova_powervm.virt.powervm.vif.plug_secure_rmc_vif',
                autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vif.get_secure_rmc_vswitch',
                autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vif.plug', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas', autospec=True)
    def test_plug_mgmt_vif(self, mock_vm_get, mock_plug,
                           mock_get_rmc_vswitch, mock_plug_rmc_vif):
        """Tests that a mgmt vif can be created."""
        inst = objects.Instance(**powervm.TEST_INSTANCE)

        # Mock up the rmc vswitch
        vswitch_w = mock.MagicMock()
        vswitch_w.href = 'fake_mgmt_uri'
        mock_get_rmc_vswitch.return_value = vswitch_w

        # Run method such that it triggers a fresh CNA search
        p_vifs = tf_net.PlugMgmtVif(self.apt, inst, 'host_uuid', 'slot_mgr')
        p_vifs.execute(None)

        # With the default get_cnas mock (which returns a Mock()), we think we
        # found an existing management CNA.
        self.assertEqual(0, mock_plug_rmc_vif.call_count)
        mock_vm_get.assert_called_once_with(
            self.apt, inst, vswitch_uri='fake_mgmt_uri')

        # Now mock get_cnas to return no hits
        mock_vm_get.reset_mock()
        mock_vm_get.return_value = []
        p_vifs.execute(None)

        # Get was called; and since it didn't have the mgmt CNA, so was plug.
        self.assertEqual(1, mock_plug_rmc_vif.call_count)
        mock_vm_get.assert_called_once_with(
            self.apt, inst, vswitch_uri='fake_mgmt_uri')

        # Now pass CNAs, but not the mgmt vif, "from PlugVifs"
        cnas = [mock.Mock(vswitch_uri='uri1'), mock.Mock(vswitch_uri='uri2')]
        mock_plug_rmc_vif.reset_mock()
        mock_vm_get.reset_mock()
        p_vifs.execute(cnas)

        # Get wasn't called, since the CNAs were passed "from PlugVifs"; but
        # since the mgmt vif wasn't included, plug was called.
        self.assertEqual(0, mock_vm_get.call_count)
        self.assertEqual(1, mock_plug_rmc_vif.call_count)

        # Finally, pass CNAs including the mgmt.
        cnas.append(mock.Mock(vswitch_uri='fake_mgmt_uri'))
        mock_plug_rmc_vif.reset_mock()
        p_vifs.execute(cnas)

        # Neither get nor plug was called.
        self.assertEqual(0, mock_vm_get.call_count)
        self.assertEqual(0, mock_plug_rmc_vif.call_count)

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_net.PlugMgmtVif(self.apt, inst, 'host_uuid', 'slot_mgr')
        tf.assert_called_once_with(name='plug_mgmt_vif', provides='mgmt_cna',
                                   requires=['vm_cnas'])

    def test_get_vif_events(self):
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
        p_vifs.crt_network_infos = net_info

        resp = p_vifs._get_vif_events()

        # Only one should be returned since only one was active.
        self.assertEqual(1, len(resp))
