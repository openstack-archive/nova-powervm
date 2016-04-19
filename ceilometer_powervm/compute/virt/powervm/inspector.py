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

import datetime

from oslo_log import log as logging
from pypowervm import adapter as pvm_adpt
from pypowervm.helpers import log_helper as log_hlp
from pypowervm.helpers import vios_busy as vio_hlp
from pypowervm.tasks.monitor import util as pvm_mon_util
from pypowervm.utils import uuid as pvm_uuid
from pypowervm.wrappers import logical_partition as pvm_lpar
from pypowervm.wrappers import managed_system as pvm_ms
from pypowervm.wrappers import network as pvm_net

from ceilometer.compute.virt import inspector as virt_inspector
from ceilometer_powervm.compute.virt.powervm.i18n import _
from ceilometer_powervm.compute.virt.powervm.i18n import _LW

LOG = logging.getLogger(__name__)


class PowerVMInspector(virt_inspector.Inspector):
    """The implementation of the inspector for the PowerVM hypervisor.

    This code requires that it is run on the PowerVM Compute Host directly.
    Utilizes the pypowervm library to gather the instance metrics.
    """

    def __init__(self):
        super(PowerVMInspector, self).__init__()

        # Build the adapter.  May need to attempt the connection multiple times
        # in case the REST server is starting.
        session = pvm_adpt.Session(conn_tries=300)
        self.adpt = pvm_adpt.Adapter(
            session, helpers=[log_hlp.log_helper,
                              vio_hlp.vios_busy_retry_helper])

        # Get the host system UUID
        host_uuid = self._get_host_uuid(self.adpt)

        # Ensure that metrics gathering is running for the host.
        pvm_mon_util.ensure_ltm_monitors(self.adpt, host_uuid)

        # Get the VM Metric Utility
        self.vm_metrics = pvm_mon_util.LparMetricCache(self.adpt, host_uuid)

    @staticmethod
    def _puuid(instance):
        """Derives the PowerVM UUID for an instance.

        :param instance: The OpenStack instance object.
        :return: The PowerVM API's UUID for the instance.
        """
        return pvm_uuid.convert_uuid_to_pvm(instance.id).upper()

    def _get_host_uuid(self, adpt):
        """Returns the Host system's UUID for pypowervm.

        The pypowervm API needs a UUID of the server that it is managing.  This
        method returns the UUID of that host.

        :param adpt: The pypowervm adapter.
        :return: The UUID of the host system.
        """
        hosts = pvm_ms.System.wrap(adpt.read(pvm_ms.System.schema_type))
        if len(hosts) != 1:
            raise Exception(_("Expected exactly one host; found %d."),
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
                _('VM %s not found in PowerVM Metrics Sample.') %
                instance.name)

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
                _('VM %s not found in PowerVM Metrics Sample.') %
                instance.name)

        # Get the current data.
        cur_util_cap = cur_metric.processor.util_cap_proc_cycles
        cur_util_uncap = cur_metric.processor.util_uncap_proc_cycles
        cur_idle = cur_metric.processor.idle_proc_cycles
        cur_donated = cur_metric.processor.donated_proc_cycles
        cur_entitled = cur_metric.processor.entitled_proc_cycles

        # Get the previous sample data
        if prev_metric is None:
            # If there is no previous sample, that is either a new VM or is
            # a live migrated system.  A live migrated system will pull all
            # of its metrics with it.  The issue with this is it could have
            # CPU cycles for months of run time.  So we can't really determine
            # the CPU utilization within the last X seconds...because to THIS
            # host it's new (only in the cur_metric).  So we error out, the
            # inspector will use a debug message in the log.
            LOG.warning(_LW("Unable to derive CPU Utilization for VM %s. "
                            "It is either a new VM or was recently migrated. "
                            "It will be collected in the next inspection "
                            "cycle."), instance.name)
            message = (_("Unable to derive CPU Utilization for VM %s.") %
                       instance.name)
            raise virt_inspector.InstanceNotFoundException(message)

        # Gather the previous metrics
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

        # If the entitled is zero, that generally means that the VM has not
        # been started yet (everything else would be zero as well).  So to
        # avoid a divide by zero error, just return 0% in that case.
        if entitled == 0:
            return virt_inspector.CPUUtilStats(util=0.0)

        util = float(util_cap + util_uncap - idle - donated) / float(entitled)

        # Utilization is reported as percents.  Therefore, multiply by 100.0
        # to get a readable percentage based format.
        return virt_inspector.CPUUtilStats(util=util * 100.0)

    @staticmethod
    def mac_for_metric_cna(metric_cna, client_cnas):
        """Finds the mac address for a given metric.

        :param metric_cna: The metric for a given client network adapter (CNA)
        :param client_cnas: The list of wrappers from pypowervm for the CNAs
                            attached to a given instance.
        :return: Mac address of the adapter.  If unable to be found, then None
                 is returned.
        """
        # TODO(thorst) Investigate optimization in pypowervm for this.
        for client_cna in client_cnas:
            if client_cna.loc_code == metric_cna.physical_location:
                # Found the appropriate mac.  The PowerVM format is upper
                # cased without colons.  Convert it.
                mac = client_cna.mac.lower()
                return ':'.join(mac[i:i + 2]
                                for i in range(0, len(mac), 2))
        return None

    def _get_cnas(self, lpar_uuid):
        """Returns the client VM's Network Adapters.

        :param lpar_uuid: The UUID of the VM.
        :return: A list of pypowervm CNA wrappers.
        """
        client_cna_resp = self.adpt.read(
            pvm_lpar.LPAR.schema_type, root_id=lpar_uuid,
            child_type=pvm_net.CNA.schema_type)
        return pvm_net.CNA.wrap(client_cna_resp)

    def inspect_vnics(self, instance):
        """Inspect the vNIC statistics for an instance.

        :param instance: the target instance
        :return: for each vNIC, the number of bytes & packets
                 received and transmitted
        """
        # Get the current and previous sample.  Delta is performed between
        # these two.
        uuid = self._puuid(instance)
        cur_date, cur_metric = self.vm_metrics.get_latest_metric(uuid)

        # If the cur_metric is none, then the instance can not be found in the
        # sample and an error should be raised.
        if cur_metric is None:
            raise virt_inspector.InstanceNotFoundException(
                _('VM %s not found in PowerVM Metrics Sample.') %
                instance.name)

        # If there isn't network information, this is because the Virtual
        # I/O Metrics were turned off.  Have to pass through this method.
        if cur_metric.network is None:
            return

        # Get the network interfaces.  A 'cna' is a Client VM's Network Adapter
        client_cnas = self._get_cnas(uuid)

        for metric_cna in cur_metric.network.cnas:
            # Get the mac, but if it isn't found, then move to the next.  Might
            # have been removed since the last sample.
            mac = self.mac_for_metric_cna(metric_cna, client_cnas)
            if mac is None:
                continue

            # The name will be the location code.  MAC is identified from
            # above.  Others appear libvirt specific.
            interface = virt_inspector.Interface(
                name=metric_cna.physical_location,
                mac=mac, fref=None, parameters=None)

            stats = virt_inspector.InterfaceStats(
                rx_bytes=metric_cna.received_bytes,
                rx_packets=metric_cna.received_packets,
                tx_bytes=metric_cna.sent_bytes,
                tx_packets=metric_cna.sent_packets)

            # Yield the stats up to the invoker
            yield (interface, stats)

    def inspect_vnic_rates(self, instance, duration=None):
        """Inspect the vNIC rate statistics for an instance.

        :param instance: the target instance
        :param duration: the last 'n' seconds, over which the value should be
               inspected

               The PowerVM implementation does not make use of the duration
               field.
        :return: for each vNIC, the rate of bytes & packets
                 received and transmitted
        """
        # Get the current and previous sample.  Delta is performed between
        # these two.
        uuid = self._puuid(instance)
        cur_date, cur_metric = self.vm_metrics.get_latest_metric(uuid)
        prev_date, prev_metric = self.vm_metrics.get_previous_metric(uuid)

        # If the current is none, then the instance can not be found in the
        # sample and an error should be raised.
        if cur_metric is None:
            raise virt_inspector.InstanceNotFoundException(
                _('VM %s not found in PowerVM Metrics Sample.') %
                instance.name)

        # If there isn't network information, this is because the Virtual
        # I/O Metrics were turned off.  Have to pass through this method.
        if (cur_metric.network is None or prev_metric is None or
                prev_metric.network is None):
            return

        # Get the network interfaces.  A 'cna' is a Client VM's Network Adapter
        client_cnas = self._get_cnas(uuid)

        def find_prev_net(metric_cna):
            """Finds the metric vNIC from the previous sample's vNICs."""
            # If no previous, return None
            if prev_metric is None or prev_metric.network is None:
                return None

            for prev_cna in prev_metric.network.cnas:
                if prev_cna.physical_location == metric_cna.physical_location:
                    return prev_cna

            # Couldn't find a previous.  Maybe the interface was recently
            # added to the instance?  Return None
            return None

        # Need to determine the time delta between the samples.  This is
        # usually 30 seconds from the API, but the metrics will be specific.
        date_delta_num = float((cur_date - prev_date).seconds)

        for metric_cna in cur_metric.network.cnas:
            # Get the mac, but if it isn't found, then move to the next.  Might
            # have been removed since the last sample.
            mac = self.mac_for_metric_cna(metric_cna, client_cnas)
            if mac is None:
                continue

            # The name will be the location code.  MAC is identified from
            # above.  Others appear libvirt specific.
            interface = virt_inspector.Interface(
                name=metric_cna.physical_location,
                mac=mac, fref=None, parameters=None)

            # Note that here, the previous may be none.  That simply indicates
            # that the adapter was dynamically added to the VM before the
            # previous collection.  Not the migration scenario above.
            # In this case, we can default the base to 0.
            prev = find_prev_net(metric_cna)
            rx_bytes_diff = (metric_cna.received_bytes -
                             (0 if prev is None else prev.received_bytes))
            tx_bytes_diff = (metric_cna.sent_bytes -
                             (0 if prev is None else prev.sent_bytes))

            # Stats are the difference in the bytes, divided by the difference
            # in time between the two samples.
            rx_rate = float(rx_bytes_diff) / float(date_delta_num)
            tx_rate = float(tx_bytes_diff) / float(date_delta_num)
            stats = virt_inspector.InterfaceRateStats(rx_rate, tx_rate)

            # Yield the results back to the invoker.
            yield (interface, stats)

    def inspect_disks(self, instance):
        """Inspect the disk statistics for an instance.

        The response is a generator of the values.

        :param instance: the target instance
        :return disk: The Disk indicating the device for the storage device.
        :return stats: The DiskStats indicating the read/write data to the
                       device.
        """
        # Get the current and previous sample.  Delta is performed between
        # these two.
        uuid = self._puuid(instance)
        cur_date, cur_metric = self.vm_metrics.get_latest_metric(uuid)

        # If the cur_metric is none, then the instance can not be found in the
        # sample and an error should be raised.
        if cur_metric is None:
            raise virt_inspector.InstanceNotFoundException(
                _('VM %s not found in PowerVM Metrics Sample.') %
                instance.name)

        # If there isn't storage information, this is because the Virtual
        # I/O Metrics were turned off.  Have to pass through this method.
        if cur_metric.storage is None:
            LOG.debug("Current storage metric was unavailable from the API "
                      "instance %s." % instance.name)
            return

        # Bundle together the SCSI and virtual FC adapters
        adpts = cur_metric.storage.virt_adpts + cur_metric.storage.vfc_adpts

        # Loop through all the storage adapters
        for adpt in adpts:
            # PowerVM only shows the connection (SCSI or FC).  Name after
            # the connection name
            disk = virt_inspector.Disk(device=adpt.name)
            stats = virt_inspector.DiskStats(
                read_requests=adpt.num_reads, read_bytes=adpt.read_bytes,
                write_requests=adpt.num_writes, write_bytes=adpt.write_bytes,
                errors=0)
            yield (disk, stats)

    def inspect_disk_iops(self, instance):
        """Inspect the Disk Input/Output operations per second for an instance.

        The response is a generator of the values.

        :param instance: the target instance
        :return disk: The Disk indicating the device for the storage device.
        :return stats: The DiskIOPSStats indicating the I/O operations per
                       second for the device.
        """
        # Get the current and previous sample.  Delta is performed between
        # these two.
        uuid = self._puuid(instance)
        cur_date, cur_metric = self.vm_metrics.get_latest_metric(uuid)
        prev_date, prev_metric = self.vm_metrics.get_previous_metric(uuid)

        # If the cur_metric is none, then the instance can not be found in the
        # sample and an error should be raised.
        if cur_metric is None:
            raise virt_inspector.InstanceNotFoundException(
                _('VM %s not found in PowerVM Metrics Sample.') %
                instance.name)

        # If there isn't storage information, this may be because the Virtual
        # I/O Metrics were turned off.  If the previous metric is unavailable,
        # also have to pass through this method.
        if (cur_metric.storage is None or prev_metric is None or
                prev_metric.storage is None):
            LOG.debug("Current storage metric was unavailable from the API "
                      "instance %s." % instance.name)
            return

        # Need to determine the time delta between the samples.  This is
        # usually 30 seconds from the API, but the metrics will be specific.
        # However, if there is no previous sample, then we have to estimate.
        # Therefore, we estimate 15 seconds - half of the standard 30 seconds.
        date_delta = ((cur_date - prev_date) if prev_date is not None else
                      datetime.timedelta(seconds=15))

        # Bundle together the SCSI and virtual FC adapters
        cur_adpts = (cur_metric.storage.virt_adpts +
                     cur_metric.storage.vfc_adpts)
        prev_adpts = (prev_metric.storage.virt_adpts +
                      prev_metric.storage.vfc_adpts)

        def find_prev(cur_adpt):
            for prev_adpt in prev_adpts:
                if prev_adpt.name == cur_adpt.name:
                    return prev_adpt
            return None

        # Loop through all the storage adapters
        for cur_adpt in cur_adpts:
            # IOPs is the read/write counts of the current - prev divided by
            # second difference between the two, rounded to the integer.  :-)
            cur_ops = cur_adpt.num_reads + cur_adpt.num_writes

            # The previous adapter may be None.  This simply indicates that the
            # adapter was added between the previous sample and this one.  It
            # does not indicate a live migrate scenario like noted above, as
            # the VM itself hasn't moved.
            prev_adpt = find_prev(cur_adpt)
            prev_ops = ((prev_adpt.num_reads + prev_adpt.num_writes)
                        if prev_adpt else 0)
            iops = (cur_ops - prev_ops) // date_delta.seconds

            # PowerVM only shows the connection (SCSI or FC).  Name after
            # the connection name
            disk = virt_inspector.Disk(device=cur_adpt.name)
            stats = virt_inspector.DiskIOPSStats(iops_count=iops)
            yield (disk, stats)
