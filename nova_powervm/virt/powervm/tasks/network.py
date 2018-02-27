# Copyright 2015, 2018 IBM Corp.
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

import eventlet

from nova import exception

from oslo_log import log as logging
from pypowervm.wrappers import network as pvm_net
from taskflow import task

from nova_powervm import conf as cfg
from nova_powervm.virt.powervm import vif
from nova_powervm.virt.powervm import vm


LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class UnplugVifs(task.Task):

    """The task to unplug Virtual Network Interfaces from a VM."""

    def __init__(self, adapter, instance, network_infos, host_uuid,
                 slot_mgr):
        """Create the task.

        :param adapter: The pypowervm adapter.
        :param instance: The nova instance.
        :param network_infos: The network information containing the nova
                              VIFs to create.
        :param host_uuid: The host system's PowerVM UUID.
        :param slot_mgr: A NovaSlotManager.  Used to store/retrieve the client
                         slots used when a VIF is detached from the VM.
        """
        self.adapter = adapter
        self.network_infos = network_infos or []
        self.host_uuid = host_uuid
        self.slot_mgr = slot_mgr
        self.instance = instance

        super(UnplugVifs, self).__init__(
            name='unplug_vifs', requires=['lpar_wrap'])

    def execute(self, lpar_wrap):
        # If the state is not in an OK state for deleting, then throw an
        # error up front.
        modifiable, reason = lpar_wrap.can_modify_io()
        if not modifiable:
            LOG.error("Unable to remove VIFs in the instance's current state. "
                      "The reason reported by the system is: %(reason)s",
                      {'reason': reason}, instance=self.instance)
            raise exception.VirtualInterfaceUnplugException(reason=reason)

        # Get all the current Client Network Adapters (CNA) on the VM itself.
        cna_w_list = vm.get_cnas(self.adapter, self.instance)

        # Walk through the VIFs and delete the corresponding CNA on the VM.
        for network_info in self.network_infos:
            vif.unplug(self.adapter, self.host_uuid, self.instance,
                       network_info, self.slot_mgr, cna_w_list=cna_w_list)

        return cna_w_list


class PlugVifs(task.Task):

    """The task to plug the Virtual Network Interfaces to a VM."""

    def __init__(self, virt_api, adapter, instance, network_infos, host_uuid,
                 slot_mgr):
        """Create the task.

        Provides 'vm_cnas' - the list of the Virtual Machine's Client Network
        Adapters as they stand after all VIFs are plugged.  May be None, in
        which case the Task requiring 'vm_cnas' should discover them afresh.

        :param virt_api: The VirtAPI for the operation.
        :param adapter: The pypowervm adapter.
        :param instance: The nova instance.
        :param network_infos: The network information containing the nova
                              VIFs to create.
        :param host_uuid: The host system's PowerVM UUID.
        :param slot_mgr: A NovaSlotManager.  Used to store/retrieve the client
                         slots used when a VIF is attached to the VM.
        """
        self.virt_api = virt_api
        self.adapter = adapter
        self.network_infos = network_infos or []
        self.host_uuid = host_uuid
        self.slot_mgr = slot_mgr
        self.crt_network_infos, self.update_network_infos = [], []
        self.cnas, self.vnics = None, None
        self.instance = instance

        super(PlugVifs, self).__init__(name='plug_vifs', provides='vm_cnas',
                                       requires=['lpar_wrap'])

    def _vif_exists(self, network_info):
        """Does the instance have a CNA/VNIC (as appropriate) for a given net?

        :param network_info: A network information dict.  This method expects
                             it to contain keys 'vnic_type' (value is 'direct'
                             for VNIC; otherwise assume CNA); and 'address'
                             (MAC address).
        :return: True if a CNA/VNIC (as appropriate) with the network_info's
                 MAC address exists on the instance.  False otherwise.
        """
        # Are we looking for a VNIC or a CNA?
        if network_info['vnic_type'] == 'direct':
            if self.vnics is None:
                self.vnics = vm.get_vnics(self.adapter, self.instance)
            vifs = self.vnics
        else:
            if self.cnas is None:
                self.cnas = vm.get_cnas(self.adapter, self.instance)
            vifs = self.cnas

        return network_info['address'] in [vm.norm_mac(v.mac) for v in vifs]

    def execute(self, lpar_wrap):
        # We will have two types of network infos.  One is for newly created
        # vifs.  The others are those that exist, but should be re-'treated'
        for network_info in self.network_infos:
            if self._vif_exists(network_info):
                self.update_network_infos.append(network_info)
            else:
                self.crt_network_infos.append(network_info)

        # If there are no vifs to create or update, then just exit immediately.
        if not self.crt_network_infos and not self.update_network_infos:
            return []

        # Check to see if the LPAR is OK to add VIFs to.
        modifiable, reason = lpar_wrap.can_modify_io()
        if not modifiable and self.crt_network_infos:
            LOG.error("Unable to create VIF(s) in the instance's current "
                      "state. The reason reported by the system is: "
                      "%(reason)s", {'reason': reason}, instance=self.instance)
            raise exception.VirtualInterfaceCreateException()

        # TODO(KYLEH): We're setting up to wait for an instance event.  The
        # event needs to come back to our compute manager so we need to ensure
        # the instance.host is set to our host.  We shouldn't need to do this
        # but in the evacuate/recreate case it may reflect the old host.
        # See: https://bugs.launchpad.net/nova/+bug/1535918
        undo_host_change = False
        if self.instance.host != CONF.host:
            LOG.warning('Instance was not assigned to this host. '
                        'It was assigned to: %s', self.instance.host,
                        instance=self.instance)
            # Update the instance...
            old_host = self.instance.host
            self.instance.host = CONF.host
            self.instance.save()
            undo_host_change = True

        # For existing VIFs that we just need to update, run the plug but do
        # not wait for the neutron event as that likely won't be sent (it was
        # already done).
        for network_info in self.update_network_infos:
            LOG.info("Updating VIF with mac %(mac)s",
                     {'mac': network_info['address']}, instance=self.instance)
            vif.plug(self.adapter, self.host_uuid, self.instance,
                     network_info, self.slot_mgr, new_vif=False)

        # For the VIFs, run the creates (and wait for the events back)
        try:
            with self.virt_api.wait_for_instance_event(
                    self.instance, self._get_vif_events(),
                    deadline=CONF.vif_plugging_timeout,
                    error_callback=self._vif_callback_failed):
                for network_info in self.crt_network_infos:
                    LOG.info('Creating VIF with mac %(mac)s.',
                             {'mac': network_info['address']},
                             instance=self.instance)
                    new_vif = vif.plug(
                        self.adapter, self.host_uuid, self.instance,
                        network_info, self.slot_mgr, new_vif=True)
                    if self.cnas is not None and isinstance(new_vif,
                                                            pvm_net.CNA):
                        self.cnas.append(new_vif)
        except eventlet.timeout.Timeout:
            LOG.error('Error waiting for VIF to be created.',
                      instance=self.instance)
            raise exception.VirtualInterfaceCreateException()
        finally:
            if undo_host_change:
                LOG.info('Undoing temporary host assignment to instance.',
                         instance=self.instance)
                self.instance.host = old_host
                self.instance.save()

        return self.cnas

    def _vif_callback_failed(self, event_name, instance):
        LOG.error('VIF plug failure for callback on event %(event)s.',
                  {'event': event_name}, instance=instance)
        if CONF.vif_plugging_is_fatal:
            raise exception.VirtualInterfaceCreateException()

    def _get_vif_events(self):
        """Returns the VIF events that need to be received for a VIF plug.

        In order for a VIF plug to be successful, certain events should be
        received from other components within the OpenStack ecosystem. This
        method returns the events neutron needs for a given deploy.
        """
        # See libvirt's driver.py -> _get_neutron_events method for
        # more information.
        if CONF.vif_plugging_is_fatal and CONF.vif_plugging_timeout:
            return [('network-vif-plugged', network_info['id'])
                    for network_info in self.crt_network_infos
                    if not network_info.get('active', True)]

    def revert(self, lpar_wrap, result, flow_failures):
        if not self.network_infos:
            return

        # The parameters have to match the execute method, plus the response +
        # failures even if only a subset are used.
        LOG.warning('VIF creation is being rolled back.',
                    instance=self.instance)

        # Get the current adapters on the system
        cna_w_list = vm.get_cnas(self.adapter, self.instance)
        for network_info in self.crt_network_infos:
            try:
                vif.unplug(self.adapter, self.host_uuid, self.instance,
                           network_info, self.slot_mgr, cna_w_list=cna_w_list)
            except Exception:
                LOG.exception("Error unplugging during vif rollback. "
                              "Ignoring.", instance=self.instance)


class PlugMgmtVif(task.Task):

    """The task to plug the Management VIF into a VM."""

    def __init__(self, adapter, instance, host_uuid, slot_mgr):
        """Create the task.

        Requires 'vm_cnas' from PlugVifs.  If None, this Task will retrieve the
        VM's list of CNAs.

        Provides the mgmt_cna.  This may be none if no management device was
        created.  This is the CNA of the mgmt vif for the VM.

        :param adapter: The pypowervm adapter.
        :param instance: The nova instance.
        :param host_uuid: The host system's PowerVM UUID.
        :param slot_mgr: A NovaSlotManager.  Used to store/retrieve the client
                         slots used when a VIF is attached to the VM
        """
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.slot_mgr = slot_mgr
        self.instance = instance

        super(PlugMgmtVif, self).__init__(
            name='plug_mgmt_vif', provides='mgmt_cna', requires=['vm_cnas'])

    def execute(self, vm_cnas):
        # If configured to not use RMC mgmt vifs, then return None.  Need to
        # return None because the Config Drive step (which may be used...may
        # not be) required the mgmt vif.
        if not CONF.powervm.use_rmc_mgmt_vif:
            LOG.debug('No management VIF created because '
                      'CONF.powervm.use_rmc_mgmt_vif is False',
                      instance=self.instance)
            return None

        LOG.info('Plugging the management network interface.',
                 instance=self.instance)
        # Determine if we need to create the secure RMC VIF.  This should only
        # be needed if there is not a VIF on the secure RMC vSwitch
        vswitch = vif.get_secure_rmc_vswitch(self.adapter, self.host_uuid)
        if vswitch is None:
            LOG.warning('No management VIF created due to lack of management '
                        'virtual switch', instance=self.instance)
            return None

        # This next check verifies that there are no existing NICs on the
        # vSwitch, so that the VM does not end up with multiple RMC VIFs.
        if vm_cnas is None:
            has_mgmt_vif = vm.get_cnas(self.adapter, self.instance,
                                       vswitch_uri=vswitch.href)
        else:
            has_mgmt_vif = vswitch.href in [cna.vswitch_uri for cna in vm_cnas]

        if has_mgmt_vif:
            LOG.debug('Management VIF already exists.', instance=self.instance)
            return None

        # Return the created management CNA
        return vif.plug_secure_rmc_vif(
            self.adapter, self.instance, self.host_uuid, self.slot_mgr)
