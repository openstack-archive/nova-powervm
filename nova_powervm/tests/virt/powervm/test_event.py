# Copyright 2014, 2016 IBM Corp.
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

from __future__ import absolute_import

import logging
import mock
from nova import test
from pypowervm.wrappers import base_partition as pvm_bp

from nova_powervm.tests.virt import powervm
from nova_powervm.virt.powervm import event
from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)
logging.basicConfig()


class TestPowerVMNovaEventHandler(test.TestCase):
    def setUp(self):
        super(TestPowerVMNovaEventHandler, self).setUp()
        self.mock_driver = mock.Mock()
        self.handler = event.PowerVMNovaEventHandler(self.mock_driver)

    @mock.patch('nova.context.get_admin_context', mock.MagicMock())
    @mock.patch.object(vm, 'get_instance')
    @mock.patch.object(vm, 'get_vm_qp')
    def test_events(self, mock_qprops, mock_get_inst):
        # Test events
        event_data = [
            mock.Mock(etype='NEW_CLIENT', data='', eid='1452692619554',
                      detail=''),
            mock.Mock(etype='MODIFY_URI', detail='Other', eid='1452692619555',
                      data='http://localhost:12080/rest/api/uom/Managed'
                           'System/c889bf0d-9996-33ac-84c5-d16727083a77'),
            mock.Mock(etype='MODIFY_URI', detail='ReferenceCode,Other',
                      eid='1452692619563',
                      data='http://localhost:12080/rest/api/uom/Managed'
                           'System/c889bf0d-9996-33ac-84c5-d16727083a77/'
                           'LogicalPartition/794654F5-B6E9-4A51-BEC2-'
                           'A73E41EAA938'),
            mock.Mock(etype='MODIFY_URI',
                      detail='RMCState,PartitionState,Other',
                      eid='1452692619566',
                      data='http://localhost:12080/rest/api/uom/Managed'
                           'System/c889bf0d-9996-33ac-84c5-d16727083a77/'
                           'LogicalPartition/794654F5-B6E9-4A51-BEC2-'
                           'A73E41EAA938'),
            mock.Mock(etype='MODIFY_URI',
                      detail='NVRAM',
                      eid='1452692619566',
                      data='http://localhost:12080/rest/api/uom/Managed'
                           'System/c889bf0d-9996-33ac-84c5-d16727083a77/'
                           'LogicalPartition/794654F5-B6E9-4A51-BEC2-'
                           'A73E41EAA938'),
        ]

        mock_qprops.return_value = pvm_bp.LPARState.RUNNING
        mock_get_inst.return_value = powervm.TEST_INST1

        self.handler.process(event_data)
        mock_get_inst.assert_called_once_with(mock.ANY, '794654F5-B6E9-'
                                              '4A51-BEC2-A73E41EAA938')

        self.assertTrue(self.mock_driver.emit_event.called)
        self.assertTrue(self.mock_driver.nvram_mgr.store.called)
