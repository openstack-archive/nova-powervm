===================
PowerVM Nova Driver
===================

The IBM PowerVM hypervisor provides virtualization on POWER hardware.  PowerVM
admins can see benefits in their environments by making use of OpenStack.
This driver (along with a Neutron ML2 compatible agent and Ceilometer agent)
provides the capability for operators of PowerVM to use OpenStack natively.


Problem Description
===================

As ecosystems continue to evolve around the POWER platform, a single OpenStack
driver does not meet all of the needs for the various hypervisors.  The
standard libvirt driver provides support for KVM on POWER systems.  This nova
driver provides PowerVM support to OpenStack environment.

This driver meets the following:

* Built within the community

* Fits the OpenStack model

* Utilizes automated functional and unit tests

* Enables use of PowerVM systems through the OpenStack APIs

* Allows attachment of volumes from Cinder over supported protocols


This driver makes the following use cases available for PowerVM:

* As a deployer, all of the standard lifecycle operations (start, stop,
  reboot, migrate, destroy, etc.) should be supported on a PowerVM based
  instance.

* As a deployer, I should be able to capture an instance to an image.

* VNC console to instances deployed.


Usage
=====

To use the driver, install the nova-powervm project on your NovaLink-based
PowerVM system.  The nova-powervm project has a minimal set of configuration.
See the configuration options section of the dev-ref for more information.

It is recommended that operators also make use of the networking-powervm
project.  The project ensures that the network bridge supports the VLAN-based
networks required for the workloads.

There is also a ceilometer-powervm project that can be included.

Future work will be done to include PowerVM into the various OpenStack
deployment models.


Overview of Architecture
========================

The driver enables the following:

* Provide deployments that work with the OpenStack model.

* Driver is implemented using a new version of the PowerVM REST API.

* Ephemeral disks are supported either with Virtual I/O Server (VIOS)
  hosted local disks or via Shared Storage Pools (a PowerVM cluster file
  system).

* Volume support is provided via Cinder through supported protocols for the
  Hypervisor (virtual SCSI and N-Port ID Virtualization).

* Live migration support is available when using Shared Storage Pools or boot
  from volume.

* Network integration is supported via the ML2 compatible Neutron Agent.  This
  is the openstack/networking-powervm project.

* Automated Functional Testing is provided to validate changes from the broader
  OpenStack community against the PowerVM driver.

* Thorough unit, syntax, and style testing is provided and enforced for the
  driver.

The intention is that this driver follows the OpenStack Nova model and will
be a candidate for promotion (via a subsequent blueprint) into the nova core
project.


Data Model Impact
-----------------

* The evacuate API is supported as part of the PowerVM driver.  It optionally
  allows for the NVRAM data to be stored to a Swift database.  However this
  does not impact the data model itself.  It simply provides a location to
  optionally store the VM's NVRAM metadata in the event of a rebuild,
  evacuate, shelve, migration or resize.


REST API Impact
---------------

No REST API impacts.


Security Impact
---------------

No known security impacts.


Notifications Impact
--------------------

No new notifications.  The driver does expect that the Neutron agent will
return an event when the VIF plug has occurred, assuming that Neutron is
the network service.


Other End User Impact
---------------------

The administrator may notice new logging messages in the nova compute logs.


Performance Impact
------------------

The driver has a similar deployment speed and agility to other hypervisors.
It has been tested with up to 10 concurrent deploys with several hundred VMs
on a given server.

Most operations are comparable in speed.  Deployment, attach/detach volumes,
lifecycle, etc... are quick.

Due to the nature of the project, any performance impacts are limited to the
Compute Driver.  The API processes for instance are not impacted.


Other Deployer Impact
---------------------

The cloud administrator will need to refer to documentation on how to
configure OpenStack for use with a PowerVM hypervisor.

A 'powervm' configuration group is used to contain all the PowerVM specific
configuration settings. Existing configuration file attributes will be
reused as much as possible (e.g. vif_plugging_timeout). This reduces the number
of PowerVM specific items that will be needed.

It is the goal of the project to only require minimal additional attributes.
The deployer may specify additional attributes to fit their configuration.


Live Migration
--------------

In the Mitaka release, the Nova project moved to using conductor-based objects
for the live migration flow.  These objects exist in
nova/objects/migrate_data.py.

While the PowerVM driver supports live migration, it can not register its own
live migration object due to being out of tree.  The team is working with the
nova core team to bring the PowerVM driver in tree.  However until that time,
the use of live migration with the PowerVM driver requires starting a
PowerVM-specific conductor.

This conductor does not limit the OpenStack cloud to only supporting PowerVM.
Rather, it simply allows an existing cloud to include PowerVM support within
it.

To use the conductor, install the nova-powervm project on the node running the
nova conductor.  Then start the 'nova-conductor-powervm' process.  This will
support ALL of the hypervisors, including PowerVM.

To reiterate, this is only needed if you plan to use PowerVM's live migrate
functionality.


Developer Impact
----------------

The code for this driver is currently contained within a powervm project.
The driver is within the /nova_powervm/virt/powervm/ package and extends the
nova.virt.driver.ComputeDriver class.

The code interacts with PowerVM through the pypowervm library.  This python
binding is a wrapper to the PowerVM REST API.  All hypervisor operations
interact with the PowerVM REST API via this binding.  The driver is
maintained to support future revisions of the PowerVM REST API as needed.

For ephemeral disk support, either a Virtual I/O Server hosted local disk or a
Shared Storage Pool (a PowerVM clustered file system) is supported.  For
volume attachments, the driver supports Cinder-based attachments via
protocols supported by the hypervisor (e.g. Fibre Channel).

For networking, the networking-powervm project provides a Neutron ML2 Agent.
The agent provides the necessary configuration on the Virtual I/O Server for
networking.  The PowerVM Nova driver code creates the VIF for the client VM,
but the Neutron agent creates the VIF for VLANs.

Automated functional testing is provided through a third party continuous
integration system.  It monitors for incoming Nova change sets, runs a set
of functional tests (lifecycle operations) against the incoming change, and
provides a non-gating vote (+1 or -1).

Developers should not be impacted by these changes unless they wish to try the
driver.


Community Impact
----------------

The intent of this project is to bring another driver to OpenStack that
aligns with the ideals and vision of the community.  The intention is to
promote this to core Nova.


Alternatives
------------

No alternatives appear viable to bring PowerVM support into the OpenStack
community.


Implementation
==============

Assignee(s)
-----------

Primary assignees:
   adreznec
   efried
   kyleh
   thorst

Other contributors:
   multiple


Dependencies
============

* Utilizes the PowerVM REST API specification for management.  Will
  utilize future versions of this specification as it becomes available:
  http://ibm.co/1lThV9R

* Builds on top of the `pypowervm library`_.  This is a prerequisite to
  utilizing the driver.

.. _pypowervm library: https://github.com/powervm/pypowervm

Testing
=======

Tempest Tests
-------------

Since the tempest tests should be implementation agnostic, the existing
tempest tests should be able to run against the PowerVM driver without issue.

Tempest tests that require function that the platform does not yet support
(e.g. iSCSI or Floating IPs) will not pass.  These should be ommitted from
the Tempest test suite.

A `sample Tempest test configuration` for the PowerVM driver has been provided.

Thorough unit tests exist within the project to validate specific functions
within this implementation.

.. _sample Tempest test configuration: https://github.com/powervm/powervm-ci/tree/master/tempest


Functional Tests
----------------

A third party functional test environment has been created.  It monitors
for incoming nova change sets.  Once it detects a new change set, it will
execute the existing lifecycle API tests.  A non-gating vote (+1 or -1) will
be provided with information provided (logs) based on the result.


API Tests
---------

Existing APIs should be valid.  All testing is planned within the functional
testing system and via unit tests.


Documentation Impact
====================

User Documentation
------------------

See the dev-ref for documentation on how to configure, contribute, use, etc.
this driver implementation.


Developer Documentation
-----------------------

The existing Nova developer documentation should typically suffice.  However,
until merge into Nova, we will maintain a subset of dev-ref documentation.


References
==========

* PowerVM REST API Specification (may require newer versions as they
  become available): http://ibm.co/1lThV9R

* PowerVM Virtualization Introduction and Configuration:
  http://www.redbooks.ibm.com/abstracts/sg247940.html

* PowerVM Best Practices: http://www.redbooks.ibm.com/abstracts/sg248062.html
