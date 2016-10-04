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

import abc
import logging
import six

from nova import exception
from nova.network import linux_net
from nova.network import model as network_model
from nova import utils
from oslo_concurrency import lockutils
from oslo_config import cfg
from oslo_utils import importutils
from pypowervm import exceptions as pvm_ex
from pypowervm.tasks import cna as pvm_cna
from pypowervm.tasks import partition as pvm_par
from pypowervm.tasks import sriov as sriovtask
from pypowervm import util as pvm_util
from pypowervm.wrappers import iocard as pvm_card
from pypowervm.wrappers import logical_partition as pvm_lpar
from pypowervm.wrappers import managed_system as pvm_ms
from pypowervm.wrappers import network as pvm_net

from nova_powervm.virt.powervm.i18n import _
from nova_powervm.virt.powervm.i18n import _LE
from nova_powervm.virt.powervm.i18n import _LI
from nova_powervm.virt.powervm.i18n import _LW
from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)

SECURE_RMC_VSWITCH = 'MGMTSWITCH'
SECURE_RMC_VLAN = 4094

VIF_MAPPING = {'pvm_sea': 'nova_powervm.virt.powervm.vif.PvmSeaVifDriver',
               'ovs': 'nova_powervm.virt.powervm.vif.PvmOvsVifDriver',
               'bridge': 'nova_powervm.virt.powervm.vif.PvmLBVifDriver',
               'pvm_sriov':
               'nova_powervm.virt.powervm.vif.PvmVnicSriovVifDriver'}

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
        LOG.exception(he.args[0])
        raise exception.VirtualInterfacePlugException()
    # Other exceptions are (hopefully) custom VirtualInterfacePlugException
    # generated lower in the call stack.

    # If the slot number hadn't been provided initially, save it for the
    # next rebuild
    if not slot_num and new_vif:
        slot_mgr.register_vnet(vnet_w)

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
    except pvm_ex.HttpError as he:
        # Log the message constructed by HttpError
        LOG.exception(he.args[0])
        raise exception.VirtualInterfaceUnplugException(reason=he.args[0])

    if vnet_w:
        slot_mgr.drop_vnet(vnet_w)


def post_live_migrate_at_destination(adapter, host_uuid, instance, vif):
    """Performs live migrate cleanup on the destination host.

    :param adapter: The pypowervm adapter.
    :param host_uuid: The host UUID for the PowerVM API.
    :param instance: The nova instance object.
    :param vif: The virtual interface that was migrated.  This may be called
                network_info in other portions of the code.
    """
    vif_drv = _build_vif_driver(adapter, host_uuid, instance, vif)
    vif_drv.post_live_migrate_at_destination(vif)


def post_live_migrate_at_source(adapter, host_uuid, instance, vif):
    """Performs live migrate cleanup on the source host.

    :param adapter: The pypowervm adapter.
    :param host_uuid: The host UUID for the PowerVM API.
    :param instance: The nova instance object.
    :param vif: The virtual interface that was migrated.  This may be called
                network_info in other portions of the code.
    """
    vif_drv = _build_vif_driver(adapter, host_uuid, instance, vif)
    vif_drv.post_live_migrate_at_source(vif)


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

    # Create the adapter.
    lpar_uuid = vm.get_pvm_uuid(instance)
    cna_w = pvm_cna.crt_cna(adapter, host_uuid, lpar_uuid, SECURE_RMC_VLAN,
                            vswitch=SECURE_RMC_VSWITCH, crt_vswitch=True,
                            slot_num=slot_num, mac_addr=mac)

    # Save the mgmt vif to the slot map.
    if not mac:
        slot_mgr.register_cna(cna_w)

    return cna_w


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
            LOG.warning(_LW('Unable to unplug VIF with mac %(mac)s for '
                            'instance %(inst)s.  The VIF was not found on '
                            'the instance.'),
                        {'mac': vif['address'], 'inst': self.instance.name})
            return None

        LOG.info(_LI('Deleting VIF with mac %(mac)s for instance %(inst)s.'),
                 {'mac': vif['address'], 'inst': self.instance.name})
        try:
            cna_w.delete()
        except Exception as e:
            LOG.error(_LE('Unable to unplug VIF with mac %(mac)s for instance '
                          '%(inst)s.'), {'mac': vif['address'],
                                         'inst': self.instance.name})
            raise exception.VirtualInterfaceUnplugException(reason=e.args[0])
        return cna_w

    def _find_cna_for_vif(self, cna_w_list, vif):
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

    def post_live_migrate_at_destination(self, vif):
        """Performs live migrate cleanup on the destination host.

        This is optional, child classes do not need to implement this.

        :param vif: The virtual interface that was migrated.
        """
        pass

    def post_live_migrate_at_source(self, vif):
        """Performs live migrate cleanup on the source host.

        This is optional, child classes do not need to implement this.

        :param vif: The virtual interface that was migrated.
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

        LOG.debug("Creating SEA based VIF with VLAN %s" % str(vlan))
        cna_w = pvm_cna.crt_cna(self.adapter, self.host_uuid, lpar_uuid, vlan,
                                mac_addr=vif['address'], slot_num=slot_num)

        return cna_w


@six.add_metaclass(abc.ABCMeta)
class PvmLioVifDriver(PvmVifDriver):
    """An abstract VIF driver that uses Linux I/O to host."""

    def get_trunk_dev_name(self, vif):
        """Returns the device name for the trunk adapter.

        A given VIF in the Linux I/O model will have a trunk adapter and a
        client adapter.  This will return the trunk adapter's name as it
        will appear on the management VM.

        :param vif: The nova network interface
        :return: The device name.
        """
        if 'devname' in vif:
            return vif['devname']
        return ("nic" + vif['id'])[:network_model.NIC_NAME_LEN]

    def plug(self, vif, slot_num, new_vif=True):
        """Plugs a virtual interface (network) into a VM.

        Creates a 'peer to peer' connection between the Management partition
        hosting the Linux I/O and the client VM.  There will be one trunk
        adapter for a given client adapter.

        The device will be 'up' on the mgmt partition.

        :param vif: The virtual interface to plug into the instance.
        :param slot_num: Which slot number to plug the VIF into.  May be None.
        :param new_vif: (Optional, Default: True) If set, indicates that it is
                        a brand new VIF.  If False, it indicates that the VIF
                        is already on the client but should be treated on the
                        bridge.
        :return: The new vif that was created.  Only returned if new_vif is
                 set to True.  Otherwise None is expected.
        """
        dev_name = self.get_trunk_dev_name(vif)

        if new_vif:
            # Create the trunk and client adapter.
            lpar_uuid = vm.get_pvm_uuid(self.instance)
            mgmt_uuid = pvm_par.get_this_partition(self.adapter).uuid
            cna_w = pvm_cna.crt_p2p_cna(
                self.adapter, self.host_uuid, lpar_uuid, [mgmt_uuid],
                CONF.powervm.pvm_vswitch_for_novalink_io, crt_vswitch=True,
                mac_addr=vif['address'], dev_name=dev_name,
                slot_num=slot_num)[0]
        else:
            cna_w = None

        # Make sure to just run the up just in case.
        utils.execute('ip', 'link', 'set', dev_name, 'up', run_as_root=True)

        return cna_w


class PvmLBVifDriver(PvmLioVifDriver):
    """The Linux Bridge VIF driver for PowerVM."""

    def plug(self, vif, slot_num, new_vif=True):
        """Plugs a virtual interface (network) into a VM.

        Extends the base Lio implementation.  Will make sure that the bridge
        supports the trunk adapter.

        :param vif: The virtual interface to plug into the instance.
        :param slot_num: Which slot number to plug the VIF into.  May be None.
        :param new_vif: (Optional, Default: True) If set, indicates that it is
                        a brand new VIF.  If False, it indicates that the VIF
                        is already on the client but should be treated on the
                        bridge.
        :return: The new vif that was created.  Only returned if new_vif is
                 set to True.  Otherwise None is expected.
        """
        # Only call the parent if it is truly a new VIF
        if new_vif:
            cna_w = super(PvmLBVifDriver, self).plug(vif, slot_num)
        else:
            cna_w = None

        # Similar to libvirt's vif.py plug_bridge.  Need to attach the
        # interface to the bridge.
        linux_net.LinuxBridgeInterfaceDriver.ensure_bridge(
            vif['network']['bridge'], self.get_trunk_dev_name(vif))

        return cna_w

    def unplug(self, vif, cna_w_list=None):
        """Unplugs a virtual interface (network) from a VM.

        Extends the base implementation, but before invoking it will remove
        itself from the bridge it is connected to and delete the corresponding
        trunk device on the mgmt partition.

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
            LOG.warning(_LW('Unable to unplug VIF with mac %(mac)s for '
                            'instance %(inst)s.  The VIF was not found on '
                            'the instance.'),
                        {'mac': vif['address'], 'inst': self.instance.name})
            return None

        # Find and delete the trunk adapters
        trunks = pvm_cna.find_trunks(self.adapter, cna_w)

        dev_name = self.get_trunk_dev_name(vif)
        utils.execute('ip', 'link', 'set', dev_name, 'down', run_as_root=True)
        try:
            utils.execute('brctl', 'delif', vif['network']['bridge'],
                          dev_name, run_as_root=True)
        except Exception as e:
            LOG.warning(_LW('Unable to delete device %(dev_name)s from bridge '
                            '%(bridge)s. Error: %(error)s'),
                        {'dev_name': dev_name,
                         'bridge': vif['network']['bridge'],
                         'error': e.message})
        for trunk in trunks:
            trunk.delete()

        # Now delete the client CNA
        return super(PvmLBVifDriver, self).unplug(vif, cna_w_list=cna_w_list)


class PvmVnicSriovVifDriver(PvmVifDriver):
    """The SR-IOV VIF driver for PowerVM."""

    def plug(self, vif, slot_num, new_vif=True):
        if not new_vif:
            return None

        physnet = vif.get_physical_network()
        LOG.debug("Plugging vNIC SR-IOV vif for physical network "
                  "'%(physnet)s' into instance %(inst)s.",
                  {'physnet': physnet, 'inst': self.instance.name})

        # Physical ports for the given physical network
        pports = vif['details']['physical_ports']
        if not pports:
            raise exception.VirtualInterfacePlugException(
                _("Unable to find SR-IOV physical ports for physical "
                  "network '%(physnet)s' (instance %(inst)s).  VIF: %(vif)s") %
                {'physnet': physnet, 'inst': self.instance.name, 'vif': vif})

        # MAC
        mac_address = pvm_util.sanitize_mac_for_api(vif['address'])

        # vlan id
        vlan_id = int(vif['details']['vlan'])

        # Redundancy: plugin sets from binding:profile, then conf, then default
        redundancy = int(vif['details']['redundancy'])

        # Capacity: plugin sets from binding:profile, then conf, then default
        capacity = vif['details']['capacity']

        vnic = pvm_card.VNIC.bld(
            self.adapter, vlan_id, slot_num=slot_num, mac_addr=mac_address,
            allowed_vlans=pvm_util.VLANList.NONE,
            allowed_macs=pvm_util.MACList.NONE)

        sriovtask.set_vnic_back_devs(
            vnic, pports, min_redundancy=redundancy, max_redundancy=redundancy,
            capacity=capacity, check_port_status=True)

        return vnic.create(parent_type=pvm_lpar.LPAR,
                           parent_uuid=vm.get_pvm_uuid(self.instance))

    def unplug(self, vif, cna_w_list=None):
        mac = pvm_util.sanitize_mac_for_api(vif['address'])
        vnic = vm.get_vnics(
            self.adapter, self.instance, mac=mac, one_result=True)
        if not vnic:
            LOG.warning(_LW('Unable to unplug VIF with mac %(mac)s for '
                            'instance %(inst)s.  No matching vNIC was found '
                            'on the instance.  VIF: %(vif)s'),
                        {'mac': mac, 'inst': self.instance.name, 'vif': vif})
            return None
        vnic.delete()
        return vnic


class PvmOvsVifDriver(PvmLioVifDriver):
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
        # Only call the parent if it is truly a new VIF
        if new_vif:
            cna_w = super(PvmOvsVifDriver, self).plug(vif, slot_num)
        else:
            cna_w = None

        # There will only be one trunk wrap, as we have created with just the
        # mgmt lpar.  Next step is to set the device up and connect to the OVS
        dev_name = self.get_trunk_dev_name(vif)
        utils.execute('ip', 'link', 'set', dev_name, 'up', run_as_root=True)
        linux_net.create_ovs_vif_port(vif['network']['bridge'], dev_name,
                                      self.get_ovs_interfaceid(vif),
                                      vif['address'], self.instance.uuid)

        return cna_w

    def get_ovs_interfaceid(self, vif):
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
            LOG.warning(_LW('Unable to unplug VIF with mac %(mac)s for '
                            'instance %(inst)s.  The VIF was not found on '
                            'the instance.'),
                        {'mac': vif['address'], 'inst': self.instance.name})
            return None

        # Find and delete the trunk adapters
        trunks = pvm_cna.find_trunks(self.adapter, cna_w)
        dev = self.get_trunk_dev_name(vif)
        linux_net.delete_ovs_vif_port(vif['network']['bridge'], dev)
        for trunk in trunks:
            trunk.delete()

        # Now delete the client CNA
        return super(PvmOvsVifDriver, self).unplug(vif, cna_w_list=cna_w_list)

    def post_live_migrate_at_destination(self, vif):
        """Performs live migrate cleanup on the destination host.

        This is optional, child classes do not need to implement this.

        :param vif: The virtual interface that was migrated.
        """
        # 1) Find a free vlan to use
        # 2) Update the migrated CNA to use the new vlan that was found
        #    and ensure that the CNA is enabled
        # 3) Create a trunk adapter on the destination of the migration
        #    using the same vlan as the CNA

        mgmt_wrap = pvm_par.get_this_partition(self.adapter)
        dev_name = self.get_trunk_dev_name(vif)
        mac = pvm_util.sanitize_mac_for_api(vif['address'])

        # Get vlan
        vswitch_w = pvm_net.VSwitch.search(
            self.adapter, parent_type=pvm_ms.System.schema_type,
            one_result=True, parent_uuid=self.host_uuid,
            name=CONF.powervm.pvm_vswitch_for_novalink_io)
        cna = vm.get_cnas(
            self.adapter, self.instance, mac=mac, one_result=True)

        # Assigns a free vlan (which is returned) to the cna_list
        # also enable the cna
        cna = pvm_cna.assign_free_vlan(
            self.adapter, self.host_uuid, vswitch_w, cna)
        # Create a trunk with the vlan_id
        trunk_adpt = pvm_net.CNA.bld(
            self.adapter, cna.pvid, vswitch_w.related_href, trunk_pri=1,
            dev_name=dev_name)
        trunk_adpt.create(parent=mgmt_wrap)

        utils.execute('ip', 'link', 'set', dev_name, 'up', run_as_root=True)
        linux_net.create_ovs_vif_port(vif['network']['bridge'], dev_name,
                                      self.get_ovs_interfaceid(vif),
                                      vif['address'], self.instance.uuid)

    @lockutils.synchronized("post_migration_pvm_ovs")
    def post_live_migrate_at_source(self, vif):
        """Performs live migrate cleanup on the source host.

        This is optional, child classes do not need to implement this.

        :param vif: The virtual interface that was migrated.
        """
        # Deletes orphaned trunks
        orphaned_trunks = pvm_cna.find_orphaned_trunks(
            self.adapter, CONF.powervm.pvm_vswitch_for_novalink_io)
        dev = self.get_trunk_dev_name(vif)
        linux_net.delete_ovs_vif_port(vif['network']['bridge'], dev)
        for orphan in orphaned_trunks:
            orphan.delete()
