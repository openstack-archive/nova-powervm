# Copyright 2016, 2018 IBM Corp.
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

import abc
import six

from nova import context as ctx
from nova import exception
from nova import network as net_api
from nova.network import model as network_model
from oslo_config import cfg
from oslo_log import log
from oslo_serialization import jsonutils
from oslo_utils import excutils
from oslo_utils import importutils
import pypowervm.const as pvm_c
from pypowervm import exceptions as pvm_ex
from pypowervm.tasks import cna as pvm_cna
from pypowervm.tasks import partition as pvm_par
from pypowervm.tasks import sriov as sriovtask
from pypowervm import util as pvm_util
from pypowervm.wrappers import event as pvm_evt
from pypowervm.wrappers import iocard as pvm_card
from pypowervm.wrappers import logical_partition as pvm_lpar
from pypowervm.wrappers import managed_system as pvm_ms
from pypowervm.wrappers import network as pvm_net

from nova_powervm.virt.powervm.i18n import _
from nova_powervm.virt.powervm import vm


LOG = log.getLogger(__name__)

SECURE_RMC_VSWITCH = 'MGMTSWITCH'
SECURE_RMC_VLAN = 4094

# Provider tag for custom events from this module
EVENT_PROVIDER_ID = 'NOVA_PVM_VIF'

VIF_TYPE_PVM_SEA = 'pvm_sea'
VIF_TYPE_PVM_OVS = 'ovs'
VIF_TYPE_PVM_SRIOV = 'pvm_sriov'

VIF_MAPPING = {VIF_TYPE_PVM_SEA:
               'nova_powervm.virt.powervm.vif.PvmSeaVifDriver',
               VIF_TYPE_PVM_OVS:
               'nova_powervm.virt.powervm.vif.PvmOvsVifDriver',
               VIF_TYPE_PVM_SRIOV:
               'nova_powervm.virt.powervm.vif.PvmVnicSriovVifDriver'}

# NOTE(svenkat): Manually adjust CNA child ordering to workaround bug 1731657
# TODO(svenkat) Remove workaround when pypowervm is fixed
child_order = list(pvm_net.CNA._child_order)
child_order.remove('VirtualNetworks')
child_order.append('VirtualNetworks')
pvm_net.CNA._child_order = tuple(child_order)

CONF = cfg.CONF


def _build_vif_driver(adapter, host_uuid, instance, vif):
    """Returns the appropriate VIF Driver for the given VIF.

    :param adapter: The pypowervm adapter API interface.
    :param host_uuid: The host system UUID.
    :param instance: The nova instance.
    :param vif: The virtual interface to from Nova.
    :return: The appropriate PvmVifDriver for the VIF.
    """
    if vif.get('type') is None:
        raise exception.VirtualInterfacePlugException(
            _("vif_type parameter must be present for this vif_driver "
              "implementation"))

    # Check the type to the implementations
    if VIF_MAPPING.get(vif['type']):
        return importutils.import_object(
            VIF_MAPPING.get(vif['type']), adapter, host_uuid, instance)

    # No matching implementation, raise error.
    raise exception.VirtualInterfacePlugException(
        _("Unable to find appropriate PowerVM VIF Driver for VIF type "
          "%(vif_type)s on instance %(instance)s") %
        {'vif_type': vif['type'], 'instance': instance.name})


def _push_vif_event(adapter, action, vif_w, instance, vif_type):
    """Push a custom event to the REST server for a vif action (plug/unplug).

    This event prompts the neutron agent to mark the port up or down.

    :param adapter: The pypowervm adapter.
    :param action: The action taken on the vif - either 'plug' or 'unplug'
    :param vif_w: The pypowervm wrapper of the affected vif (CNA, VNIC, etc.)
    :param instance: The nova instance for the event
    :param vif_type: The type of event source (pvm_sea, ovs, bridge,
                     pvm_sriov etc)
    """
    data = vif_w.href
    detail = jsonutils.dumps(dict(provider=EVENT_PROVIDER_ID, action=action,
                                  mac=vif_w.mac, type=vif_type))
    event = pvm_evt.Event.bld(adapter, data, detail)
    try:
        event = event.create()
        LOG.debug('Pushed custom event for consumption by neutron agent: %s',
                  str(event), instance=instance)
    except Exception:
        with excutils.save_and_reraise_exception(logger=LOG):
            LOG.exception('Custom VIF event push failed.  %s', str(event),
                          instance=instance)


def plug(adapter, host_uuid, instance, vif, slot_mgr, new_vif=True):
    """Plugs a virtual interface (network) into a VM.

    :param adapter: The pypowervm adapter.
    :param host_uuid: The host UUID for the PowerVM API.
    :param instance: The nova instance object.
    :param vif: The virtual interface to plug into the instance.
    :param slot_mgr: A NovaSlotManager.  Used to store/retrieve the client
                     slots used when a VIF is attached to the VM.
    :param new_vif: (Optional, Default: True) If set, indicates that it is
                    a brand new VIF.  If False, it indicates that the VIF
                    is already on the client but should be treated on the
                    bridge.
    :return: The wrapper (CNA or VNIC) representing the plugged virtual
             network.  None if the vnet was not created.
    """
    vif_drv = _build_vif_driver(adapter, host_uuid, instance, vif)

    # Get the slot number to use for the VIF creation.  May be None
    # indicating usage of the next highest available.
    slot_num = slot_mgr.build_map.get_vnet_slot(vif['address'])

    # Invoke the plug
    try:
        vnet_w = vif_drv.plug(vif, slot_num, new_vif=new_vif)
    except pvm_ex.HttpError as he:
        # Log the message constructed by HttpError
        LOG.exception("HttpError during vif plug operation.",
                      instance=instance)
        raise exception.VirtualInterfacePlugException(reason=he.args[0])
    # Other exceptions are (hopefully) custom VirtualInterfacePlugException
    # generated lower in the call stack.

    # If the slot number hadn't been provided initially, save it for the
    # next rebuild
    if not slot_num and new_vif:
        slot_mgr.register_vnet(vnet_w)

    # Push a custom event if we really plugged the vif
    if vnet_w is not None:
        _push_vif_event(adapter, 'plug', vnet_w, instance, vif['type'])

    return vnet_w


def unplug(adapter, host_uuid, instance, vif, slot_mgr, cna_w_list=None):
    """Unplugs a virtual interface (network) from a VM.

    :param adapter: The pypowervm adapter.
    :param host_uuid: The host UUID for the PowerVM API.
    :param instance: The nova instance object.
    :param vif: The virtual interface to plug into the instance.
    :param slot_mgr: A NovaSlotManager.  Used to store/retrieve the client
                     slots used when a VIF is detached from the VM.
    :param cna_w_list: (Optional, Default: None) The list of Client Network
                       Adapters from pypowervm.  Providing this input
                       allows for an improvement in operation speed.
    """
    vif_drv = _build_vif_driver(adapter, host_uuid, instance, vif)
    try:
        vnet_w = vif_drv.unplug(vif, cna_w_list=cna_w_list)
        # Push a custom event, but only if the vif existed in the first place
        if vnet_w:
            _push_vif_event(adapter, 'unplug', vnet_w, instance, vif['type'])
    except pvm_ex.HttpError as he:
        # Log the message constructed by HttpError
        LOG.exception("HttpError during vif unplug operation.",
                      instance=instance)
        raise exception.VirtualInterfaceUnplugException(reason=he.args[0])

    if vnet_w:
        slot_mgr.drop_vnet(vnet_w)


def pre_live_migrate_at_destination(adapter, host_uuid, instance, vif,
                                    vea_vlan_mappings):
    """Performs the pre live migrate on the destination host.

    :param adapter: The pypowervm adapter.
    :param host_uuid: The host UUID for the PowerVM API.
    :param instance: The nova instance object.
    :param vif: The virtual interface that will be migrated.  This may be
                called network_info in other portions of the code.
    :param vea_vlan_mappings: The VEA VLAN mappings.  Key is the vif mac
                              address, value is the destination's target
                              hypervisor VLAN.
    """
    vif_drv = _build_vif_driver(adapter, host_uuid, instance, vif)
    vif_drv.pre_live_migrate_at_destination(vif, vea_vlan_mappings)


def rollback_live_migration_at_destination(adapter, host_uuid, instance, vif,
                                           vea_vlan_mappings):
    """Performs the rollback of the live migrate on the destination host.

    :param adapter: The pypowervm adapter.
    :param host_uuid: The host UUID for the PowerVM API.
    :param instance: The nova instance object.
    :param vif: The virtual interface that is being rolled back.  This may be
                called network_info in other portions of the code.
    :param vea_vlan_mappings: The VEA VLAN mappings.  Key is the vif mac
                              address, value is the destination's target
                              hypervisor VLAN.
    """
    vif_drv = _build_vif_driver(adapter, host_uuid, instance, vif)
    vif_drv.rollback_live_migration_at_destination(vif, vea_vlan_mappings)


def pre_live_migrate_at_source(adapter, host_uuid, instance, vif):
    """Performs the pre live migrate on the source host.

    This is executed directly before the migration is started on the source
    host.

    :param adapter: The pypowervm adapter.
    :param host_uuid: The host UUID for the PowerVM API.
    :param instance: The nova instance object.
    :param vif: The virtual interface that will be migrated.  This may be
                called network_info in other portions of the code.
    :return: The list of TrunkAdapter's on the source that are hosting the
             VM's vif.  Should only return data if those trunks should be
             deleted after the migration.
    """
    vif_drv = _build_vif_driver(adapter, host_uuid, instance, vif)
    return vif_drv.pre_live_migrate_at_source(vif)


def post_live_migrate_at_source(adapter, host_uuid, instance, vif):
    """Performs the post live migrate on the source host.

    :param adapter: The pypowervm adapter.
    :param host_uuid: The host UUID for the PowerVM API.
    :param instance: The nova instance object.
    :param vif: The virtual interface of the instance.  This may be
                called network_info in other portions of the code.
    """
    vif_drv = _build_vif_driver(adapter, host_uuid, instance, vif)
    return vif_drv.post_live_migrate_at_source(vif)


def get_secure_rmc_vswitch(adapter, host_uuid):
    """Returns the vSwitch that is used for secure RMC.

    :param adapter: The pypowervm adapter API interface.
    :param host_uuid: The host system UUID.
    :return: The wrapper for the secure RMC vSwitch.  If it does not exist
             on the system, None is returned.
    """
    vswitches = pvm_net.VSwitch.search(
        adapter, parent_type=pvm_ms.System.schema_type,
        parent_uuid=host_uuid, name=SECURE_RMC_VSWITCH)
    if len(vswitches) == 1:
        return vswitches[0]
    return None


def plug_secure_rmc_vif(adapter, instance, host_uuid, slot_mgr):
    """Creates the Secure RMC Network Adapter on the VM.

    :param adapter: The pypowervm adapter API interface.
    :param instance: The nova instance to create the VIF against.
    :param host_uuid: The host system UUID.
    :param slot_mgr: A NovaSlotManager.  Used to store/retrieve the client
                     slots used when a VIF is attached to the VM
    :return: The created network adapter wrapper.
    """
    # Gather the mac and slot number for the mgmt vif
    mac, slot_num = slot_mgr.build_map.get_mgmt_vea_slot()
    if not mac:
        # This is either a deploy case or rebuild case. For remote restart,
        # mac will not be none, as it will be available from slot data.
        # Deploy case - mac is None at both slot and instance_system_metadata
        # and crt_cna will auto-generate it.
        # Rebuild case - mac is none from slot data but is available
        # at instance system_metadata.
        mac = instance.system_metadata.get('mgmt_interface_mac')

    # Create the adapter.
    lpar_uuid = vm.get_pvm_uuid(instance)
    cna_w = pvm_cna.crt_cna(adapter, host_uuid, lpar_uuid, SECURE_RMC_VLAN,
                            vswitch=SECURE_RMC_VSWITCH, crt_vswitch=True,
                            slot_num=slot_num, mac_addr=mac)

    # Save the mgmt vif to the slot map.
    # For the rebuild case, mac will be present but not slot_num.
    # For deploy case, both will be none. We want to register cna in both cases
    if not slot_num:
        slot_mgr.register_cna(cna_w)
    if cna_w.mac != mac:
        # Update instance system metadata to store instance management
        # interface mac address.
        instance.system_metadata.update({'mgmt_interface_mac': cna_w.mac})

    return cna_w


def _get_trunk_dev_name(vif):
    """Returns the device name for the trunk adapter.

    A given VIF will have a trunk adapter and a
    client adapter.  This will return the trunk adapter's name as it
    will appear on the management VM.

    :param vif: The nova network interface
    :return: The device name.
    """
    if 'devname' in vif:
        return vif['devname']
    return ("nic" + vif['id'])[:network_model.NIC_NAME_LEN]


@six.add_metaclass(abc.ABCMeta)
class PvmVifDriver(object):
    """Represents an abstract class for a PowerVM Vif Driver.

    A VIF Driver understands a given virtual interface type (network).  It
    understands how to plug and unplug a given VIF for a virtual machine.
    """

    def __init__(self, adapter, host_uuid, instance):
        """Initializes a VIF Driver.

        :param adapter: The pypowervm adapter API interface.
        :param host_uuid: The host system UUID.
        :param instance: The nova instance that the vif action will be run
                         against.
        """
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.instance = instance

    @abc.abstractmethod
    def plug(self, vif, slot_num, new_vif=True):
        """Plugs a virtual interface (network) into a VM.

        :param vif: The virtual interface to plug into the instance.
        :param slot_num: Which slot number to plug the VIF into.  May be None.
        :param new_vif: (Optional, Default: True) If set, indicates that it is
                        a brand new VIF.  If False, it indicates that the VIF
                        is already on the client but should be treated on the
                        bridge.
        :return: The new vif that was created.  Only returned if new_vif is
                 set to True.  Otherwise None is expected.
        """
        pass

    def unplug(self, vif, cna_w_list=None):
        """Unplugs a virtual interface (network) from a VM.

        :param vif: The virtual interface to plug into the instance.
        :param cna_w_list: (Optional, Default: None) The list of Client Network
                           Adapters from pypowervm.  Providing this input
                           allows for an improvement in operation speed.
        :return cna_w: The deleted Client Network Adapter.
        """
        # This is a default implementation that most implementations will
        # require.

        # Need to find the adapters if they were not provided
        if not cna_w_list:
            cna_w_list = vm.get_cnas(self.adapter, self.instance)

        cna_w = self._find_cna_for_vif(cna_w_list, vif)
        if not cna_w:
            LOG.warning('Unable to unplug VIF with mac %(mac)s.  The VIF was '
                        'not found on the instance.',
                        {'mac': vif['address']}, instance=self.instance)
            return None

        LOG.info('Deleting VIF with mac %(mac)s.',
                 {'mac': vif['address']}, instance=self.instance)
        try:
            cna_w.delete()
        except Exception as e:
            LOG.exception('Unable to unplug VIF with mac %(mac)s.',
                          {'mac': vif['address']}, instance=self.instance)
            raise exception.VirtualInterfaceUnplugException(
                reason=six.text_type(e))
        return cna_w

    @staticmethod
    def _find_cna_for_vif(cna_w_list, vif):
        """Finds the PowerVM CNA for a given Nova VIF.

        :param cna_w_list: The list of Client Network Adapter wrappers from
                           pypowervm.
        :param vif: The Nova Virtual Interface (virtual network interface).
        :return: The CNA that corresponds to the VIF.  None if one is not
                 part of the cna_w_list.
        """
        for cna_w in cna_w_list:
            # If the MAC address matched, attempt the delete.
            if vm.norm_mac(cna_w.mac) == vif['address']:
                return cna_w
        return None

    def pre_live_migrate_at_destination(self, vif, vea_vlan_mappings):
        """Performs the pre live migrate on the destination host.

        Pre live migrate at destination is invoked before
        pre_live_migrate_at_source.

        :param vif: The virtual interface that will be migrated.  This may be
                    called network_info in other portions of the code.
        :param vea_vlan_mappings: The VEA VLAN mappings.  Key is the vif
                                  mac address, value is the destination's
                                  target hypervisor VLAN.
        """
        pass

    def rollback_live_migration_at_destination(self, vif, vea_vlan_mappings):
        """Rolls back the pre live migrate on the destination host.

        :param vif: The virtual interface that was being migrated.  This may be
                    called network_info in other portions of the code.
        :param vea_vlan_mappings: The VEA VLAN mappings.  Key is the vif
                                  mac address, value is the destination's
                                  target hypervisor VLAN.
        """
        pass

    def pre_live_migrate_at_source(self, vif):
        """Performs the pre live migrate on the source host.

        This is executed directly before the migration is started on the source
        host.

        :param vif: The virtual interface that will be migrated.  This may be
                    called network_info in other portions of the code.
        :return: The list of TrunkAdapter's on the source that are hosting the
                 VM's vif.  Should only return data if those trunks should be
                 deleted after the migration.
        """
        return []

    def post_live_migrate_at_source(self, vif):
        """Performs the post live migrate on the source host.

        :param vif: The virtual interface of an instance.  This may be
                    called network_info in other portions of the code.
        """
        pass


class PvmSeaVifDriver(PvmVifDriver):
    """The PowerVM Shared Ethernet Adapter VIF Driver."""

    def plug(self, vif, slot_num, new_vif=True):
        """Plugs a virtual interface (network) into a VM.

        This method simply creates the client network adapter into the VM.

        :param vif: The virtual interface to plug into the instance.
        :param slot_num: Which slot number to plug the VIF into.  May be None.
        :param new_vif: (Optional, Default: True) If set, indicates that it is
                        a brand new VIF.  If False, it indicates that the VIF
                        is already on the client but should be treated on the
                        bridge.
        :return: The new vif that was created.  Only returned if new_vif is
                 set to True.  Otherwise None is expected.
        """
        # Do nothing if not a new VIF
        if not new_vif:
            return None

        lpar_uuid = vm.get_pvm_uuid(self.instance)

        # CNA's require a VLAN.  Nova network puts it in network-meta.
        # The networking-powervm neutron agent will also send it, if so via
        # the vif details.
        vlan = vif['network']['meta'].get('vlan', None)
        if not vlan:
            vlan = int(vif['details']['vlan'])

        LOG.debug("Creating SEA-based VIF with VLAN %s", str(vlan),
                  instance=self.instance)
        cna_w = pvm_cna.crt_cna(self.adapter, self.host_uuid, lpar_uuid, vlan,
                                mac_addr=vif['address'], slot_num=slot_num)

        return cna_w


class PvmVnicSriovVifDriver(PvmVifDriver):
    """The SR-IOV VIF driver for PowerVM."""

    def plug(self, vif, slot_num, new_vif=True):
        if not new_vif:
            return None

        physnet = vif.get_physical_network()
        if not physnet:
            # Get physnet from neutron network if not present in vif
            # TODO(svenkat): This section of code will be eliminated in
            # pike release. Design will be in place to fix any vif
            # that has physical_network missing. The fix will be in
            # compute startup code.
            net_id = vif['network']['id']
            admin_context = ctx.get_admin_context()
            napi = net_api.API()
            network = napi.get(admin_context, net_id)
            physnet = network.physical_network

        LOG.debug("Plugging vNIC SR-IOV vif for physical network %(physnet)s.",
                  {'physnet': physnet}, instance=self.instance)

        # Get the msys
        msys = pvm_ms.System.get(self.adapter)[0]
        # Physical ports for the given port label
        pports_w = sriovtask.find_pports_for_portlabel(physnet, self.adapter,
                                                       msys)
        pports = [pport.loc_code for pport in pports_w]

        if not pports:
            raise exception.VirtualInterfacePlugException(
                _("Unable to find acceptable Ethernet ports on physical "
                  "network '%(physnet)s' for instance %(inst)s for SRIOV "
                  "based VIF with MAC address %(vif_mac)s.") %
                {'physnet': physnet, 'inst': self.instance.name,
                 'vif_mac': vif['address']})

        # MAC
        mac_address = pvm_util.sanitize_mac_for_api(vif['address'])

        # vlan id
        vlan_id = int(vif['details']['vlan'])

        # Redundancy: plugin sets from binding:profile, then conf, then default
        redundancy = int(vif['details']['redundancy'])

        # Capacity: plugin sets from binding:profile, then conf, then default
        capacity = vif['details']['capacity']
        maxcapacity = vif['details'].get('maxcapacity')

        vnic = pvm_card.VNIC.bld(
            self.adapter, vlan_id, slot_num=slot_num, mac_addr=mac_address,
            allowed_vlans=pvm_util.VLANList.NONE,
            allowed_macs=pvm_util.MACList.NONE)

        try:
            sriovtask.set_vnic_back_devs(vnic, pports, sys_w=msys,
                                         redundancy=redundancy,
                                         capacity=capacity,
                                         max_capacity=maxcapacity,
                                         check_port_status=True)
        except ValueError as ve:
            LOG.exception("Failed to set vNIC backing devices")
            msg = ''
            if ve.args:
                msg = ve.args[0]
            raise exception.VirtualInterfacePlugException(message=msg)

        return vnic.create(parent_type=pvm_lpar.LPAR,
                           parent_uuid=vm.get_pvm_uuid(self.instance))

    def unplug(self, vif, cna_w_list=None):
        mac = pvm_util.sanitize_mac_for_api(vif['address'])
        vnic = vm.get_vnics(
            self.adapter, self.instance, mac=mac, one_result=True)
        if not vnic:
            LOG.warning('Unable to unplug VIF with mac %(mac)s. No matching '
                        'vNIC was found on the instance. VIF: %(vif)s',
                        {'mac': mac, 'vif': vif}, instance=self.instance)
            return None
        vnic.delete()
        return vnic


class PvmMetaAttrs(list):
    """Represents meta attributes for a PowerVM Vif Driver.

    """

    def __init__(self, vif, instance):
        """Initializes meta attributes.

        :param vif: The virtual interface for the instance
        :param instance: The nova instance that the vif action will be run
                         against.
        """
        self.append('iface-id=%s' % (vif.get('ovs_interfaceid') or vif['id']))
        self.append('iface-status=active')
        self.append('attached-mac=%s' % vif['address'])
        self.append('vm-uuid=%s' % instance.uuid)

    def __str__(self):
        return ','.join(self)


class PvmOvsVifDriver(PvmVifDriver):
    """The Open vSwitch VIF driver for PowerVM."""

    def plug(self, vif, slot_num, new_vif=True):
        """Plugs a virtual interface (network) into a VM.

        Extends the Lio implementation.  Will make sure that the trunk device
        has the appropriate metadata (ex. port id) set on it so that the
        Open vSwitch agent picks it up properly.

        :param vif: The virtual interface to plug into the instance.
        :param slot_num: Which slot number to plug the VIF into.  May be None.
        :param new_vif: (Optional, Default: True) If set, indicates that it is
                        a brand new VIF.  If False, it indicates that the VIF
                        is already on the client but should be treated on the
                        bridge.
        :return: The new vif that was created.  Only returned if new_vif is
                 set to True.  Otherwise None is expected.
        """
        lpar_uuid = vm.get_pvm_uuid(self.instance)
        mgmt_uuid = pvm_par.get_mgmt_partition(self.adapter).uuid

        # There will only be one trunk wrap, as we have created with just
        # the mgmt lpar.  Next step is to connect to the OVS.
        mtu = vif['network'].get_meta('mtu')
        dev_name = _get_trunk_dev_name(vif)

        meta_attrs = PvmMetaAttrs(vif, self.instance)

        if new_vif:
            # Create the trunk and client adapter.
            return pvm_cna.crt_p2p_cna(
                self.adapter, self.host_uuid, lpar_uuid, [mgmt_uuid],
                CONF.powervm.pvm_vswitch_for_novalink_io, crt_vswitch=True,
                mac_addr=vif['address'], dev_name=dev_name,
                slot_num=slot_num, ovs_bridge=vif['network']['bridge'],
                ovs_ext_ids=str(meta_attrs), configured_mtu=mtu)[0]
        else:
            # Bug : https://bugs.launchpad.net/nova-powervm/+bug/1731548
            # When a host is rebooted, something is discarding tap devices for
            # VMs deployed with OVS vif. To prevent VMs losing network
            # connectivity, this is fixed by recreating the tap devices during
            # init of the nova compute service, which will call vif plug with
            # new_vif==False.

            # Find the CNA for this vif.
            # TODO(svenkat) improve performance by caching VIOS wrapper(s) and
            # CNA lists (in case >1 vif per VM).
            cna_w_list = vm.get_cnas(self.adapter, self.instance)
            cna_w = self._find_cna_for_vif(cna_w_list, vif)
            # Find the corresponding trunk adapter
            trunks = pvm_cna.find_trunks(self.adapter, cna_w)
            for trunk in trunks:
                # Set MTU, OVS external ids, and OVS bridge metadata
                # TODO(svenkat) set_parm_value calls should be replaced once
                # pypowervm supports setting these values directly.
                trunk.set_parm_value('ConfiguredMTU',
                                     mtu, attrib=pvm_c.ATTR_KSV160)
                trunk.set_parm_value('OvsPortExternalIds',
                                     meta_attrs, attrib=pvm_c.ATTR_KSV160)
                trunk.set_parm_value('OvsBridge',
                                     vif['network']['bridge'],
                                     attrib=pvm_c.ATTR_KSV160)
                # Updating the trunk adapter will cause NovaLink to reassociate
                # the tap device.
                trunk.update()

    @staticmethod
    def get_ovs_interfaceid(vif):
        """Returns the interface id to set for a given VIF.

        When a VIF is plugged for an Open vSwitch, it needs to have the
        interface ID set in the OVS metadata.  This returns what the
        appropriate interface id is.

        :param vif: The Nova network interface.
        """
        return vif.get('ovs_interfaceid') or vif['id']

    def unplug(self, vif, cna_w_list=None):
        """Unplugs a virtual interface (network) from a VM.

        Extends the base implementation, but before calling it will remove
        the adapter from the Open vSwitch and delete the trunk.

        :param vif: The virtual interface to plug into the instance.
        :param cna_w_list: (Optional, Default: None) The list of Client Network
                           Adapters from pypowervm.  Providing this input
                           allows for an improvement in operation speed.
        :return cna_w: The deleted Client Network Adapter.
        """
        # Need to find the adapters if they were not provided
        if not cna_w_list:
            cna_w_list = vm.get_cnas(self.adapter, self.instance)

        # Find the CNA for this vif.
        cna_w = self._find_cna_for_vif(cna_w_list, vif)
        if not cna_w:
            LOG.warning('Unable to unplug VIF with mac %(mac)s for. The VIF '
                        'was not found on the instance.',
                        {'mac': vif['address']}, instance=self.instance)
            return None

        # Find and delete the trunk adapters
        trunks = pvm_cna.find_trunks(self.adapter, cna_w)
        for trunk in trunks:
            trunk.delete()

        # Now delete the client CNA
        return super(PvmOvsVifDriver, self).unplug(vif, cna_w_list=cna_w_list)

    def pre_live_migrate_at_destination(self, vif, vea_vlan_mappings):
        """Performs the pre live migrate on the destination host.

        This method will create the trunk adapter on the destination host,
        set its link state up, and attach it to the integration OVS switch.
        It also updates the vea_vlan_mappings to indicate which unique
        hypervisor VLAN should be used for this VIF for the migration operation
        to complete properly.

        :param vif: The virtual interface that will be migrated.  This may be
                    called network_info in other portions of the code.
        :param vea_vlan_mappings: The VEA VLAN mappings.  Key is the vif
                                  mac address, value is the destination's
                                  target hypervisor VLAN.
        """
        self._cleanup_orphan_adapters(vif,
                                      CONF.powervm.pvm_vswitch_for_novalink_io)
        mgmt_wrap = pvm_par.get_mgmt_partition(self.adapter)
        dev = _get_trunk_dev_name(vif)

        meta_attrs = PvmMetaAttrs(vif, self.instance)

        mtu = vif['network'].get_meta('mtu')

        # Find a specific free VLAN and create the Trunk in a single atomic
        # action.
        cna_w = pvm_cna.crt_trunk_with_free_vlan(
            self.adapter, self.host_uuid, [mgmt_wrap.uuid],
            CONF.powervm.pvm_vswitch_for_novalink_io, dev_name=dev,
            ovs_bridge=vif['network']['bridge'],
            ovs_ext_ids=str(meta_attrs), configured_mtu=mtu)[0]

        # Save this data for the migration command.
        vea_vlan_mappings[vif['address']] = cna_w.pvid
        LOG.info("VIF with mac %(mac)s is going on trunk %(dev)s with PVID "
                 "%(pvid)s",
                 {'mac': vif['address'], 'dev': dev, 'pvid': cna_w.pvid},
                 instance=self.instance)

    def rollback_live_migration_at_destination(self, vif, vea_vlan_mappings):
        """Rolls back the pre live migrate on the destination host.

        Will delete the TrunkAdapter that pre_live_migrate_at_destination
        created with its unique hypervisor VLAN.  This uses the
        vea_vlan_mappings to provide the information as to what TrunkAdapter
        it should remove.

        :param vif: The virtual interface that was being migrated.  This may be
                    called network_info in other portions of the code.
        :param vea_vlan_mappings: The VEA VLAN mappings.  Key is the vif
                                  mac address, value is the destination's
                                  target hypervisor VLAN.
        """
        LOG.warning("Rolling back the live migrate of VIF with mac %(mac)s.",
                    {'mac': vif['address']}, instance=self.instance)

        # We know that we just attached the VIF to the NovaLink VM.  Search
        # for a trunk adapter with the PVID and vSwitch that we specified
        # above.  This is guaranteed to be unique.
        vlan = int(vea_vlan_mappings[vif['address']])
        vswitch_id = pvm_net.VSwitch.search(
            self.adapter, parent_type=pvm_ms.System, one_result=True,
            name=CONF.powervm.pvm_vswitch_for_novalink_io).switch_id

        # Find the trunk
        mgmt_wrap = pvm_par.get_mgmt_partition(self.adapter)
        child_adpts = pvm_net.CNA.get(self.adapter, parent=mgmt_wrap)
        trunk = None
        for adpt in child_adpts:
            # We need a trunk adapter (so check trunk_pri).  Then the trunk
            # is unique by PVID and PowerVM vSwitch ID.
            if (adpt.pvid == vlan and adpt.vswitch_id == vswitch_id and
                    adpt.trunk_pri):
                trunk = adpt
                break

        if trunk:
            # Delete the peer'd trunk adapter.
            LOG.warning("Deleting target side trunk adapter %(dev)s for "
                        "rollback operation", {'dev': trunk.dev_name},
                        instance=self.instance)
            trunk.delete()

    def pre_live_migrate_at_source(self, vif):
        """Performs the pre live migrate on the source host.

        This is executed directly before the migration is started on the source
        host.

        :param vif: The virtual interface that will be migrated.  This may be
                    called network_info in other portions of the code.
        :return: The list of TrunkAdapter's on the source that are hosting the
                 VM's vif.  Should only return data if those trunks should be
                 deleted after the migration.
        """
        # Right before the migration, we need to find the trunk on the source
        # host.
        mac = pvm_util.sanitize_mac_for_api(vif['address'])
        cna_w = pvm_net.CNA.search(
            self.adapter, parent_type=pvm_lpar.LPAR.schema_type,
            parent_uuid=vm.get_pvm_uuid(self.instance), one_result=True,
            mac=mac)

        return pvm_cna.find_trunks(self.adapter, cna_w)

    def post_live_migrate_at_source(self, vif):
        """Performs the post live migrate on the source host.

        :param vif: The virtual interface of an instance.  This may be
                    called network_info in other portions of the code.
        """
        self._cleanup_orphan_adapters(vif,
                                      CONF.powervm.pvm_vswitch_for_novalink_io)

    def _cleanup_orphan_adapters(self, vif, vswitch_name):
        """Finds and removes trunk VEAs that have no corresponding CNA."""
        # Find and delete orphan adapters with macs matching our vif
        orphans = pvm_cna.find_orphaned_trunks(self.adapter, vswitch_name)
        for orphan in orphans:
            if vm.norm_mac(orphan.mac) == vif['address']:
                orphan.delete()
