# Copyright 2016, 2017 IBM Corp.
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

CONF = cfg.CONF

powervm_group = cfg.OptGroup(
    'powervm',
    title='PowerVM Options')


powervm_opts = [
    cfg.IntOpt('uncapped_proc_weight',
               default=64, min=1, max=255,
               help='The processor weight to assign to newly created VMs.  '
                    'Value should be between 1 and 255.  Represents how '
                    'aggressively LPARs grab CPU when unused cycles are '
                    'available.'),
    cfg.StrOpt('vopt_media_volume_group',
               default='rootvg',
               help='The volume group on the system that should be used '
                    'to store the config drive metadata that will be attached '
                    'to VMs.  If not specified and no media repository '
                    'exists, rootvg will be used.  This option is ignored if '
                    'a media repository already exists.'),
    cfg.IntOpt('vopt_media_rep_size',
               default=1, min=1,
               help='The size of the media repository (in GB) for the '
                    'metadata for config drive.  Only used if the media '
                    'repository needs to be created.'),
    cfg.StrOpt('image_meta_local_path',
               default='/tmp/cfgdrv/',
               help='The location where the config drive ISO files should be '
                    'built.'),
    cfg.StrOpt('pvm_vswitch_for_novalink_io',
               default='NovaLinkVEABridge',
               help="Name of the PowerVM virtual switch to be used when "
                    "mapping Linux based network ports to PowerVM virtual "
                    "Ethernet devices"),
    cfg.BoolOpt('remove_vopt_media_on_boot',
                default=False,
                help="If enabled, tells the PowerVM driver to trigger the "
                     "removal of the media from the virtual optical device "
                     "used for initialization of VMs on spawn after "
                     "'remove_vopt_media_time' minutes."),
    cfg.IntOpt('remove_vopt_media_time',
               default=60, min=0,
               help="The amount of time in minutes after a VM has been "
                    "created for the virtual optical media to be removed."),
    cfg.BoolOpt('use_rmc_mgmt_vif',
                default=True,
                help="If enabled, tells the PowerVM Driver to create an RMC "
                     "network interface on the deploy of a VM.  This is an "
                     "adapter that can only talk to the NovaLink partition "
                     "and enables DLPAR actions."),
    cfg.BoolOpt('use_rmc_ipv6_scheme',
                default=True,
                help="Only used if use_rmc_mgmt_vif is True and config drive "
                     "is being used.  If set, the system will configure the "
                     "RMC network interface with an IPv6 link local address. "
                     "This is generally set to True, but users may wish to "
                     "turn this off if their operating system has "
                     "compatibility issues."),
    cfg.IntOpt('vios_active_wait_timeout',
               default=300,
               help="Default time in seconds to wait for Virtual I/O Server "
                    "to be up and running.")
]

ssp_opts = [
    cfg.StrOpt('cluster_name',
               default='',
               help='Cluster hosting the Shared Storage Pool to use for '
                    'storage operations.  If none specified, the host is '
                    'queried; if a single Cluster is found, it is used. '
                    'Not used unless disk_driver option is set to ssp.')
]

vol_adapter_opts = [
    cfg.StrOpt('fc_attach_strategy',
               choices=['vscsi', 'npiv'], ignore_case=True,
               default='vscsi', mutable=True,
               help='The Fibre Channel Volume Strategy defines how FC Cinder '
                    'volumes should be attached to the Virtual Machine.  The '
                    'options are: npiv or vscsi. If npiv is selected then '
                    'the ports_per_fabric and fabrics option should be '
                    'specified and at least one fabric_X_port_wwpns option '
                    '(where X corresponds to the fabric name) must be '
                    'specified.'),
    cfg.StrOpt('fc_npiv_adapter_api',
               default='nova_powervm.virt.powervm.volume.npiv.'
               'NPIVVolumeAdapter',
               help='Volume Adapter API to connect FC volumes using NPIV'
                    'connection mechanism.'),
    cfg.StrOpt('fc_vscsi_adapter_api',
               default='nova_powervm.virt.powervm.volume.vscsi.'
               'PVVscsiFCVolumeAdapter',
               help='Volume Adapter API to connect FC volumes through Virtual '
                    'I/O Server using PowerVM vSCSI connection mechanism.'),
    cfg.IntOpt('vscsi_vios_connections_required',
               default=1, min=1,
               help='Indicates a minimum number of Virtual I/O Servers that '
                    'are required to support a Cinder volume attach with the '
                    'vSCSI volume connector.'),
    cfg.BoolOpt('volume_use_multipath',
                default=False,
                help="Use multipath connections when attaching iSCSI or FC"),
    cfg.StrOpt('iscsi_iface',
               default='default',
               help="The iSCSI transport iface to use to connect to target in "
                    "case offload support is desired. Do not confuse the "
                    "iscsi_iface parameter to be provided here with the "
                    "actual transport name."),
    cfg.StrOpt('rbd_user',
               default='',
               help="Refer to this user when connecting and authenticating "
                    "with the Ceph RBD server.")
]

# NPIV Options.  Only applicable if the 'fc_attach_strategy' is set to 'npiv'.
# Otherwise this section can be ignored.
npiv_opts = [
    cfg.IntOpt('ports_per_fabric',
               default=1, min=1,
               help='The number of physical ports that should be connected '
                    'directly to the Virtual Machine, per fabric.  '
                    'Example: 2 fabrics and ports_per_fabric set to 2 will '
                    'result in 4 NPIV ports being created, two per fabric. '
                    'If multiple Virtual I/O Servers are available, will '
                    'attempt to span ports across I/O Servers.'),
    cfg.StrOpt('fabrics', default='A',
               help='Unique identifier for each physical FC fabric that is '
                    'available.  This is a comma separated list.  If there '
                    'are two fabrics for multi-pathing, then this could be '
                    'set to A,B.'
                    'The fabric identifiers are used for the '
                    '\'fabric_<identifier>_port_wwpns\' key.')
]

remote_restart_opts = [
    cfg.StrOpt('nvram_store',
               choices=['none', 'swift'], ignore_case=True,
               default='none',
               help='The NVRAM store to use to hold the PowerVM NVRAM for '
               'virtual machines.'),
]

swift_opts = [
    cfg.StrOpt('swift_container', default='powervm_nvram',
               help='The Swift container to store the PowerVM NVRAM in. This '
               'must be configured the same value for all compute hosts.'),
    cfg.StrOpt('swift_username', default='powervm',
               help='The Swift user name to use for operations that use '
               'the Swift store.'),
    cfg.StrOpt('swift_user_domain_name', default='powervm',
               help='The Swift domain the user is a member of.'),
    cfg.StrOpt('swift_password', secret=True,
               help='The password for the Swift user.'),
    cfg.StrOpt('swift_project_name', default='powervm',
               help='The Swift project.'),
    cfg.StrOpt('swift_project_domain_name', default='powervm',
               help='The Swift project domain.'),
    cfg.StrOpt('swift_auth_version', default='3', help='The Keystone API '
               'version.'),
    cfg.StrOpt('swift_auth_url', help='The Keystone authorization url. '
               'Example: "http://keystone-hostname:5000/v3"'),
    cfg.StrOpt('swift_cacert', required=False, help='Path to CA certificate '
               'file.  Example: /etc/swiftclient/myca.pem'),
    cfg.StrOpt('swift_endpoint_type', help='The endpoint/interface type for '
               'the Swift client to select from the Keystone Service Catalog '
               'for the connection URL.  Swift defaults to "publicURL".')
]

vnc_opts = [
    cfg.BoolOpt('vnc_use_x509_auth', default=False,
                help='If enabled, uses X509 Authentication for the '
                'VNC sessions started for each VM.'),
    cfg.StrOpt('vnc_ca_certs', help='Path to CA certificate '
               'to use for verifying VNC X509 Authentication.'),
    cfg.StrOpt('vnc_server_cert', help='Path to Server certificate '
               'to use for verifying VNC X509 Authentication.'),
    cfg.StrOpt('vnc_server_key', help='Path to Server private key '
               'to use for verifying VNC X509 Authentication.')
]

STATIC_OPTIONS = (powervm_opts + ssp_opts + vol_adapter_opts + npiv_opts
                  + remote_restart_opts + swift_opts + vnc_opts)

# Dictionary where the key is the NPIV Fabric Name, and the value is a list of
# Physical WWPNs that match the key.
NPIV_FABRIC_WWPNS = {}
FABRIC_WWPN_HELP = ('A comma delimited list of all the physical FC port '
                    'WWPNs that support the specified fabric.  Is tied to '
                    'the NPIV fabrics key.')
# This is only used to provide a sample for the list_opt() method
fabric_sample = [
    cfg.StrOpt('fabric_A_port_wwpns', default='', help=FABRIC_WWPN_HELP),
    cfg.StrOpt('fabric_B_port_wwpns', default='', help=FABRIC_WWPN_HELP),
]


def _register_fabrics(conf, fabric_mapping):
    """Registers the fabrics to WWPNs options and builds a mapping.

    This method registers the 'fabric_X_port_wwpns' (where X is determined by
    the 'fabrics' option values) and then builds a dictionary that mapps the
    fabrics to the WWPNs.  This mapping can then be later used without having
    to reparse the options.
    """
    # At this point, the fabrics should be specified.  Iterate over those to
    # determine the port_wwpns per fabric.
    if conf.powervm.fabrics is not None:
        port_wwpn_keys = []
        fabrics = conf.powervm.fabrics.split(',')
        for fabric in fabrics:
            opt = cfg.StrOpt('fabric_%s_port_wwpns' % fabric,
                             default='', help=FABRIC_WWPN_HELP)
            port_wwpn_keys.append(opt)

        conf.register_opts(port_wwpn_keys, group='powervm')

        # Now that we've registered the fabrics, saturate the NPIV dictionary
        for fabric in fabrics:
            key = 'fabric_%s_port_wwpns' % fabric
            wwpns = conf.powervm[key].split(',')
            wwpns = [x.upper().strip(':') for x in wwpns]
            fabric_mapping[fabric] = wwpns


def register_opts(conf):
    conf.register_group(powervm_group)
    conf.register_opts(STATIC_OPTIONS, group=powervm_group)
    _register_fabrics(conf, NPIV_FABRIC_WWPNS)


# To generate a sample config run:
# $ oslo-config-generator --namespace nova_powervm > nova_powervm_sample.conf
def list_opts():
    # The nova conf tooling expects each module to return a dict of options.
    # When powervm is pulled into nova proper the return value would be in
    # this form:
    # return {powervm_group.name: STATIC_OPTIONS + fabric_sample}
    #
    # The oslo-config-generator tooling expects a tuple:
    return [(powervm_group.name, STATIC_OPTIONS + fabric_sample)]
