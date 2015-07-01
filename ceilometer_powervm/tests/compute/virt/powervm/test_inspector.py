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

from oslotest import base
from pypowervm.tests import test_fixtures as api_fx

from ceilometer_powervm.compute.virt.powervm import inspector as p_inspect
from ceilometer_powervm.tests.compute.virt.powervm import pvm_fixtures


class TestPowerVMInspector(base.BaseTestCase):

    def setUp(self):
        super(TestPowerVMInspector, self).setUp()

        # These fixtures allow for stand up of the unit tests that use
        # pypowervm.
        self.useFixture(api_fx.AdapterFx())
        pvm_mon_fx = self.useFixture(pvm_fixtures.PyPowerVMMetrics())

        # Individual test cases will set return values on the metrics that
        # come back from pypowervm.
        self.mock_metrics = pvm_mon_fx.vm_metrics

        with mock.patch('ceilometer_powervm.compute.virt.powervm.inspector.'
                        'PowerVMInspector._get_host_uuid'):
            # Create the inspector
            self.inspector = p_inspect.PowerVMInspector()

    def test_sample(self):
        """Baseline for future test.

        This is a temporary test to ensure that the setUp works.  Will be
        removed once a real test is in place.
        """
        # TODO(thorst) Add test cases as new function is implemented.
        pass
