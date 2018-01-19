# Copyright 2014, 2017 IBM Corp.
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
from nova.compute import power_state
from nova import exception
from nova import test
from pypowervm.wrappers import event as pvm_evt

from nova_powervm.virt.powervm import event


class TestGetInstance(test.NoDBTestCase):
    @mock.patch('nova.context.get_admin_context', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_instance', autospec=True)
    def test_get_instance(self, mock_get_inst, mock_get_context):
        # If instance provided, vm.get_instance not called
        self.assertEqual('inst', event._get_instance('inst', 'uuid'))
        self.assertEqual(0, mock_get_inst.call_count)
        # Note that we can only guarantee get_admin_context wasn't called
        # because _get_instance is mocked everywhere else in this suite.
        # Otherwise it could run from another test case executing in parallel.
        self.assertEqual(0, mock_get_context.call_count)

        # If instance not provided, vm.get_instance is called
        mock_get_inst.return_value = 'inst2'
        for _ in range(2):
            # Doing it the second time doesn't call get_admin_context() again.
            self.assertEqual('inst2', event._get_instance(None, 'uuid'))
            mock_get_context.assert_called_once_with()
            mock_get_inst.assert_called_once_with(
                mock_get_context.return_value, 'uuid')
            mock_get_inst.reset_mock()
            # Don't reset mock_get_context


class TestPowerVMNovaEventHandler(test.NoDBTestCase):
    def setUp(self):
        super(TestPowerVMNovaEventHandler, self).setUp()
        lceh_process_p = mock.patch(
            'nova_powervm.virt.powervm.event.PowerVMLifecycleEventHandler.'
            'process')
        self.addCleanup(lceh_process_p.stop)
        self.mock_lceh_process = lceh_process_p.start()
        self.mock_driver = mock.Mock()
        self.handler = event.PowerVMNovaEventHandler(self.mock_driver)

    @mock.patch('nova_powervm.virt.powervm.event._get_instance', autospec=True)
    def test_get_inst_uuid(self, mock_get_instance):
        fake_inst1 = mock.Mock(uuid='uuid1')
        fake_inst2 = mock.Mock(uuid='uuid2')
        mock_get_instance.side_effect = lambda i, u: {
            'fake_pvm_uuid1': fake_inst1,
            'fake_pvm_uuid2': fake_inst2}.get(u)

        self.assertEqual(
            (fake_inst1, 'uuid1'),
            self.handler._get_inst_uuid(fake_inst1, 'fake_pvm_uuid1'))
        self.assertEqual(
            (fake_inst2, 'uuid2'),
            self.handler._get_inst_uuid(fake_inst2, 'fake_pvm_uuid2'))
        self.assertEqual(
            (None, 'uuid1'),
            self.handler._get_inst_uuid(None, 'fake_pvm_uuid1'))
        self.assertEqual(
            (fake_inst2, 'uuid2'),
            self.handler._get_inst_uuid(fake_inst2, 'fake_pvm_uuid2'))
        self.assertEqual(
            (fake_inst1, 'uuid1'),
            self.handler._get_inst_uuid(fake_inst1, 'fake_pvm_uuid1'))
        mock_get_instance.assert_has_calls(
            [mock.call(fake_inst1, 'fake_pvm_uuid1'),
             mock.call(fake_inst2, 'fake_pvm_uuid2')])

    @mock.patch('nova_powervm.virt.powervm.event._get_instance', autospec=True)
    def test_handle_inst_event(self, mock_get_instance):
        # If no event we care about, or NVRAM but no nvram_mgr, nothing happens
        self.mock_driver.nvram_mgr = None
        for dets in ([], ['foo', 'bar', 'baz'], ['NVRAM']):
            self.assertEqual('inst', self.handler._handle_inst_event(
                'inst', 'uuid', dets))
        self.assertEqual(0, mock_get_instance.call_count)
        self.mock_lceh_process.assert_not_called()

        self.mock_driver.nvram_mgr = mock.Mock()

        # PartitionState only: no NVRAM handling, and inst is passed through.
        self.assertEqual('inst', self.handler._handle_inst_event(
            'inst', 'uuid', ['foo', 'PartitionState', 'bar']))
        self.assertEqual(0, mock_get_instance.call_count)
        self.mock_driver.nvram_mgr.store.assert_not_called()
        self.mock_lceh_process.assert_called_once_with('inst', 'uuid')

        self.mock_lceh_process.reset_mock()

        # No instance; nothing happens (we skip PartitionState handling too)
        mock_get_instance.return_value = None
        self.assertIsNone(self.handler._handle_inst_event(
            'inst', 'uuid', ['NVRAM', 'PartitionState']))
        mock_get_instance.assert_called_once_with('inst', 'uuid')
        self.mock_driver.nvram_mgr.store.assert_not_called()
        self.mock_lceh_process.assert_not_called()

        mock_get_instance.reset_mock()
        fake_inst = mock.Mock(uuid='fake-uuid')
        mock_get_instance.return_value = fake_inst

        # NVRAM only - no PartitionState handling, instance is returned
        self.assertEqual(fake_inst, self.handler._handle_inst_event(
            None, 'uuid', ['NVRAM', 'baz']))
        mock_get_instance.assert_called_once_with(None, 'uuid')
        self.mock_driver.nvram_mgr.store.assert_called_once_with('fake-uuid')
        self.mock_lceh_process.assert_not_called()

        mock_get_instance.reset_mock()
        self.mock_driver.nvram_mgr.store.reset_mock()
        self.handler._uuid_cache.clear()

        # Both event types
        self.assertEqual(fake_inst, self.handler._handle_inst_event(
            None, 'uuid', ['PartitionState', 'NVRAM']))
        mock_get_instance.assert_called_once_with(None, 'uuid')
        self.mock_driver.nvram_mgr.store.assert_called_once_with('fake-uuid')
        self.mock_lceh_process.assert_called_once_with(fake_inst, 'uuid')

        mock_get_instance.reset_mock()
        self.mock_driver.nvram_mgr.store.reset_mock()
        self.handler._uuid_cache.clear()

        # Handle multiple NVRAM and PartitionState events
        self.assertEqual(fake_inst, self.handler._handle_inst_event(
            None, 'uuid', ['NVRAM']))
        self.assertEqual(None, self.handler._handle_inst_event(
            None, 'uuid', ['NVRAM']))
        self.assertEqual(None, self.handler._handle_inst_event(
            None, 'uuid', ['PartitionState']))
        self.assertEqual(fake_inst, self.handler._handle_inst_event(
            fake_inst, 'uuid', ['NVRAM']))
        self.assertEqual(fake_inst, self.handler._handle_inst_event(
            fake_inst, 'uuid', ['NVRAM', 'PartitionState']))
        mock_get_instance.assert_called_once_with(None, 'uuid')
        self.mock_driver.nvram_mgr.store.assert_has_calls(
            [mock.call('fake-uuid')] * 4)
        self.mock_lceh_process.assert_has_calls(
            [mock.call(None, 'uuid'),
             mock.call(fake_inst, 'uuid')])

    @mock.patch('nova_powervm.virt.powervm.event.PowerVMNovaEventHandler.'
                '_handle_inst_event')
    @mock.patch('pypowervm.util.get_req_path_uuid', autospec=True)
    def test_process(self, mock_get_rpu, mock_handle):
        # NEW_CLIENT/CACHE_CLEARED events are ignored
        events = [mock.Mock(etype=pvm_evt.EventType.NEW_CLIENT),
                  mock.Mock(etype=pvm_evt.EventType.CACHE_CLEARED)]
        self.handler.process(events)
        self.assertEqual(0, mock_get_rpu.call_count)
        mock_handle.assert_not_called()

        moduri = pvm_evt.EventType.MODIFY_URI
        # If get_req_path_uuid doesn't find a UUID, or not a LogicalPartition
        # URI, or details is empty, or has no actions we care about, no action
        # is taken.
        mock_get_rpu.side_effect = [None, 'uuid1', 'uuid2', 'uuid3']
        events = [
            mock.Mock(etype=moduri, data='foo/LogicalPartition/None',
                      details='NVRAM,PartitionState'),
            mock.Mock(etype=moduri, data='bar/VirtualIOServer/uuid1',
                      details='NVRAM,PartitionState'),
            mock.Mock(etype=moduri, data='baz/LogicalPartition/uuid2',
                      detail=''),
            mock.Mock(etype=moduri, data='blah/LogicalPartition/uuid3',
                      detail='do,not,care')]
        self.handler.process(events)
        mock_get_rpu.assert_has_calls(
            [mock.call(uri, preserve_case=True)
             for uri in ('bar/VirtualIOServer/uuid1',
                         'baz/LogicalPartition/uuid2',
                         'blah/LogicalPartition/uuid3')])
        mock_handle.assert_not_called()

        mock_get_rpu.reset_mock()

        # The stars align, and we handle some events.
        uuid_det = (('uuid1', 'NVRAM'),
                    ('uuid2', 'this,one,ignored'),
                    ('uuid3', 'PartitionState,baz,NVRAM'),
                    # Repeat uuid1 to test the cache
                    ('uuid1', 'blah,PartitionState'),
                    ('uuid5', 'also,ignored'))
        mock_get_rpu.side_effect = [ud[0] for ud in uuid_det]
        events = [
            mock.Mock(etype=moduri, data='LogicalPartition/' + uuid,
                      detail=detail) for uuid, detail in uuid_det]
        # Set up _handle_inst_event to test the cache and the exception path
        mock_handle.side_effect = ['inst1', None, ValueError]
        # Run it!
        self.handler.process(events)
        mock_get_rpu.assert_has_calls(
            [mock.call(uri, preserve_case=True) for uri in
             ('LogicalPartition/' + ud[0] for ud in uuid_det)])
        mock_handle.assert_has_calls(
            [mock.call(None, 'uuid1', ['NVRAM']),
             mock.call(None, 'uuid3', ['PartitionState', 'baz', 'NVRAM']),
             # inst1 pulled from the cache based on uuid1
             mock.call('inst1', 'uuid1', ['blah', 'PartitionState'])])

    @mock.patch('nova_powervm.virt.powervm.event._get_instance', autospec=True)
    @mock.patch('pypowervm.util.get_req_path_uuid', autospec=True)
    def test_uuid_cache(self, mock_get_rpu, mock_get_instance):
        deluri = pvm_evt.EventType.DELETE_URI
        moduri = pvm_evt.EventType.MODIFY_URI

        fake_inst1 = mock.Mock(uuid='uuid1')
        fake_inst2 = mock.Mock(uuid='uuid2')
        fake_inst4 = mock.Mock(uuid='uuid4')
        mock_get_instance.side_effect = lambda i, u: {
            'fake_pvm_uuid1': fake_inst1,
            'fake_pvm_uuid2': fake_inst2,
            'fake_pvm_uuid4': fake_inst4}.get(u)
        mock_get_rpu.side_effect = lambda d, **k: d.split('/')[1]

        uuid_det = (('fake_pvm_uuid1', 'NVRAM', moduri),
                    ('fake_pvm_uuid2', 'NVRAM', moduri),
                    ('fake_pvm_uuid4', 'NVRAM', moduri),
                    ('fake_pvm_uuid1', 'NVRAM', moduri),
                    ('fake_pvm_uuid2', '', deluri),
                    ('fake_pvm_uuid2', 'NVRAM', moduri),
                    ('fake_pvm_uuid1', '', deluri),
                    ('fake_pvm_uuid3', '', deluri))
        events = [
            mock.Mock(etype=etype, data='LogicalPartition/' + uuid,
                      detail=detail) for uuid, detail, etype in uuid_det]
        self.handler.process(events[0:4])
        mock_get_instance.assert_has_calls([
            mock.call(None, 'fake_pvm_uuid1'),
            mock.call(None, 'fake_pvm_uuid2'),
            mock.call(None, 'fake_pvm_uuid4')])
        self.assertEqual({
            'fake_pvm_uuid1': 'uuid1',
            'fake_pvm_uuid2': 'uuid2',
            'fake_pvm_uuid4': 'uuid4'}, self.handler._uuid_cache)

        mock_get_instance.reset_mock()

        # Test the cache with a second process call
        self.handler.process(events[4:7])
        mock_get_instance.assert_has_calls([
            mock.call(None, 'fake_pvm_uuid2')])
        self.assertEqual({
            'fake_pvm_uuid2': 'uuid2',
            'fake_pvm_uuid4': 'uuid4'}, self.handler._uuid_cache)

        mock_get_instance.reset_mock()

        # Make sure a delete to a non-cached UUID doesn't blow up
        self.handler.process([events[7]])
        self.assertEqual(0, mock_get_instance.call_count)

        mock_get_rpu.reset_mock()
        mock_get_instance.reset_mock()

        clear_events = [mock.Mock(etype=pvm_evt.EventType.NEW_CLIENT),
                        mock.Mock(etype=pvm_evt.EventType.CACHE_CLEARED)]
        # This should clear the cache
        self.handler.process(clear_events)
        self.assertEqual(dict(), self.handler._uuid_cache)
        self.assertEqual(0, mock_get_rpu.call_count)
        self.assertEqual(0, mock_get_instance.call_count)


class TestPowerVMLifecycleEventHandler(test.NoDBTestCase):
    def setUp(self):
        super(TestPowerVMLifecycleEventHandler, self).setUp()
        self.mock_driver = mock.MagicMock()
        self.handler = event.PowerVMLifecycleEventHandler(self.mock_driver)

    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_qp', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.event._get_instance', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.translate_event', autospec=True)
    @mock.patch('nova.virt.event.LifecycleEvent', autospec=True)
    def test_emit_event(self, mock_lce, mock_tx_evt, mock_get_inst, mock_qp):
        def assert_qp():
            mock_qp.assert_called_once_with(
                self.mock_driver.adapter, 'uuid', 'PartitionState')
            mock_qp.reset_mock()

        def assert_get_inst():
            mock_get_inst.assert_called_once_with('inst', 'uuid')
            mock_get_inst.reset_mock()

        # Ignore if LPAR is gone
        mock_qp.side_effect = exception.InstanceNotFound(instance_id='uuid')
        self.handler._emit_event('uuid', None)
        assert_qp()
        self.assertEqual(0, mock_get_inst.call_count)
        self.assertEqual(0, mock_tx_evt.call_count)
        self.assertEqual(0, mock_lce.call_count)
        self.mock_driver.emit_event.assert_not_called()

        # Let get_vm_qp return its usual mock from now on
        mock_qp.side_effect = None

        # Ignore if instance is gone
        mock_get_inst.return_value = None
        self.handler._emit_event('uuid', 'inst')
        assert_qp()
        assert_get_inst()
        self.assertEqual(0, mock_tx_evt.call_count)
        self.assertEqual(0, mock_lce.call_count)
        self.mock_driver.emit_event.assert_not_called()

        # Ignore if task_state isn't one we care about
        for task_state in event._NO_EVENT_TASK_STATES:
            mock_get_inst.return_value = mock.Mock(task_state=task_state)
            self.handler._emit_event('uuid', 'inst')
            assert_qp()
            assert_get_inst()
            self.assertEqual(0, mock_tx_evt.call_count)
            self.assertEqual(0, mock_lce.call_count)
            self.mock_driver.emit_event.assert_not_called()

        # Task state we care about from now on
        inst = mock.Mock(task_state='scheduling',
                         power_state=power_state.RUNNING)
        mock_get_inst.return_value = inst

        # Ignore if not a transition we care about
        mock_tx_evt.return_value = None
        self.handler._emit_event('uuid', 'inst')
        assert_qp()
        assert_get_inst()
        mock_tx_evt.assert_called_once_with(
            mock_qp.return_value, power_state.RUNNING)
        mock_lce.assert_not_called()
        self.mock_driver.emit_event.assert_not_called()

        mock_tx_evt.reset_mock()

        # Good path
        mock_tx_evt.return_value = 'transition'
        self.handler._delayed_event_threads = {'uuid': 'thread1',
                                               'uuid2': 'thread2'}
        self.handler._emit_event('uuid', 'inst')
        assert_qp()
        assert_get_inst()
        mock_tx_evt.assert_called_once_with(
            mock_qp.return_value, power_state.RUNNING)
        mock_lce.assert_called_once_with(inst.uuid, 'transition')
        self.mock_driver.emit_event.assert_called_once_with(
            mock_lce.return_value)
        # The thread was removed
        self.assertEqual({'uuid2': 'thread2'},
                         self.handler._delayed_event_threads)

    @mock.patch('eventlet.greenthread.spawn_after', autospec=True)
    def test_process(self, mock_spawn):
        thread1 = mock.Mock()
        thread2 = mock.Mock()
        mock_spawn.side_effect = [thread1, thread2]
        # First call populates the delay queue
        self.assertEqual({}, self.handler._delayed_event_threads)
        self.handler.process(None, 'uuid')
        mock_spawn.assert_called_once_with(15, self.handler._emit_event,
                                           'uuid', None)
        self.assertEqual({'uuid': thread1},
                         self.handler._delayed_event_threads)
        thread1.cancel.assert_not_called()
        thread2.cancel.assert_not_called()

        mock_spawn.reset_mock()

        # Second call cancels the first thread and replaces it in delay queue
        self.handler.process('inst', 'uuid')
        mock_spawn.assert_called_once_with(15, self.handler._emit_event,
                                           'uuid', 'inst')
        self.assertEqual({'uuid': thread2},
                         self.handler._delayed_event_threads)
        thread1.cancel.assert_called_once_with()
        thread2.cancel.assert_not_called()
