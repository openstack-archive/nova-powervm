# Copyright 2015 IBM Corp.
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

from oslo_log import log as logging
from pypowervm import adapter as pvm_adpt
from pypowervm.tasks.monitor import util as pvm_mon_util
from pypowervm.utils import uuid as pvm_uuid
from pypowervm.wrappers import managed_system as pvm_ms

from ceilometer.compute.virt import inspector as virt_inspector
from ceilometer.i18n import _

LOG = logging.getLogger(__name__)


class PowerVMInspector(virt_inspector.Inspector):
    """The implementation of the inspector for the PowerVM hypervisor.

    This code requires that it is run on the PowerVM Compute Host directly.
    Utilizes the pypowervm library to gather the instance metrics.
    """

    def __init__(self):
        super(PowerVMInspector, self).__init__()

        # Build the adapter to the PowerVM API.
        adpt = pvm_adpt.Adapter(pvm_adpt.Session())

        # Get the host system UUID
        host_uuid = self._get_host_uuid(adpt)

        # Ensure that metrics gathering is running for the host.
        pvm_mon_util.ensure_ltm_monitors(adpt, host_uuid)

        # Get the VM Metric Utility
        self.vm_metrics = pvm_mon_util.LparMetricCache(adpt, host_uuid)

    @staticmethod
    def _puuid(instance):
        """Derives the PowerVM UUID for an instance.

        :param instance: The OpenStack instance object.
        :return: The PowerVM API's UUID for the instance.
        """
        return pvm_uuid.convert_uuid_to_pvm(instance.id).upper()

    def _get_host_uuid(self, adpt):
        """Returns the Host systems UUID for pypowervm.

        The pypowervm API needs a UUID of the server that it is managing.  This
        method returns the UUID of that host.

        :param adpt: The pypowervm adapter.
        :return: The UUID of the host system.
        """
        hosts = pvm_ms.System.wrap(adpt.read(pvm_ms.System.schema_type))
        if len(hosts) != 1:
            raise Exception(_("Expected exactly one host; found %d"),
                            len(hosts))
        LOG.debug("Host UUID: %s" % hosts[0].uuid)
        return hosts[0].uuid

    def inspect_cpus(self, instance):
        """Inspect the CPU statistics for an instance.

        :param instance: the target instance
        :return: the number of CPUs and cumulative CPU time
        """
        uuid = self._puuid(instance)
        cur_date, cur_metric = self.vm_metrics.get_latest_metric(uuid)

        # If the current metric is none, then the instance can not be found in
        # the sample set.  An error should be raised.
        if cur_metric is None:
            raise virt_inspector.InstanceNotFoundException(
                _('VM %s not found in PowerVM Metrics Sample') % instance.name)

        cpu_time = (cur_metric.processor.util_cap_proc_cycles +
                    cur_metric.processor.util_uncap_proc_cycles)
        return virt_inspector.CPUStats(number=cur_metric.processor.virt_procs,
                                       time=cpu_time)

    def inspect_cpu_util(self, instance, duration=None):
        """Inspect the CPU Utilization (%) for an instance.

        :param instance: the target instance
        :param duration: the last 'n' seconds, over which the value should be
               inspected.

               The PowerVM implementation does not make use of the duration
               field.
        :return: the percentage of CPU utilization
        """
        # The duration is ignored.  There is precedent for this in other
        # inspectors if the platform doesn't support duration.
        #
        # Given the nature of PowerVM to collect samples over coarse periods
        # of time, it does not lend well to duration based collection.
        # Therefore this works by gathering the latest utilization from the
        # samples and ignores the duration.

        # Get the current and previous sample.  Delta is performed between
        # these two.
        uuid = self._puuid(instance)
        cur_date, cur_metric = self.vm_metrics.get_latest_metric(uuid)
        prev_date, prev_metric = self.vm_metrics.get_previous_metric(uuid)

        # If the current is none, then the instance can not be found in the
        # sample and an error should be raised.
        if cur_metric is None:
            raise virt_inspector.InstanceNotFoundException(
                _('VM %s not found in PowerVM Metrics Sample') % instance.name)

        # Get the current data.
        cur_util_cap = cur_metric.processor.util_cap_proc_cycles
        cur_util_uncap = cur_metric.processor.util_uncap_proc_cycles
        cur_idle = cur_metric.processor.idle_proc_cycles
        cur_donated = cur_metric.processor.donated_proc_cycles
        cur_entitled = cur_metric.processor.entitled_proc_cycles

        # Get the previous sample data
        if prev_metric is None:
            # If there is no previous sample, we blanket these to zero.  All
            # the cycles that were used in the current sample are 'fully'
            # utilized.
            prev_util_cap = 0
            prev_util_uncap = 0
            prev_idle = 0
            prev_donated = 0
            prev_entitled = 0
        else:
            prev_util_cap = prev_metric.processor.util_cap_proc_cycles
            prev_util_uncap = prev_metric.processor.util_uncap_proc_cycles
            prev_idle = prev_metric.processor.idle_proc_cycles
            prev_donated = prev_metric.processor.donated_proc_cycles
            prev_entitled = prev_metric.processor.entitled_proc_cycles

        # Utilization can be driven by multiple factors on PowerVM.
        # PowerVM has 'entitled' cycles.  These are cycles that, if the VM
        # needs them, they get them no matter what.
        #
        # In terms of how those cycles are returned from the API:
        #   util_cap_proc_cycles - How many cycles from the guaranteed
        #          capacity were used.
        #   util_uncap_proc_cycles - How many cycles were used that were
        #          taken from spare (which is either unused processors cycles
        #          or donated cycles from other VMs).
        #   idle_proc_cycles - How many cycles (as reported by the OS to the
        #          hypervisor) were reported as idle.
        #   donated_proc_cycles - Cycles that were not needed by this VM that
        #          were given to another VM in need of cycles.
        #
        #
        # So the final utilization equation is:
        #   (util cap + util uncap - idle - donated) / entitled
        #
        # It is important to note that idle and donated proc cycles are
        # included in the 'util_cap_proc_cycles'.  That is why they are
        # subtracted out.
        #
        # The interesting aspect of this is that the CPU Utilization can go
        # dramatically above 100% if there are free processors or if the
        # other workloads are in a lull.
        util_cap = cur_util_cap - prev_util_cap
        util_uncap = cur_util_uncap - prev_util_uncap
        idle = cur_idle - prev_idle
        donated = cur_donated - prev_donated
        entitled = cur_entitled - prev_entitled

        util = float(util_cap + util_uncap - idle - donated) / float(entitled)

        # Utilization is reported as percents.  Therefore, multiply by 100.0
        # to get a readable percentage based format.
        return virt_inspector.CPUUtilStats(util=util * 100.0)
