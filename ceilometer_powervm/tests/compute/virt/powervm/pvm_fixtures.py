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

"""Generic fixtures for unit testing PowerVM and Ceilometer integration."""

import fixtures
import mock


class PyPowerVMMetrics(fixtures.Fixture):
    """Fixtures for the PowerVM Metrics."""

    def setUp(self):
        super(PyPowerVMMetrics, self).setUp()

        self._vm_metrics_patcher = mock.patch('pypowervm.tasks.monitor.util.'
                                              'LparMetricCache')
        self._ensure_ltm = mock.patch('pypowervm.tasks.monitor.util.'
                                      'ensure_ltm_monitors')
        self._uuid_converter = mock.patch('pypowervm.utils.uuid.'
                                          'convert_uuid_to_pvm')

        self.vm_metrics = self._vm_metrics_patcher.start()
        self.ensure_ltm = self._ensure_ltm.start()
        self.uuid_converter = self._uuid_converter.start()

        self.addCleanup(self._vm_metrics_patcher.stop)
        self.addCleanup(self._ensure_ltm.stop)
        self.addCleanup(self._uuid_converter.stop)
