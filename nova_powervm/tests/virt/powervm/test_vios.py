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
from pypowervm.wrappers import managed_system as pvm_ms
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
        self.adpt.read.assert_called_with(pvm_ms.System.schema_type,
                                          root_id='host_uuid',
                                          child_type=pvm_vios.VIOS.schema_type,
                                          xag=None)

    def test_get_physical_wwpns(self):
        self.adpt.read.return_value = self.vios_feed_resp
        expected = set(['21000024FF649104'])
        result = set(vios.get_physical_wwpns(self.adpt, 'fake_uuid'))
        self.assertSetEqual(expected, result)

    @mock.patch('retrying.retry')
    @mock.patch('nova_powervm.virt.powervm.vios.get_active_vioses')
    def test_is_vios_ready(self, mock_get_active_vioses, mock_retry):
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

            def zero_elems():
                return []

            def wrapped(_poll_for_dev):
                return zero_elems
            return wrapped

        # Validate that we will eventually timeout.
        mock_retry.side_effect = retry_timeout
        self.assertRaises(
            nova_pvm_exc.ViosNotAvailable, vios.validate_vios_ready, adpt,
            host_uuid)

        # Now test where we pass through to the actual method in the retry.
        def retry_passthrough(**kwargs):
            validate_retry(kwargs)

            def wrapped(_poll_for_dev):
                return _poll_for_dev
            return wrapped

        mock_retry.side_effect = retry_passthrough

        # First run should fail because we return no active VIOSes
        mock_get_active_vioses.side_effect = [[]]
        self.assertRaises(
            nova_pvm_exc.ViosNotAvailable, vios.validate_vios_ready, adpt,
            host_uuid)

        # Second run should fail because we raise an exception (which retries,
        # and then eventually times out)
        mock_get_active_vioses.side_effect = Exception('testing error')
        self.assertRaises(
            nova_pvm_exc.ViosNotAvailable, vios.validate_vios_ready, adpt,
            host_uuid)

        # Last run should succeed
        mock_get_active_vioses.side_effect = ['fake_vios']
        vios.validate_vios_ready(adpt, host_uuid)
