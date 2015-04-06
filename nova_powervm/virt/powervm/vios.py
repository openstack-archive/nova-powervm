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

from oslo_config import cfg
from oslo_log import log as logging

from pypowervm.wrappers import managed_system as pvm_ms
from pypowervm.wrappers import virtual_io_server as pvm_vios


LOG = logging.getLogger(__name__)
CONF = cfg.CONF


def get_vios_name_map(adapter, host_uuid):
    """Returns the map of VIOS names to UUIDs.

    :param adapter: The pypowervm adapter for the query.
    :param host_uuid: The host servers UUID.
    :return: A dictionary with all of the Virtual I/O Servers on the system.
             The format is:
             {
                 'vio_name': 'vio_uuid',
                 'vio2_name': 'vio2_uuid',
                 etc...
             }
    """
    vio_feed_resp = adapter.read(pvm_ms.System.schema_type, root_id=host_uuid,
                                 child_type=pvm_vios.VIOS.schema_type)
    wrappers = pvm_vios.VIOS.wrap(vio_feed_resp)
    return {wrapper.name: wrapper.uuid for wrapper in wrappers}


def get_physical_wwpns(adapter, ms_uuid):
    """Returns the WWPNs of the FC adapters across all VIOSes on system."""
    resp = adapter.read(pvm_ms.System.schema_type, root_id=ms_uuid,
                        child_type=pvm_vios.VIOS.schema_type)
    vios_feed = pvm_vios.VIOS.wrap(resp)
    wwpn_list = []
    for vios in vios_feed:
        wwpn_list.extend(vios.get_pfc_wwpns())
    return wwpn_list
