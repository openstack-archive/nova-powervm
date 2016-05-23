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


import mock

from nova import test

from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.tests.test_utils import pvmhttp
from pypowervm.wrappers import base_partition as pvm_bp
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.virt.powervm import exception as nova_pvm_exc
from nova_powervm.virt.powervm import vios

VIOS_FEED = 'fake_vios_feed2.txt'


class TestVios(test.TestCase):

    def setUp(self):
        super(TestVios, self).setUp()
        self.adpt = self.useFixture(pvm_fx.AdapterFx()).adpt

        def resp(file_name):
            return pvmhttp.load_pvm_resp(file_name).get_response()
        self.vios_feed_resp = resp(VIOS_FEED)

    def test_get_active_vioses(self):
        self.adpt.read.return_value = self.vios_feed_resp
        vioses = vios.get_active_vioses(self.adpt, 'host_uuid')
        self.assertEqual(1, len(vioses))

        vio = vioses[0]
        self.assertEqual(pvm_bp.LPARState.RUNNING, vio.state)
        self.assertEqual(pvm_bp.RMCState.ACTIVE, vio.rmc_state)
        self.adpt.read.assert_called_with(pvm_vios.VIOS.schema_type, xag=None)

    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.get')
    def test_get_active_vioses_w_vios_wraps(self, mock_get):
        mock_vios1 = mock.Mock(state='running', rmc_state='active')
        mock_vios2 = mock.Mock(state='running', rmc_state='inactive')
        vios_wraps = [mock_vios1, mock_vios2]

        vioses = vios.get_active_vioses(
            self.adpt, 'host_uuid', vios_wraps=vios_wraps)
        self.assertEqual(1, len(vioses))

        vio = vioses[0]
        self.assertEqual(pvm_bp.LPARState.RUNNING, vio.state)
        self.assertEqual(pvm_bp.RMCState.ACTIVE, vio.rmc_state)
        self.assertEqual(0, mock_get.call_count)

    def test_get_inactive_running_vioses(self):
        # No
        mock_vios1 = mock.Mock(
            state=pvm_bp.LPARState.NOT_ACTIVATED,
            rmc_state=pvm_bp.RMCState.INACTIVE)
        # No
        mock_vios2 = mock.Mock(
            state=pvm_bp.LPARState.RUNNING,
            rmc_state=pvm_bp.RMCState.BUSY)
        # Yes
        mock_vios3 = mock.Mock(
            state=pvm_bp.LPARState.RUNNING,
            rmc_state=pvm_bp.RMCState.UNKNOWN)
        # No
        mock_vios4 = mock.Mock(
            state=pvm_bp.LPARState.UNKNOWN,
            rmc_state=pvm_bp.RMCState.ACTIVE)
        # No
        mock_vios5 = mock.Mock(
            state=pvm_bp.LPARState.RUNNING,
            rmc_state=pvm_bp.RMCState.ACTIVE)
        # Yes
        mock_vios6 = mock.Mock(
            state=pvm_bp.LPARState.RUNNING,
            rmc_state=pvm_bp.RMCState.INACTIVE)

        self.assertEqual(
            {mock_vios6, mock_vios3}, set(vios.get_inactive_running_vioses(
                [mock_vios1, mock_vios2, mock_vios3, mock_vios4,
                 mock_vios5, mock_vios6])))
        mock_vios2 = mock.Mock()

    def test_get_physical_wwpns(self):
        self.adpt.read.return_value = self.vios_feed_resp
        expected = set(['21000024FF649104'])
        result = set(vios.get_physical_wwpns(self.adpt, 'fake_uuid'))
        self.assertSetEqual(expected, result)

    @mock.patch('retrying.retry')
    @mock.patch('nova_powervm.virt.powervm.vios.get_active_vioses')
    @mock.patch('nova_powervm.virt.powervm.vios.get_inactive_running_vioses')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.get')
    def test_is_vios_ready(self, mock_get, mock_get_inactive_vioses,
                           mock_get_active_vioses, mock_retry):
        adpt = mock.MagicMock()
        host_uuid = 'host_uuid'

        # Validates the retry method itself.
        def validate_retry(kwargs):
            self.assertIn('retry_on_result', kwargs)
            self.assertEqual(5000, kwargs['wait_fixed'])
            self.assertEqual(300000, kwargs['stop_max_delay'])

        # Used to simulate an eventual timeout.
        def retry_timeout(**kwargs):
            # First validate the retry.
            validate_retry(kwargs)

            def one_running_inactive_vio():
                mock_vios1 = mock.Mock()
                mock_vios1.configure_mock(name='vios1')
                return [mock_vios1]

            def wrapped(_poll_for_dev):
                return one_running_inactive_vio
            return wrapped

        # Validate that we will eventually timeout.
        mock_retry.side_effect = retry_timeout
        mock_get_active_vioses.return_value = [mock.Mock()]

        # Shouldn't raise an error because we have active vioses
        vios.validate_vios_ready(adpt, host_uuid)

        # Should raise an exception now because we timed out and there
        # weren't any active VIOSes
        mock_get_active_vioses.return_value = []
        self.assertRaises(
            nova_pvm_exc.ViosNotAvailable, vios.validate_vios_ready, adpt,
            host_uuid)

        # Now test where we pass through to the actual method in the retry.
        def retry_passthrough(**kwargs):
            validate_retry(kwargs)

            def wrapped(_poll_for_dev):
                return _poll_for_dev
            return wrapped

        def get_active_vioses_side_effect(*args, **kwargs):
            return kwargs['vios_wraps']

        mock_retry.side_effect = retry_passthrough

        # First run should succeed because all VIOSes should be active and
        # running
        mock_get.return_value = ['vios1', 'vios2', 'vios3', 'vios4']
        mock_get_inactive_vioses.return_value = []
        mock_get_active_vioses.side_effect = get_active_vioses_side_effect
        vios.validate_vios_ready(adpt, host_uuid)

        # Second run should fail because we raise an exception (which retries,
        # and then eventually times out with no active VIOSes)
        mock_get.reset_mock()
        mock_get.side_effect = Exception('testing error')
        mock_get_active_vioses.reset_mock()
        mock_get_active_vioses.return_value = []
        self.assertRaises(
            nova_pvm_exc.ViosNotAvailable, vios.validate_vios_ready, adpt,
            host_uuid)

        # Last run should succeed but raise a warning because there's
        # still inactive running VIOSes
        mock_vios1, mock_vios2 = mock.Mock(), mock.Mock()
        mock_vios1.configure_mock(name='vios1')
        mock_vios2.configure_mock(name='vios2')
        mock_get_inactive_vioses.return_value = [mock_vios1, mock_vios2]
        mock_get_active_vioses.reset_mock()
        mock_get_active_vioses.side_effect = ['vios1', 'vios2']
        vios.validate_vios_ready(adpt, host_uuid)
