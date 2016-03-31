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
#

import mock

from nova.cmd import conductor as nova_cond
from nova import test

from nova_powervm.cmd import conductor
from nova_powervm import objects


class TestConductor(test.TestCase):

    @mock.patch.object(objects, 'register_all')
    @mock.patch.object(nova_cond, 'main')
    def test_conductor(self, mock_main, mock_reg):

        # Call the main conductor
        conductor.main()
        mock_main.assert_called_once_with()
        mock_reg.assert_called_once_with()
