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

from eventlet import greenthread
from nova.compute import power_state
from nova.compute import task_states
from nova import context as ctx
from nova import exception
from nova.virt import event
from oslo_concurrency import lockutils
from oslo_log import log as logging
from pypowervm import adapter as pvm_apt
from pypowervm import util as pvm_util
from pypowervm.wrappers import event as pvm_evt

from nova_powervm.virt.powervm import vm


LOG = logging.getLogger(__name__)

_INST_ACTIONS_HANDLED = {'PartitionState', 'NVRAM'}
_NO_EVENT_TASK_STATES = {
    task_states.SPAWNING,
    task_states.RESIZE_MIGRATING,
    task_states.RESIZE_REVERTING,
    task_states.REBOOTING,
    task_states.REBOOTING_HARD,
    task_states.REBOOT_STARTED_HARD,
    task_states.PAUSING,
    task_states.UNPAUSING,
    task_states.SUSPENDING,
    task_states.RESUMING,
    task_states.POWERING_OFF,
    task_states.POWERING_ON,
    task_states.RESCUING,
    task_states.UNRESCUING,
    task_states.REBUILDING,
    task_states.REBUILD_SPAWNING,
    task_states.MIGRATING,
    task_states.DELETING,
    task_states.SOFT_DELETING,
    task_states.RESTORING,
    task_states.SHELVING,
    task_states.SHELVING_OFFLOADING,
    task_states.UNSHELVING,
}

_LIFECYCLE_EVT_LOCK = 'pvm_lifecycle_event'

_CONTEXT = None


def _get_instance(inst, pvm_uuid):
    global _CONTEXT
    if inst is not None:
        return inst
    with lockutils.lock('get_context_once'):
        if _CONTEXT is None:
            _CONTEXT = ctx.get_admin_context()
    LOG.debug('PowerVM Nova Event Handler: Getting inst for id %s', pvm_uuid)
    return vm.get_instance(_CONTEXT, pvm_uuid)


class PowerVMNovaEventHandler(pvm_apt.WrapperEventHandler):
    """Used to receive and handle events from PowerVM and convert to Nova."""
    def __init__(self, driver):
        self._driver = driver
        self._lifecycle_handler = PowerVMLifecycleEventHandler(self._driver)
        self._uuid_cache = {}

    def _get_inst_uuid(self, inst, pvm_uuid):
        """Retrieve instance UUID from cache keyed by the PVM UUID.

        :param inst: the instance object.
        :param pvm_uuid: the PowerVM uuid of the vm
        :return inst: the instance object.
        :return inst_uuid: The nova instance uuid
        """
        inst_uuid = self._uuid_cache.get(pvm_uuid)
        if not inst_uuid:
            inst = _get_instance(inst, pvm_uuid)
            inst_uuid = inst.uuid if inst else None
            if inst_uuid:
                self._uuid_cache[pvm_uuid] = inst_uuid
        return inst, inst_uuid

    def _handle_inst_event(self, inst, pvm_uuid, details):
        """Handle an instance event.

        This method will check if an instance event signals a change in the
        state of the instance as known to OpenStack and if so, trigger an
        event upward.

        :param inst: the instance object.
        :param pvm_uuid: the PowerVM uuid of the vm
        :param details: Parsed Details from the event
        :return inst: The nova instance, which may be None
        """
        # If the NVRAM has changed for this instance and a store is configured.
        if 'NVRAM' in details and self._driver.nvram_mgr is not None:
            # Schedule the NVRAM for the instance to be stored.
            # We'll need to fetch the instance object in the event we don't
            # have the object and the UUID isn't cached. By updating the
            # object reference here and returning it the process method will
            # save the object in its cache.
            inst, inst_uuid = self._get_inst_uuid(inst, pvm_uuid)
            if inst_uuid is None:
                return None

            LOG.debug('Handle NVRAM event for PowerVM LPAR %s', pvm_uuid)
            self._driver.nvram_mgr.store(inst_uuid)

        # If the state of the vm changed see if it should be handled
        if 'PartitionState' in details:
            self._lifecycle_handler.process(inst, pvm_uuid)

        return inst

    def process(self, events):
        """Process the event that comes back from PowerVM.

        :param events: The pypowervm Event wrapper.
        """
        inst_cache = {}
        for pvm_event in events:
            try:
                if pvm_event.etype in (pvm_evt.EventType.NEW_CLIENT,
                                       pvm_evt.EventType.CACHE_CLEARED):
                    # TODO(efried): Should we pull and check all the LPARs?
                    self._uuid_cache.clear()
                    continue
                # See if this uri (from data) ends with a PowerVM UUID.
                pvm_uuid = pvm_util.get_req_path_uuid(
                    pvm_event.data, preserve_case=True)
                if pvm_uuid is None:
                    continue
                # Is it an instance event?
                if not pvm_event.data.endswith('LogicalPartition/' + pvm_uuid):
                    continue

                # Are we deleting? Meaning we need to clear the cache entry.
                if pvm_event.etype == pvm_evt.EventType.DELETE_URI:
                    try:
                        del self._uuid_cache[pvm_uuid]
                    except KeyError:
                        pass
                    continue
                # Pull all the pieces of the event.
                details = (pvm_event.detail.split(',') if pvm_event.detail
                           else [])
                # Is it one we care about?
                if not _INST_ACTIONS_HANDLED & set(details):
                    continue

                inst_cache[pvm_event.data] = self._handle_inst_event(
                    inst_cache.get(pvm_event.data), pvm_uuid, details)

            except Exception:
                # We deliberately keep this exception clause as broad as
                # possible - we don't want *any* error to stop us from
                # attempting to process the next event.
                LOG.exception('Unable to process PowerVM event %s',
                              str(pvm_event))


class PowerVMLifecycleEventHandler(object):
    """Because lifecycle events are weird, we need our own handler.

    Lifecycle events that come back from the hypervisor are very 'surface
    value'.  They tell you that it started, stopped, migrated, etc...  However,
    multiple events may come in quickly that represent a bigger action.  For
    instance a restart will generate a stop and then a start rapidly.

    Nova being asynchronous can flip those events around.  Where the start
    would flow through before the stop.  That is bad.

    We need to make sure that these events that can be linked to bigger
    lifecycle events can be wiped out if the converse action is run against
    it.  Ex. Don't send a stop event up to nova if you received a start event
    shortly after it.
    """
    def __init__(self, driver):
        self._driver = driver
        self._delayed_event_threads = {}

    @lockutils.synchronized(_LIFECYCLE_EVT_LOCK)
    def _emit_event(self, pvm_uuid, inst):
        # Get the current state
        try:
            pvm_state = vm.get_vm_qp(self._driver.adapter, pvm_uuid,
                                     'PartitionState')
        except exception.InstanceNotFound:
            LOG.debug("LPAR %s was deleted while event was delayed.", pvm_uuid,
                      instance=inst)
            return

        LOG.debug('New state %s for partition %s', pvm_state, pvm_uuid,
                  instance=inst)

        inst = _get_instance(inst, pvm_uuid)
        if inst is None:
            LOG.debug("Not emitting LifecycleEvent: no instance for LPAR %s",
                      pvm_uuid)
            return

        # If we're in the middle of a nova-driven operation, no event necessary
        if inst.task_state in _NO_EVENT_TASK_STATES:
            LOG.debug("Not emitting LifecycleEvent: instance task_state is %s",
                      inst.task_state, instance=inst)
            return

        # See if it's really a change of state from what OpenStack knows
        transition = vm.translate_event(pvm_state, inst.power_state)
        if transition is None:
            LOG.debug("No LifecycleEvent necessary for pvm_state(%s) and "
                      "power_state(%s).", pvm_state,
                      power_state.STATE_MAP[inst.power_state], instance=inst)
            return

        # Log as if normal event
        lce = event.LifecycleEvent(inst.uuid, transition)
        LOG.info('Sending LifecycleEvent for instance state change to: %s',
                 pvm_state, instance=inst)
        self._driver.emit_event(lce)

        # Delete out the queue
        del self._delayed_event_threads[pvm_uuid]

    @lockutils.synchronized(_LIFECYCLE_EVT_LOCK)
    def process(self, inst, pvm_uuid):
        """Emits the event, or adds it to the queue for delayed emission.

        :param inst: The nova instance.  May be None.
        :param pvm_uuid: The PowerVM LPAR UUID.
        """
        # Cancel out the current delay event.  Can happen as it goes
        # from SHUTTING_DOWN to NOT_ACTIVATED, multiple delayed events
        # can come in at once.  Only want the last.
        if pvm_uuid in self._delayed_event_threads:
            self._delayed_event_threads[pvm_uuid].cancel()

        # Spawn in the background
        elem = greenthread.spawn_after(15, self._emit_event, pvm_uuid, inst)
        self._delayed_event_threads[pvm_uuid] = elem
