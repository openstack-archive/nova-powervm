..
      Copyright 2015, 2016 IBM
      All Rights Reserved.

      Licensed under the Apache License, Version 2.0 (the "License"); you may
      not use this file except in compliance with the License. You may obtain
      a copy of the License at

          http://www.apache.org/licenses/LICENSE-2.0

      Unless required by applicable law or agreed to in writing, software
      distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
      WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
      License for the specific language governing permissions and limitations
      under the License.

Usage
=====

To make use of the PowerVM drivers, a PowerVM system set up with `NovaLink`_ is
required.  The nova-powervm driver should be installed on the management VM.

.. _NovaLink: http://www-01.ibm.com/common/ssi/cgi-bin/ssialias?infotype=AN&subtype=CA&htmlfid=897/ENUS215-262&appname=USN

The NovaLink architecture is such that the compute driver runs directly on the
PowerVM system.  No external management element (e.g. Hardware Management
Console or PowerVC) is needed.  Management of the virtualization is driven
through a thin virtual machine running on the PowerVM system.

Configuration of the PowerVM system and NovaLink is required ahead of time.  If
the operator is using volumes or Shared Storage Pools, they are required to be
configured ahead of time.


Configuration File Options
--------------------------
The standard nova configuration options are supported.  Additionally, a
``[powervm]`` section is used to provide additional customization to the driver.

By default, no additional inputs are needed.  The base configuration allows for
a Nova driver to support ephemeral disks to a local volume group (only
one can be on the system in the default config).  Connecting Fibre Channel
hosted disks via Cinder will use the Virtual SCSI connections through the
Virtual I/O Servers.

Operators may change the disk driver (nova based disks - NOT Cinder) via the
``disk_driver`` property.

All of these values are under the ``[powervm]`` section.  The tables are broken
out into logical sections.

To generate a sample config file for ``[powervm]`` run::

  oslo-config-generator --namespace nova_powervm > nova_powervm_sample.conf

The ``[powervm]`` section of the sample can then be edited and pasted into the
full nova.conf file.

VM Processor Options
~~~~~~~~~~~~~~~~~~~~
+--------------------------------------+------------------------------------------------------------+
| Configuration option = Default Value | Description                                                |
+======================================+============================================================+
| proc_units_factor = 0.1              | (FloatOpt) Factor used to calculate the processor units    |
|                                      | per vcpu.  Valid values are: 0.05 - 1.0                    |
+--------------------------------------+------------------------------------------------------------+
| uncapped_proc_weight = 64            | (IntOpt) The processor weight to assign to newly created   |
|                                      | VMs. Value should be between 1 and 255.  Represents the    |
|                                      | relative share of the uncapped processor cycles the        |
|                                      | Virtual Machine will receive when unused processor cycles  |
|                                      | are available.                                             |
+--------------------------------------+------------------------------------------------------------+


Disk Options
~~~~~~~~~~~~
+--------------------------------------+------------------------------------------------------------+
| Configuration option = Default Value | Description                                                |
+======================================+============================================================+
| disk_driver = localdisk              | (StrOpt) The disk driver to use for PowerVM disks.  Valid  |
|                                      | options are: localdisk, ssp                                |
|                                      |                                                            |
|                                      | If localdisk is specified and only one non-rootvg Volume   |
|                                      | Group exists on one of the Virtual I/O Servers, then no    |
|                                      | further config is needed.  If multiple volume groups exist,|
|                                      | then further specification can be done via the             |
|                                      | volume_group_* options.                                    |
|                                      |                                                            |
|                                      | Live migration is not supported with a localdisk config.   |
|                                      |                                                            |
|                                      | If ssp is specified, then a Shared Storage Pool will be    |
|                                      | used.  If only one SSP exists on the system, no further    |
|                                      | configuration is needed.  If multiple SSPs exist, then the |
|                                      | cluster_name property must be specified.  Live migration   |
|                                      | can be done within a SSP cluster.                          |
+--------------------------------------+------------------------------------------------------------+
| cluster_name = None                  | (StrOpt) Cluster hosting the Shared Storage Pool to use    |
|                                      | for storage operations.  If none specified, the host is    |
|                                      | queried; if a single Cluster is found, it is used.  Not    |
|                                      | used unless disk_driver option is set to ssp.              |
+--------------------------------------+------------------------------------------------------------+
| volume_group_name = None             | (StrOpt) Volume Group to use for block device operations.  |
|                                      | Must not be rootvg.  If disk_driver is localdisk, and more |
|                                      | than one non-rootvg volume group exists across the         |
|                                      | Virtual I/O Servers, then this attribute must be specified.|
+--------------------------------------+------------------------------------------------------------+
| volume_group_vios_name = None        | (StrOpt) (Optional) The name of the Virtual I/O Server     |
|                                      | hosting the volume group.  If this is not specified, the   |
|                                      | system will query through the Virtual I/O Servers looking  |
|                                      | for one that matches the volume_group_vios_name.  This is  |
|                                      | only needed if the system has multiple Virtual I/O Servers |
|                                      | with a non-rootvg volume group whose name is duplicated.   |
|                                      |                                                            |
|                                      | Typically paired with the volume_group_name attribute.     |
+--------------------------------------+------------------------------------------------------------+


Volume Options
~~~~~~~~~~~~~~
+--------------------------------------+------------------------------------------------------------+
| Configuration option = Default Value | Description                                                |
+======================================+============================================================+
| fc_attach_strategy = vscsi           | (StrOpt) The Fibre Channel Volume Strategy defines how FC  |
|                                      | Cinder volumes should be attached to the Virtual Machine.  |
|                                      | The options are: npiv or vscsi.                            |
|                                      |                                                            |
|                                      | It should be noted that if NPIV is chosen, the WWPNs will  |
|                                      | not be active on the backing fabric during the deploy.     |
|                                      | Some Cinder drivers will operate without issue.  Others    |
|                                      | may query the fabric and thus will fail attachment. It is  |
|                                      | advised that if an issue occurs using NPIV, the operator   |
|                                      | fall back to vscsi based deploys.                          |
+--------------------------------------+------------------------------------------------------------+
| vscsi_vios_connections_required = 1  | (IntOpt) Indicates a minimum number of Virtual I/O Servers |
|                                      | that are required to support a Cinder volume attach with   |
|                                      | the vSCSI volume connector.                                |
+--------------------------------------+------------------------------------------------------------+
| ports_per_fabric = 1                 | (IntOpt) (NPIV only) The number of physical ports that     |
|                                      | should be connected directly to the Virtual Machine, per   |
|                                      | fabric.                                                    |
|                                      |                                                            |
|                                      | Example: 2 fabrics and ports_per_fabric set to 2 will      |
|                                      | result in 4 NPIV ports being created, two per fabric.  If  |
|                                      | multiple Virtual I/O Servers are available, will attempt   |
|                                      | to span ports across I/O Servers.                          |
+--------------------------------------+------------------------------------------------------------+
| fabrics = A                          | (StrOpt) (NPIV only) Unique identifier for each physical   |
|                                      | FC fabric that is available.  This is a comma separated    |
|                                      | list.  If there are two fabrics for multi-pathing, then    |
|                                      | this could be set to A,B.                                  |
|                                      |                                                            |
|                                      | The fabric identifiers are used for the                    |
|                                      | 'fabric_<identifier>_port_wwpns' key.                      |
+--------------------------------------+------------------------------------------------------------+
| fabric_<name>_port_wwpns             | (StrOpt) (NPIV only) A comma delimited list of all the     |
|                                      | physical FC port WWPNs that support the specified fabric.  |
|                                      | Is tied to the NPIV 'fabrics' key.                         |
+--------------------------------------+------------------------------------------------------------+


Config Drive Options
~~~~~~~~~~~~~~~~~~~~
+--------------------------------------+------------------------------------------------------------+
| Configuration option = Default Value | Description                                                |
+======================================+============================================================+
| vopt_media_volume_group = root_vg    | (StrOpt) The volume group on the system that should be     |
|                                      | used to store the config drive metadata that will be       |
|                                      | attached to the VMs.                                       |
+--------------------------------------+------------------------------------------------------------+
| vopt_media_rep_size = 1              | (IntOpt) The size of the media repository (in GB) for the  |
|                                      | metadata for config drive.  Only used if the media         |
|                                      | repository needs to be created.                            |
+--------------------------------------+------------------------------------------------------------+
| image_meta_local_path = /tmp/cfgdrv/ | (StrOpt) The location where the config drive ISO files     |
|                                      | should be built.                                           |
+--------------------------------------+------------------------------------------------------------+
