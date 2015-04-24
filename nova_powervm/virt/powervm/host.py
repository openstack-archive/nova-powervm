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
import subprocess

from oslo_concurrency import lockutils
from oslo_log import log as logging
from oslo_serialization import jsonutils

from pypowervm.tasks.monitor import util as pcm_util

LOG = logging.getLogger(__name__)

# Power VM hypervisor info
IBM_POWERVM_HYPERVISOR_VERSION = '710'

# The types of LPARS that are supported.
POWERVM_SUPPORTED_INSTANCES = jsonutils.dumps([(arch.PPC64,
                                                hv_type.PHYP,
                                                vm_mode.HVM),
                                               (arch.PPC64LE,
                                                hv_type.PHYP,
                                                vm_mode.HVM)])

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
    proc_units = ms_wrapper.proc_units_configurable
    proc_units_avail = ms_wrapper.proc_units_avail
    pu_used = float(proc_units) - float(proc_units_avail)
    data['vcpus'] = int(math.ceil(float(proc_units)))
    data['vcpus_used'] = int(math.ceil(pu_used))

    data['memory_mb'] = ms_wrapper.memory_configurable
    data['memory_mb_used'] = (ms_wrapper.memory_configurable -
                              ms_wrapper.memory_free)

    data["hypervisor_type"] = hv_type.PHYP
    data["hypervisor_version"] = IBM_POWERVM_HYPERVISOR_VERSION
    data["hypervisor_hostname"] = ms_wrapper.mtms.mtms_str
    data["cpu_info"] = HOST_STATS_CPU_INFO
    data["numa_topology"] = None
    data["supported_instances"] = POWERVM_SUPPORTED_INSTANCES

    stats = {'proc_units': '%.2f' % float(proc_units),
             'proc_units_used': '%.2f' % pu_used,
             'memory_region_size': ms_wrapper.memory_region_size
             }
    data["stats"] = stats

    return data


class HostCPUStats(pcm_util.MetricCache):
    """Transforms the PowerVM CPU metrics into the Nova format.

    PowerVM only gathers the CPU statistics once every 30 seconds.  It does
    this to reduce overhead.  There is a function to gather statistics quicker,
    but that can be very expensive.  Therefore, to ensure that the client's
    workload is not impacted, these 'longer term' metrics will be used.

    This class builds off of a base pypowervm function where it can obtain
    the samples through a PCM 'cache'.  If a new sample is available, the cache
    pulls the sample.  If it is not, the existing sample is used.

    This can result in multiple, quickly successive calls to the host stats
    returning the same data (because a new sample may not be available yet).

    The class analyzes the data and collapses it down to the format needed by
    the Nova manager.
    """

    def __init__(self, adapter, host_uuid):
        """Creates an instance of the HostCPUStats.

        :param adapter: The pypowervm Adapter.
        :param host_uuid: The UUID of the host CEC to maintain a metrics
                          cache for.
        """
        # A dictionary to store the number of cycles spent.  This is defined
        # in the _update_internal_metric method.
        self.cur_data, self.prev_data = None, None

        # Invoke the parent to seed the metrics.  Don't include VIO - will
        # result in quicker calls.
        super(HostCPUStats, self).__init__(adapter, host_uuid,
                                           include_vio=False)

    @lockutils.synchronized('pvm_host_metrics_get')
    def get_host_cpu_stats(self):
        """Returns the currently known host CPU stats.

        :return: The dictionary (as defined by the compute driver's
                 get_host_cpu_stats).  If insufficient data is available,
                 then 'None' will be returned.
        """
        # Refresh if needed.  Will no-op if no refresh is required.
        self._refresh_if_needed()

        # The invoking code needs the total cycles for this to work properly.
        # Return the dictionary format of the cycles as derived by the
        # _update_internal_metric method.  If there is no data yet, None would
        # be the result.
        return self.cur_data

    def _update_internal_metric(self):
        """Uses the latest stats from the cache, and parses to Nova format.

        This method is invoked by the parent class after the raw metrics are
        updated.
        """
        # If there is no 'new' data (perhaps sampling is not turned on) then
        # return no data.
        if self.cur_phyp is None:
            self.cur_data = None
            return

        # Move the current data to the previous.  The previous data is used
        # for some internal calculations.  Blank out the current data just
        # in case of error.  Don't want to persist two copies of same.
        self.prev_data, self.cur_data = self.cur_data, None

        # Now we need the firmware cycles.
        fw_cycles = self.cur_phyp.sample.system_firmware.utilized_proc_cycles

        # Compute the max cycles.
        tot_cycles = self._get_total_cycles()

        # Get the total user cycles.
        user_cycles = self._gather_user_cycles()

        # Idle is the subtraction of all.
        idle_cycles = tot_cycles - user_cycles - fw_cycles

        # Get the processor frequency.
        freq = self._get_cpu_freq()

        # Now save these cycles to the internal data structure.
        self.cur_data = {'idle': idle_cycles, 'kernel': fw_cycles,
                         'user': user_cycles, 'iowait': 0, 'frequency': freq}

    def _gather_user_cycles(self):
        """The estimated total user cycles.

        The sample data includes information about how much CPU has been used
        by workloads and the Virtual I/O Servers.  There is not one global
        counter that can be used to obtain the CPU spent cycles.

        This method will calculate the delta of workload (and I/O Server)
        cycles between the previous sample and the current sample, and then
        add it to the previous 'user cycles'.

        There are edge cases for this however.  If a VM is deleted or migrated
        its cycles will no longer be taken into account.  The algorithm takes
        this into account by building on top of the previous sample's user
        cycles.

        :return: Estimated cycles spent on workload (including VMs and Virtual
                 I/O Server).  This represents the entire server's current
                 'user' load.
        """
        # Current samples should be guaranteed to be there.
        vm_cur_samples = self.cur_phyp.sample.lpars
        vios_cur_samples = self.cur_phyp.sample.vioses

        # The previous samples may not have been there.
        vm_prev_samples, vios_prev_samples = None, None
        if self.prev_phyp is not None:
            vm_prev_samples = self.prev_phyp.sample.lpars
            vios_prev_samples = self.prev_phyp.sample.vioses

        # Gather the delta cycles between the previous and current data sets
        vm_delta_cycles = self._delta_proc_cycles(vm_cur_samples,
                                                  vm_prev_samples)
        vios_delta_cycles = self._delta_proc_cycles(vios_cur_samples,
                                                    vios_prev_samples)

        # The used cycles is the total of used cycles from before along with
        # the new delta cycles.
        prev_user_cycles = (0 if self.prev_data is None
                            else self.prev_data['user'])
        return prev_user_cycles + vm_delta_cycles + vios_delta_cycles

    @staticmethod
    def _get_cpu_freq():
        # The output will be similar to '4116.000000MHz' on a POWER system.
        cmd = ['/usr/bin/awk', '/clock/ {print $3; exit}', '/proc/cpuinfo']
        return float(subprocess.check_output(cmd).rstrip("MHz\n"))

    def _delta_proc_cycles(self, samples, prev_samples):
        """Sums all the processor delta cycles for a set of VM/VIOS samples.

        This sum is the difference from the last sample to the current sample.

        :param samples: A set of PhypVMSample or PhypViosSample samples.
        :param prev_samples: The set of the previous samples.  May be None.
        :return: The cycles spent on workload across all of the samples.
        """
        # Determine the user cycles spent between the last sample and the
        # current.
        user_cycles = 0
        for lpar_sample in samples:
            prev_sample = self._find_prev_sample(lpar_sample, prev_samples)
            user_cycles += self._delta_user_cycles(lpar_sample, prev_sample)
        return user_cycles

    @staticmethod
    def _delta_user_cycles(cur_sample, prev_sample):
        """Determines the delta of user cycles from the cur and prev sample.

        :param cur_sample: The current sample.
        :param prev_sample: The previous sample.  May be None.
        :return: The difference in cycles between the two samples.  If the data
                 only exists in the current sample (indicates a new workload),
                 then all of the cycles from the current sample will be
                 considered the delta.
        """
        prev_amount = (0 if prev_sample is None else
                       prev_sample.processor.util_cap_proc_cycles +
                       prev_sample.processor.util_uncap_proc_cycles)
        cur_amount = (cur_sample.processor.util_cap_proc_cycles +
                      cur_sample.processor.util_uncap_proc_cycles)
        return cur_amount - prev_amount

    @staticmethod
    def _find_prev_sample(sample, prev_samples):
        """Finds the previous VM Sample for a given current sample.

        :param sample: The current sample.
        :param prev_samples: The previous samples to search through.
        :return: The previous sample, if it exists.  None otherwise.
        """
        # Will occur if there are no previous samples.
        if prev_samples is None:
            return None
        for prev_sample in prev_samples:
            if prev_sample.id == sample.id and prev_sample.name == sample.name:
                return prev_sample
        return None

    def _get_total_cycles(self):
        """Returns the 'total cycles' on the system.

        :return: The estimated total cycles spent
        """
        sample = self.cur_phyp.sample

        # Gather the estimated cycle count
        total_procs = sample.processor.configurable_proc_units
        cycles_per_sec = sample.time_based_cycles
        est_total_cycles_per_sec = total_procs * cycles_per_sec

        return est_total_cycles_per_sec
