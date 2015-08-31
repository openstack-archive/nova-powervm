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


from nova import test
import os

from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.tests.wrappers.util import pvmhttp
from pypowervm.wrappers import base_partition as pvm_bp
from pypowervm.wrappers import managed_system as pvm_ms
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.virt.powervm import vios

VIOS_FEED = 'fake_vios_feed.txt'


class TestVios(test.TestCase):

    def setUp(self):
        super(TestVios, self).setUp()
        self.adpt = self.useFixture(pvm_fx.AdapterFx()).adpt

        # Find directory for response file(s)
        data_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(data_dir, 'data')

        def resp(file_name):
            file_path = os.path.join(data_dir, file_name)
            return pvmhttp.load_pvm_resp(file_path).get_response()
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
