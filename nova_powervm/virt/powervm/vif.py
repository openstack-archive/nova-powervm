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
import netifaces
import six

from nova import exception
from nova.network import linux_net
from nova import utils
from oslo_config import cfg
from oslo_utils import importutils
from pypowervm.tasks import cna as pvm_cna
from pypowervm.tasks import partition as pvm_par
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
               'ovs': 'nova_powervm.virt.powervm.vif.PvmOvsVifDriver'}

CONF = cfg.CONF


class VirtualInterfaceUnplugException(exception.NovaException):
    """Indicates that a VIF unplug failed."""
    # TODO(thorst) symmetrical to the exception in base Nova.  Evaluate
    # moving to Nova core.
    msg_fmt = _("Virtual interface unplug failed")


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


def plug(adapter, host_uuid, instance, vif, slot_mgr):
    """Plugs a virtual interface (network) into a VM.

    :param adapter: The pypowervm adapter.
    :param host_uuid: The host UUID for the PowerVM API.
    :param instance: The nova instance object.
    :param vif: The virtual interface to plug into the instance.
    :param slot_mgr: A NovaSlotManager.  Used to store/retrieve the client
                     slots used when a VIF is attached to the VM.
    """
    vif_drv = _build_vif_driver(adapter, host_uuid, instance, vif)

    # Get the slot number to use for the VIF creation.  May be None
    # indicating usage of the next highest available.
    slot_num = slot_mgr.build_map.get_vea_slot(vif['address'])

    # Invoke the plug
    cna_w = vif_drv.plug(vif, slot_num)

    # If the slot number hadn't been provided initially, save it for the
    # next rebuild
    if not slot_num:
        slot_mgr.register_cna(cna_w)


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
    cna_w = vif_drv.unplug(vif, cna_w_list=cna_w_list)
    if cna_w:
        slot_mgr.drop_cna(cna_w)


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
    def plug(self, vif, slot_num):
        """Plugs a virtual interface (network) into a VM.

        :param vif: The virtual interface to plug into the instance.
        :param slot_num: Which slot number to plug the VIF into.  May be None.
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
            cna_w_list = vm.get_cnas(self.adapter, self.instance,
                                     self.host_uuid)

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
            LOG.exception(e)
            raise VirtualInterfaceUnplugException()
        return cna_w

    def _find_cna_for_vif(self, cna_w_list, vif):
        for cna_w in cna_w_list:
            # If the MAC address matched, attempt the delete.
            if vm.norm_mac(cna_w.mac) == vif['address']:
                return cna_w
        return None


class PvmSeaVifDriver(PvmVifDriver):
    """The PowerVM Shared Ethernet Adapter VIF Driver."""

    def plug(self, vif, slot_num):
        lpar_uuid = vm.get_pvm_uuid(self.instance)

        # CNA's require a VLAN.  If the network doesn't provide, default to 1
        vlan = vif['network']['meta'].get('vlan', 1)
        cna_w = pvm_cna.crt_cna(self.adapter, self.host_uuid, lpar_uuid, vlan,
                                mac_addr=vif['address'], slot_num=slot_num)

        return cna_w


class PvmOvsVifDriver(PvmVifDriver):
    """The Open vSwitch VIF driver for PowerVM."""

    def plug(self, vif, slot_num):
        # Create the trunk and client adapter.
        lpar_uuid = vm.get_pvm_uuid(self.instance)
        mgmt_uuid = pvm_par.get_this_partition(self.adapter).uuid

        cna_w, trunk_wraps = pvm_cna.crt_p2p_cna(
            self.adapter, self.host_uuid, lpar_uuid, [mgmt_uuid],
            CONF.powervm.pvm_vswitch_for_ovs, crt_vswitch=True,
            mac_addr=vif['address'], slot_num=slot_num)

        # There will only be one trunk wrap, as we have created with just the
        # mgmt lpar.  Next step is to set the device up and connect to the OVS
        dev = self.get_trunk_dev_name(trunk_wraps[0])
        utils.execute('ip', 'link', 'set', dev, 'up', run_as_root=True)
        linux_net.create_ovs_vif_port(vif['network']['bridge'], dev,
                                      self.get_ovs_interfaceid(vif),
                                      vif['address'], self.instance.uuid)

        return cna_w

    def get_ovs_interfaceid(self, vif):
        return vif.get('ovs_interfaceid') or vif['id']

    def get_trunk_dev_name(self, trunk_w):
        # The mac address from the API is of format: 01234567890A
        # We need it in format: 01:23:45:67:89:0a
        # That means we need to add colons and lower case it
        mac_addr = ":".join(trunk_w.mac[i:i + 2]
                            for i in range(0, len(trunk_w.mac), 2)).lower()

        # Use netifaces to find the appropriate matching interface name
        # TODO(thorst) I don't like this logic.  Seems gross.
        ifaces = netifaces.interfaces()
        for iface in ifaces:
            link_addrs = netifaces.ifaddresses(iface)[netifaces.AF_LINK]
            for link_addr in link_addrs:
                if link_addr.get('addr') == mac_addr:
                    return iface

        raise exception.VirtualInterfacePlugException(
            _("Unable to find appropriate Trunk Device for mac "
              "%(mac_addr)s.") % {'mac_addr': mac_addr})

    def unplug(self, vif, cna_w_list=None):
        # Need to find the adapters if they were not provided
        if not cna_w_list:
            cna_w_list = vm.get_cnas(self.adapter, self.instance,
                                     self.host_uuid)

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
        for trunk in trunks:
            dev = self.get_trunk_dev_name(trunk)
            linux_net.delete_ovs_vif_port(vif['network']['bridge'], dev)
            trunk.delete()

        # Now delete the client CNA
        return super(PvmOvsVifDriver, self).unplug(vif, cna_w_list=cna_w_list)
