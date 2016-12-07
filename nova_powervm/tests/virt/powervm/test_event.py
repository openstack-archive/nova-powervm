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

from eventlet import greenthread
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


class TestPowerVMLifecycleEventHandler(test.TestCase):
    def setUp(self):
        super(TestPowerVMLifecycleEventHandler, self).setUp()
        self.mock_driver = mock.MagicMock()
        self.handler = event.PowerVMLifecycleEventHandler(self.mock_driver)

    def test_is_delay_event(self):
        non_delay_evts = [
            pvm_bp.LPARState.ERROR,
            pvm_bp.LPARState.OPEN_FIRMWARE,
            pvm_bp.LPARState.RUNNING,
            pvm_bp.LPARState.MIGRATING_NOT_ACTIVE,
            pvm_bp.LPARState.MIGRATING_RUNNING,
            pvm_bp.LPARState.HARDWARE_DISCOVERY,
            pvm_bp.LPARState.STARTING,
            pvm_bp.LPARState.UNKNOWN
        ]

        delay_evts = [
            pvm_bp.LPARState.NOT_ACTIVATED,
            pvm_bp.LPARState.SHUTTING_DOWN,
            pvm_bp.LPARState.SUSPENDING,
            pvm_bp.LPARState.RESUMING,
            pvm_bp.LPARState.NOT_AVAILBLE
        ]

        for non_delay_evt in non_delay_evts:
            self.assertFalse(self.handler._is_delay_event(non_delay_evt),
                             msg=non_delay_evt)

        for delay_evt in delay_evts:
            self.assertTrue(self.handler._is_delay_event(delay_evt),
                            msg=delay_evt)

    @mock.patch('nova_powervm.virt.powervm.event.'
                'PowerVMLifecycleEventHandler._register_delayed_event')
    @mock.patch('nova_powervm.virt.powervm.event.'
                'PowerVMLifecycleEventHandler._emit_event')
    def test_process(self, mock_emit, mock_reg_delay_evt):
        non_delay_evts = [
            pvm_bp.LPARState.ERROR,
            pvm_bp.LPARState.OPEN_FIRMWARE
        ]

        delay_evts = [
            pvm_bp.LPARState.NOT_ACTIVATED,
            pvm_bp.LPARState.SHUTTING_DOWN,
            pvm_bp.LPARState.RESUMING,
        ]

        for state in non_delay_evts + delay_evts:
            self.handler.process(mock.Mock(), state)

        self.assertEqual(mock_emit.call_count, 2)
        self.assertEqual(mock_reg_delay_evt.call_count, 3)

    @mock.patch('nova_powervm.virt.powervm.event.vm.translate_event')
    def test_emit_event_immed(self, mock_translate):
        mock_translate.return_value = 'test'
        mock_delayed = mock.MagicMock()
        mock_inst = mock.Mock()
        mock_inst.uuid = 'inst_uuid'
        self.handler._delayed_event_threads = {'inst_uuid': mock_delayed}

        self.handler._emit_event(pvm_bp.LPARState.RUNNING, mock_inst, True)

        self.assertEqual({}, self.handler._delayed_event_threads)
        self.mock_driver.emit_event.assert_called_once()
        mock_delayed.cancel.assert_called_once()

    @mock.patch('nova_powervm.virt.powervm.event.vm.translate_event')
    def test_emit_event_delayed(self, mock_translate):
        mock_translate.return_value = 'test'
        mock_delayed = mock.MagicMock()
        mock_inst = mock.Mock()
        mock_inst.uuid = 'inst_uuid'
        self.handler._delayed_event_threads = {'inst_uuid': mock_delayed}

        self.handler._emit_event(pvm_bp.LPARState.NOT_ACTIVATED, mock_inst,
                                 False)

        self.assertEqual({}, self.handler._delayed_event_threads)
        self.mock_driver.emit_event.assert_called_once()

    def test_emit_event_delayed_no_queue(self):
        mock_inst = mock.Mock()
        mock_inst.uuid = 'inst_uuid'
        self.handler._delayed_event_threads = {}

        self.handler._emit_event(pvm_bp.LPARState.NOT_ACTIVATED, mock_inst,
                                 False)

        self.assertFalse(self.mock_driver.emit_event.called)

    @mock.patch.object(greenthread, 'spawn_after')
    def test_register_delay_event(self, mock_spawn):
        mock_old_delayed, mock_new_delayed = mock.Mock(), mock.Mock()
        mock_spawn.return_value = mock_new_delayed

        mock_inst = mock.Mock()
        mock_inst.uuid = 'inst_uuid'
        self.handler._delayed_event_threads = {'inst_uuid': mock_old_delayed}

        self.handler._register_delayed_event(pvm_bp.LPARState.NOT_ACTIVATED,
                                             mock_inst)

        mock_old_delayed.cancel.assert_called_once()
        mock_spawn.assert_called_once_with(
            15, self.handler._emit_event, pvm_bp.LPARState.NOT_ACTIVATED,
            mock_inst, False)
        self.assertEqual({'inst_uuid': mock_new_delayed},
                         self.handler._delayed_event_threads)
