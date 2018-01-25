# Copyright 2014, 2018 IBM Corp.
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

import math
from nova.objects import fields
from oslo_log import log as logging
from oslo_serialization import jsonutils

from nova import conf as cfg


LOG = logging.getLogger(__name__)
CONF = cfg.CONF

# Power VM hypervisor info
# Normally, the hypervisor version is a string in the form of '8.0.0' and
# converted to an int with nova.virt.utils.convert_version_to_int() however
# there isn't currently a mechanism to retrieve the exact version.
# Complicating this is the fact that nova conductor only allows live migration
# from the source host to the destination if the source is equal to or less
# than the destination version.  PowerVM live migration limitations are
# checked by the PowerVM capabilities flags and not specific version levels.
# For that reason, we'll just publish the major level.
IBM_POWERVM_HYPERVISOR_VERSION = 8

# The types of LPARS that are supported.
POWERVM_SUPPORTED_INSTANCES = [
    (fields.Architecture.PPC64, fields.HVType.PHYP, fields.VMMode.HVM),
    (fields.Architecture.PPC64LE, fields.HVType.PHYP, fields.VMMode.HVM),
]

# cpu_info that will be returned by build_host_stats_from_entry()
HOST_STATS_CPU_INFO = jsonutils.dumps({'vendor': 'ibm', 'arch': 'ppc64'})


def build_host_resource_from_ms(ms_wrapper):
    """Build the host resource dict from an MS adapter wrapper

    This method builds the host resource dictionary from the
    ManagedSystem Entry wrapper

    :param ms_wrapper: ManagedSystem Entry Wrapper.
    """
    data = {}

    # Calculate the vcpus
    proc_units = float(ms_wrapper.proc_units_configurable)
    proc_units_avail = float(ms_wrapper.proc_units_avail)
    pu_used = proc_units - proc_units_avail
    data['vcpus'] = int(math.ceil(proc_units))
    data['vcpus_used'] = int(math.ceil(pu_used))

    data['memory_mb'] = ms_wrapper.memory_configurable
    data['memory_mb_used'] = (ms_wrapper.memory_configurable -
                              ms_wrapper.memory_free)

    data["hypervisor_type"] = fields.HVType.PHYP
    data["hypervisor_version"] = IBM_POWERVM_HYPERVISOR_VERSION
    data["hypervisor_hostname"] = CONF.host
    data["cpu_info"] = HOST_STATS_CPU_INFO
    data["numa_topology"] = None
    data["supported_instances"] = POWERVM_SUPPORTED_INSTANCES

    stats = {'proc_units': '%.2f' % proc_units,
             'proc_units_used': '%.2f' % pu_used,
             'memory_region_size': ms_wrapper.memory_region_size
             }
    data["stats"] = stats

    data["pci_passthrough_devices"] = _build_pci_json(ms_wrapper)

    return data


def _build_pci_json(sys_w):
    """Build the JSON string for the pci_passthrough_devices host resource.

    :param sys_w: pypowervm.wrappers.managed_system.System wrapper of the host.
    :return: JSON string representing a list of "PCI passthrough device" dicts,
             See nova.objects.pci_device.PciDevice.
    """
    # Produce SR-IOV PCI data.  Devices are validated by virtue of the network
    # name associated with their label, which must be cleared via an entry in
    # the pci_passthrough_whitelist in the nova.conf.  Each Claim allocates a
    # device and filters it from the list for subsequent claims; so we generate
    # the maximum number of "devices" (VFs) we could possibly create on each
    # port.  These are NOT real VFs.  The real VFs get created on the fly by
    # VNIC.create.
    pci_devs = [
        {"physical_network": pport.label or 'default',
         "label": pport.label or 'default',
         "dev_type": fields.PciDeviceType.SRIOV_VF,
         "address": '*:%d:%d.%d' % (sriov.sriov_adap_id, pport.port_id, vfn),
         "parent_addr": "*:*:*.*",
         "vendor_id": "*",
         "product_id": "*",
         "numa_node": 1}
        for sriov in sys_w.asio_config.sriov_adapters
        for pport in sriov.phys_ports
        for vfn in range(pport.supp_max_lps)]

    return jsonutils.dumps(pci_devs)
