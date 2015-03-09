# Copyright 2014, 2015 IBM Corp.
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

pvm_opts = [
    cfg.FloatOpt('proc_units_factor',
                 default=0.1,
                 help='Factor used to calculate the processor units per vcpu.'
                 ' Valid values are: 0.05 - 1.0'),
    cfg.IntOpt('uncapped_proc_weight',
               default=64,
               help='The processor weight to assign to newly created VMs.  '
                    'Value should be between 1 and 255.  Represents how '
                    'aggressively LPARs grab CPU when unused cycles are '
                    'available.'),
    cfg.StrOpt('vopt_media_volume_group',
               default='rootvg',
               help='The volume group on the system that should be used '
                    'for the config drive metadata that will be attached '
                    'to VMs.'),
    cfg.IntOpt('vopt_media_rep_size',
               default=1,
               help='The size of the media repository in GB for the metadata '
                    'for config drive.'),
    cfg.StrOpt('image_meta_local_path',
               default='/tmp/cfgdrv/',
               help='The location where the config drive ISO files should be '
                    'built.'),
    # TODO(kyleh) Re-evaluate these as the auth model evolves.
    cfg.StrOpt('pvm_host_mtms',
               default='',
               help='The Model Type/Serial Number of the host server to '
                    'manage.  Format is MODEL-TYPE*SERIALNUM.  Example is '
                    '8286-42A*1234ABC.'),
    cfg.StrOpt('pvm_server_ip',
               default='localhost',
               help='The IP Address hosting the PowerVM REST API'),
    cfg.StrOpt('pvm_user_id',
               default='',
               help='The user id for authentication into the API.'),
    cfg.StrOpt('pvm_pass',
               default='',
               help='The password for authentication into the API.'),
    cfg.StrOpt('fc_attach_strategy',
               default='vscsi',
               help='The Fibre Channel Volume Strategy defines how FC Cinder '
                    'volumes should be attached to the Virtual Machine.  The '
                    'options are: npiv or vscsi.')
]


CONF = cfg.CONF
CONF.register_opts(pvm_opts)
CONF.import_opt('host', 'nova.netconf')
