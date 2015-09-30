..
      Copyright 2015 IBM
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

Source Code Structure
=====================

Since nova-powervm strives to be integrated into the upstream Nova project,
the source code structure matches a standard driver.

::

  nova_powervm/
    virt/
      powervm/
        disk/
        tasks/
        volume/
        ...
    tests/
      virt/
        powervm/
          disk/
          tasks/
          volume/
          ...

nova_powervm/virt/powervm
~~~~~~~~~~~~~~~~~~~~~~~~~

The main directory for the overall driver.  Provides the driver
implementation, image support, and some high level classes to interact with
the PowerVM system (ex. host, vios, vm, etc...)

The driver attempts to utilize `TaskFlow`_ for major actions such as spawn.
This allows the driver to create atomic elements (within the tasks) to
drive operations against the system (with revert capabilities).

.. _TaskFlow: https://wiki.openstack.org/wiki/TaskFlow

nova_powervm/virt/powervm/disk
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The disk folder contains the various 'nova ephemeral' disk implementations.
These are basic images that do not involve Cinder.

Two disk implementations exist currently.

* localdisk - supports Virtual I/O Server Volume Groups.  This configuration
  uses any Volume Group on the system, allowing operators to make use of the
  physical disks local to their system.

* Shared Storage Pool - utilizes PowerVM's distributed storage.  As such this
  implementation allows operators to make use of live migration capabilities.

The standard interface between these two implementations is defined in the
driver.py.  This ensures that the nova-powervm compute driver does not need
to know the specifics about which disk implementation it is using.

nova_powervm/virt/powervm/tasks
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The task folder contains `TaskFlow`_ classes.  These implementations simply
wrap around other methods, providing logical units that the compute
driver can use when building a string of actions.

For instance, spawning an instance may require several atomic tasks:
 - Create VM
 - Plug Networking
 - Create Disk from Glance
 - Attach Disk to VM
 - Power On

The tasks in this directory encapsulate this.  If anything fails, they have
corresponding reverts.  The logic to perform these operations is contained
elsewhere; these are simple wrappers that enable embedding into Taskflow.

.. _TaskFlow: https://wiki.openstack.org/wiki/TaskFlow

nova_powervm/virt/powervm/volume
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The volume folder contains the Cinder volume connectors.  A volume connector
is the code that connects a Cinder volume (which is visible to the host) to
the Virtual Machine.

The PowerVM Compute Driver has an interface for the volume connectors defined
in this folder's `driver.py`.

The PowerVM Compute Driver provides two implementations for Fibre Channel
attached disks.

  * Virtual SCSI (vSCSI): The disk is presented to a Virtual I/O Server and
    the data is passed through to the VM through a virtualized SCSI
    connection.

  * N-Port ID Virtualization (NPIV): The disk is presented directly to the
    VM. The VM will have virtual Fibre Channel connections to the disk, and
    the Virtual I/O Server will not have the disk visible to it.
