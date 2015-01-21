# Copyright 2014 IBM Corp.
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
from nova.compute import arch
from nova.compute import hv_type
from nova.compute import vm_mode
from nova.openstack.common import log as logging

from oslo.serialization import jsonutils

from pypowervm.wrappers import constants as pvm_consts
from pypowervm.wrappers import managed_system as msentry_wrapper

LOG = logging.getLogger(__name__)

# Power VM hypervisor info
IBM_POWERVM_HYPERVISOR_VERSION = '7.1'

# The types of LPARS that are supported.
POWERVM_SUPPORTED_INSTANCES = jsonutils.dumps([(arch.PPC64,
                                                hv_type.PHYP,
                                                vm_mode.HVM),
                                               (arch.PPC64LE,
                                                hv_type.PHYP,
                                                vm_mode.HVM)])

# cpu_info that will be returned by build_host_stats_from_entry()
HOST_STATS_CPU_INFO = jsonutils.dumps({'vendor': 'ibm', 'arch': 'ppc64'})


def parse_mtm(mtm_serial):
    mtm, serial = mtm_serial.split('_', 1)
    mt = mtm[0:4]
    md = mtm[4:7]
    return mt, md, serial


def get_mtm_serial(msentry):
    mt = msentry.machine_type
    md = msentry.model
    ms = msentry.serial
    return mt + md + '_' + ms


def find_entry_by_mtm_serial(resp, mtm_serial):
    mt, md, serial = parse_mtm(mtm_serial)
    entries = resp.feed.findentries(pvm_consts.MACHINE_SERIAL, serial)
    if entries is None:
        return None
    else:
        LOG.info("Entry %s" % entries)

    # Confirm same model and type
    for entry in entries:
        wrapper = msentry_wrapper.ManagedSystem(entry)
        if (wrapper.machine_type == mt and wrapper.model == md):
            return entry

    # No matching MTM Serial was found
    return None


def build_host_resource_from_entry(msentry):
    """Build the host resource dict from an MS adapter wrapper

    This method builds the host resource dictionary from the
    ManagedSystem Entry wrapper

    :param msentry: ManagedSystem Entry Wrapper.
    """
    data = {}

    # Calculate the vcpus
    proc_units = msentry.proc_units_configurable
    proc_units_avail = msentry.proc_units_avail
    pu_used = float(proc_units) - float(proc_units_avail)
    data['vcpus'] = int(math.ceil(float(proc_units)))
    data['vcpus_used'] = int(math.ceil(pu_used))

    data['memory_mb'] = msentry.memory_configurable
    data['memory_mb_used'] = (msentry.memory_configurable -
                              msentry.memory_free)

    # TODO(IBM): make the local gb large for now
    data["local_gb"] = (1 << 21)
    data["local_gb_used"] = 0

    data["hypervisor_type"] = hv_type.PHYP
    data["hypervisor_version"] = IBM_POWERVM_HYPERVISOR_VERSION
    # TODO(IBM): Get the right host name
    data["hypervisor_hostname"] = get_mtm_serial(msentry)
    data["cpu_info"] = HOST_STATS_CPU_INFO
    data["numa_topology"] = None
    data["supported_instances"] = POWERVM_SUPPORTED_INSTANCES

    stats = {'proc_units': '%.2f' % float(proc_units),
             'proc_units_used': '%.2f' % pu_used
             }
    data["stats"] = stats

    return data
