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

# Defines the various volume connectors that can be used.
from oslo_config import cfg
CONF = cfg.CONF


vol_adapter_opts = [
    cfg.StrOpt('fc_attach_strategy',
               default='vscsi',
               help='The Fibre Channel Volume Strategy defines how FC Cinder '
                    'volumes should be attached to the Virtual Machine.  The '
                    'options are: npiv or vscsi.'),
    cfg.StrOpt('fc_npiv_adapter_api',
               default='nova_powervm.virt.powervm.volume.npiv.'
               'NPIVVolumeAdapter',
               help='Volume Adapter API to connect FC volumes using NPIV'
                    'connection mechanism'),
    cfg.StrOpt('fc_vscsi_adapter_api',
               default='nova_powervm.virt.powervm.volume.vscsi.'
               'VscsiVolumeAdapter',
               help='Volume Adapter API to connect FC volumes through Virtual '
                    'I/O Server using PowerVM vSCSI connection mechanism'),
    cfg.IntOpt('vscsi_vios_connections_required', default=1,
               help='Indicates a minimum number of Virtual I/O Servers that '
                    'are required to support a Cinder volume attach with the '
                    'vSCSI volume connector.')
]
CONF.register_opts(vol_adapter_opts, group='powervm')

FC_STRATEGY_MAPPING = {
    'npiv': CONF.powervm.fc_npiv_adapter_api,
    'vscsi': CONF.powervm.fc_vscsi_adapter_api
}
