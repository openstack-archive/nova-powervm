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

    def _get_host_uuid(self, adpt):
        """Returns the Host systems UUID for pypowervm.

        The pypowervm API needs a UUID of the server that it is managing.  This
        method returns the UUID of that server.
        :param adpt: The pypowervm adapter.
        :return: The UUID of the host system.
        """
        hosts = pvm_ms.System.wrap(adpt.read(pvm_ms.System.schema_type))
        if len(hosts) != 1:
            raise Exception(_("Expected exactly one host; found %d"),
                            len(hosts))
        LOG.debug("Host UUID: %s" % hosts[0].uuid)
        return hosts[0].uuid
