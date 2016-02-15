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
from oslo_utils import importutils
from pypowervm.tasks import cna as pvm_cna
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

VIF_MAPPING = {'pvm_sea': 'nova_powervm.virt.powervm.vif.PvmSeaVifDriver'}


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
            _("vif_type parameter must be present "
              "for this vif_driver implementation"))

    # Check the type to the implementations
    if VIF_MAPPING.get(vif['type']):
        return importutils.import_object(
            VIF_MAPPING.get(vif['type']), adapter, host_uuid, instance)

    # No matching implementation, raise error.
    raise exception.VirtualInterfacePlugException(
        _("Unable to find appropriate PowerVM VIF Driver for VIF type "
          "%(vif_type)s on instance %(instance)s") %
        {'vif_type': vif['type'], 'instance': instance.name})


def plug(adapter, host_uuid, instance, vif):
    """Plugs a virtual interface (network) into a VM.

    :param adapter: The pypowervm adapter.
    :param host_uuid: The host UUID for the PowerVM API.
    :param instance: The nova instance object.
    :param vif: The virtual interface to plug into the instance.
    """
    vif_drv = _build_vif_driver(adapter, host_uuid, instance, vif)
    vif_drv.plug(vif)


def unplug(adapter, host_uuid, instance, vif, cna_w_list=None):
    """Unplugs a virtual interface (network) from a VM.

    :param adapter: The pypowervm adapter.
    :param host_uuid: The host UUID for the PowerVM API.
    :param instance: The nova instance object.
    :param vif: The virtual interface to plug into the instance.
    :param cna_w_list: (Optional, Default: None) The list of Client Network
                       Adapters from pypowervm.  Providing this input
                       allows for an improvement in operation speed.
    """
    vif_drv = _build_vif_driver(adapter, host_uuid, instance, vif)
    vif_drv.unplug(vif, cna_w_list=cna_w_list)


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


def plug_secure_rmc_vif(adapter, instance, host_uuid):
    """Creates the Secure RMC Network Adapter on the VM.

    :param adapter: The pypowervm adapter API interface.
    :param instance: The nova instance to create the VIF against.
    :param host_uuid: The host system UUID.
    :return: The created network adapter wrapper.
    """
    lpar_uuid = vm.get_pvm_uuid(instance)
    return pvm_cna.crt_cna(adapter, host_uuid, lpar_uuid, SECURE_RMC_VLAN,
                           vswitch=SECURE_RMC_VSWITCH, crt_vswitch=True)


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
    def plug(self, vif):
        """Plugs a virtual interface (network) into a VM.

        :param vif: The virtual interface to plug into the instance.
        """
        pass

    def unplug(self, vif, cna_w_list=None):
        """Unplugs a virtual interface (network) from a VM.

        :param vif: The virtual interface to plug into the instance.
        :param cna_w_list: (Optional, Default: None) The list of Client Network
                           Adapters from pypowervm.  Providing this input
                           allows for an improvement in operation speed.
        """
        # This is a default implementation that most implementations will
        # require.

        # Need to find the adapters if they were not provided
        if not cna_w_list:
            cna_w_list = vm.get_cnas(self.adapter, self.instance,
                                     self.host_uuid)

        for cna_w in cna_w_list:
            # If the MAC address matched, attempt the delete.
            if vm.norm_mac(cna_w.mac) == vif['address']:
                LOG.info(_LI('Deleting VIF with mac %(mac)s for instance '
                             '%(inst)s.'),
                         {'mac': vif['address'], 'inst': self.instance.name})
                try:
                    cna_w.delete()
                except Exception as e:
                    LOG.error(_LE('Unable to unplug VIF with mac %(mac)s '
                                  'for instance %(inst)s.'),
                              {'mac': vif['address'],
                               'inst': self.instance.name})
                    LOG.exception(e)
                    raise VirtualInterfaceUnplugException()

                # Break from the loop as we had a successful unplug.
                # This prevents from going to 'else' loop.
                break
        else:
            LOG.warning(_LW('Unable to unplug VIF with mac %(mac)s for '
                            'instance %(inst)s.  The VIF was not found on '
                            'the instance.'),
                        {'mac': vif['address'], 'inst': self.instance.name})


class PvmSeaVifDriver(PvmVifDriver):
    """The PowerVM Shared Ethernet Adapter VIF Driver."""

    def plug(self, vif):
        lpar_uuid = vm.get_pvm_uuid(self.instance)
        # CNA's require a VLAN.  If the network doesn't provide, default to 1
        vlan = vif['network']['meta'].get('vlan', 1)
        return pvm_cna.crt_cna(self.adapter, self.host_uuid, lpar_uuid, vlan,
                               mac_addr=vif['address'])
