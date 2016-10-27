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


from nova import context as ctx
from nova.virt import event
from oslo_log import log as logging
from pypowervm import adapter as pvm_apt
from pypowervm import util as pvm_util
from pypowervm.wrappers import event as pvm_evt

from nova_powervm.virt.powervm.i18n import _LI
from nova_powervm.virt.powervm.i18n import _LW
from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)


class PowerVMNovaEventHandler(pvm_apt.WrapperEventHandler):
    """Used to receive and handle events from PowerVM and convert to Nova."""
    inst_actions_handled = {'PartitionState', 'NVRAM'}

    def __init__(self, driver):
        self._driver = driver

    def _handle_event(self, pvm_event, details, inst=None):
        """Handle an individual event.

        :param pvm_event: PowerVM Event Wrapper
        :param details: Parsed Details from the event
        :param inst: (Optional, Default: None) The pypowervm wrapper object
                    that represents the VM instance.
                    If None we try to look it up based on UUID.
        :return: returns the instance object or None (when it's not an
                 instance event or action is not partition state change
                 or NVRAM change)
        """
        # See if this uri (from data) ends with a PowerVM UUID.
        if not pvm_util.is_instance_path(pvm_event.data):
            return None

        # If a vm event and one we handle, call the inst handler.
        pvm_uuid = pvm_util.get_req_path_uuid(
            pvm_event.data, preserve_case=True)
        if (pvm_event.data.endswith('LogicalPartition/' + pvm_uuid) and
                (self.inst_actions_handled & set(details))):
            if not inst:
                LOG.debug('PowerVM Nova Event Handler: Getting inst '
                          'for id %s', pvm_uuid)
                inst = vm.get_instance(ctx.get_admin_context(),
                                       pvm_uuid)
            if inst:
                LOG.debug('Handle action "%(action)s" event for instance: '
                          '%(inst)s', dict(action=details, inst=inst.name))
                self._handle_inst_event(inst, pvm_uuid, details)
                return inst
        return None

    def _handle_inst_event(self, inst, pvm_uuid, details):
        """Handle an instance event.

        This method will check if an instance event signals a change in the
        state of the instance as known to OpenStack and if so, trigger an
        event upward.

        :param inst: the instance object.
        :param pvm_uuid: the PowerVM uuid of the vm
        :param details: Parsed Details from the event
        """
        # If the state of the vm changed see if it should be handled
        if 'PartitionState' in details:
            # Get the current state
            pvm_state = vm.get_vm_qp(self._driver.adapter, pvm_uuid,
                                     'PartitionState')
            # See if it's really a change of state from what OpenStack knows
            transition = vm.translate_event(pvm_state, inst.power_state)
            if transition is not None:
                LOG.debug('New state for instance: %s', pvm_state,
                          instance=inst)

                # Now create an event and sent it.
                lce = event.LifecycleEvent(inst.uuid, transition)
                LOG.info(_LI('Sending life cycle event for instance state '
                             'change to: %s'), pvm_state, instance=inst)
                self._driver.emit_event(lce)

        # If the NVRAM has changed for this instance and a store is configured.
        if 'NVRAM' in details and self._driver.nvram_mgr is not None:
            # Schedule the NVRAM for the instance to be stored.
            self._driver.nvram_mgr.store(inst)

    def process(self, events):
        """Process the event that comes back from PowerVM.

        :param events: The pypowervm Event wrapper.
        """
        inst_cache = {}
        for pvm_event in events:
            try:
                # Pull all the pieces of the event.
                details = (pvm_event.detail.split(',') if pvm_event.detail
                           else [])

                if pvm_event.etype not in pvm_evt.EventType.NEW_CLIENT:
                    LOG.debug('PowerVM Event-Action: %s URI: %s Details %s',
                              pvm_event.etype, pvm_event.data, details)
                inst_cache[pvm_event.data] = self._handle_event(
                    pvm_event, details, inst=inst_cache.get(pvm_event.data,
                                                            None))
            except Exception as e:
                LOG.exception(e)
                LOG.warning(_LW('Unable to parse event URI: %s from PowerVM.'),
                            pvm_event.data)
