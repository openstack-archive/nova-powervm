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

from oslo.config import cfg

hmc_opts = [
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
    # TODO(kyleh) Temporary - Only needed since we're using an HMC
    cfg.StrOpt('hmc_host_id',
               default='',
               help='TEMP - the unique id of the host to manage'),
    cfg.StrOpt('hmc_ip',
               default='',
               help='TEMP - the HMC IP.'),
    cfg.StrOpt('hmc_user',
               default='',
               help='TEMP - the HMC user.'),
    cfg.StrOpt('hmc_pass',
               default='',
               help='TEMP - the HMC password.')
]


CONF = cfg.CONF
CONF.register_opts(hmc_opts)
CONF.import_opt('host', 'nova.netconf')
