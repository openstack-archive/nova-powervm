===================
PowerVM Nova Driver
===================

Include the URL of your launchpad blueprint:

https://blueprints.launchpad.net/neutron/+spec/example

The IBM PowerVM hypervisor provides virtualization on POWER hardware.  PowerVM
admins can see benefits in their environments by making use of OpenStack.
This driver (along with a Neutron ML2 compatible agent and Ceilometer agent)
will provide capability for admins of PowerVM to use OpenStack natively.


Problem Description
===================

As ecosystems continue to evolve around the POWER platform, a single OpenStack
driver does not meet all of the needs for the varying hypervisors.  The work
done here is to build a PowerVM driver within the broader community.  This
will sit alongside the existing libvirt based driver utilized by PowerKVM
environments.

This new driver must meet the following:

* Built within the community

* Fits the OpenStack model

* Utilizes automated functional and unit tests

* Enables use of PowerVM systems through the OpenStack APIs

* Allows attachment of volumes from Cinder over supported protocols


The use cases should be the following:

* As a deployer, all of the standard lifecycle operations (start, stop,
  reboot, migrate, destroy, etc...) should be supported on a PowerVM based
  instance.

* As a deployer, I should be able to capture an instance to an image.


Proposed Change
===============

The changes proposed are the following:

* Create a PowerVM based driver in a StackForge project.  This will implement
  the nova/virt/driver Compute Driver.

* Provide deployments that work with the OpenStack model.

* Driver will be implemented using a new version of the PowerVM REST API.

* Ephemeral disks will be supported either with Virtual I/O Server (VIOS)
  hosted local disks or via Shared Storage Pools (a PowerVM cluster file
  system).

* Volume support will be via Cinder through supported protocols for the
  Hypervisor.

* Network integration will be supported via a ML2 compatible Neutron Agent.

* Automated Functional Testing will be provided to validate changes from the
  broader OpenStack community against the PowerVM driver.

* Thorough unit testing will be provided for the driver.

The changes proposed will bring support for the PowerVM hypervisor into the
OpenStack ecosystem, following the OpenStack development model.

This development will be done in StackForge in a project named ‘nova-powervm’.
The intent is that the completion of this work will provide the foundation to
bring the PowerVM Nova driver (with supporting components) into Nova Core via
a separate BluePrint in a future release of OpenStack.

Until a subsequent BluePrint is proposed and accepted, this driver is to be
considered experimental.


Data Model Impact
-----------------

No data model impacts are anticipated as part of this work.


REST API Impact
---------------

The intent of this work item is to enable PowerVM to fit within the broader
OpenStack ecosystem, without requiring changes to the REST API.

As such, no REST API impacts are anticipated.


Security Impact
---------------

New root wrap policies may need to be updated to support various commands for
the PowerVM REST API.

No other security impacts are foreseen.


Notifications Impact
--------------------

No new notifications are anticipated.


Other End User Impact
---------------------

The administrator will notice new logging messages in the nova compute logs.


Performance Impact
------------------

It is a goal of the driver to deploy systems with similar speed and agility
as the libvirt driver within OpenStack.

Since this process should match the OpenStack model, it is not planned to add
any new periodic tasks, database queries or other items.

Performance impacts should be limited to the Compute Driver, as the changes
should be consolidated within the driver on the endpoint.  The API processes
for instance should not be impacted.


Other Deployer Impact
---------------------

The cloud administrator will need to refer to documentation on how to
configure OpenStack for use with a PowerVM hypervisor.

A 'powervm' configuration group will be used to contain all the PowerVM
specific configuration settings. Existing configuration file attributes will be
reused as much as possible. This reduces the number of PowerVM specific items
that will be needed. However, the driver will require some PowerVM specific
options.

In this case, we plan to keep the PowerVM specifics contained within the
configuration file (and driver code).  These will be documented on the
driver's wiki page.

There should be no impact to customers upgrading their cloud stack as this is
a genesis driver and should not have database impacts.


Developer Impact
----------------

The code for this driver will be contained within a powervm StackForge
project.  The driver will be contained within /nova/virt/powervm/.  The driver
will extend nova.virt.driver.ComputeDriver.

The code will interact with PowerVM through the pypowervm library.  This python
binding is a wrapper to the PowerVM REST API.  All hypervisor operations will
interact with the PowerVM REST API via this binding.  The driver will be
maintained to support future revisions of the PowerVM REST API as needed.

For ephemeral disk support, either a Virtual I/O Server hosted local disk or a
Shared Storage Pool (a PowerVM clustered file system) will be supported.  For
volume attachments, the driver will support Cinder based attachments via
protocols supported by the hypervisor.

For networking, a blueprint is being proposed for the Neutron project that
will provide a Neutron ML2 Agent.  This project will be developed in
StackForge alongside nova-powervm.  The Agent will provide the necessary
configuration on the Virtual I/O Server.  The Nova driver code will have a
/nova/virt/powervm/vif.py file that will configure the network adapter on the
client VM.

Automated functional testing will be provided through a third party continuous
integration system.  It will monitor for incoming Nova change sets, run a set
of functional tests (lifecycle operations) against the incoming change, and
provide a non-gating vote (+1 or -1).

Developers should not be impacted by these changes unless they wish to try the
driver.

Until a subsequent blueprint is proposed and accepted, unless otherwise noted,
the driver will be considered experimental.


Community Impact
----------------

The intent of this blueprint is to bring another driver to OpenStack that
aligns with the ideals and vision of the community.

It will be discussed in the Nova IRC and mailing lists.


Alternatives
------------

No alternatives appear viable to bring PowerVM support into the OpenStack
community.


Implementation
==============

Assignee(s)
-----------

Primary assignee:
   kyleh

Other contributors:
   thorst
   dwarcher
   efried

Work Items
----------

* Create a base PowerVM driver that is non-functional, but defines the methods
  that need to be implemented.

* Implement the host statistics methods (get_host_stats, get_host_ip_addr,
  get_host_cpu_stats, get_host_uptime, etc.).

* Implement the spawn method.

* Implement the destroy method.

* Implement the instance information methods (list_instances, instance_exists,
  poll_rebooting_instances, etc.).

* Implement the live migration methods.  Note that, for ephemeral disks, this
  will be specific to Shared Storage Pool environments where the Virtual I/O
  Servers on the source and target systems share the same (clustered) file
  system.

* Implement support for Cinder volume operations.

* Implement an option to configure an internal management NIC - used for
  Resource Monitoring and Control (RMC) – as part of deploy.  This is a
  prerequisite for migration and resize.  This will be controlled as part of
  the CONF file.

* Implement the network interface methods (attach_interface and
  detach_interface).  Delegate the Virtual I/O Server work to the
  corresponding Neutron ML2 agent.

* Implement an automated functional test server that listens for incoming
  commits from the community and provides a non-gating vote (+1 or -1) on the
  change.


Dependencies
============

* Will utilize the PowerVM REST API specification for management.  Will
  utilize future versions of this specification as it becomes available:
  http://ibm.co/1lThV9R

* Will build on top of the pypowervm library.  This will be a prerequisite to
  utilizing the driver and identified in the requirements.txt file.


Testing
=======

Tempest Tests
-------------

Since the tempest tests should be implementation agnostic, the existing
tempest tests should be able to run against the PowerVM driver without issue.
This blueprint does not foresee any changes based off this driver.

Thorough unit tests will be created within the Nova project to validate
specific functions within this driver implementation.


Functional Tests
----------------

A third party functional test environment will be created.  It will monitor
for incoming nova change sets.  Once it detects a new change set, it will
execute the existing lifecycle API tests.  A non-gating vote (+1 or -1) will
be provided with information provided (logs) based on the result.


API Tests
---------

The REST APIs are not planned to change as part of this.  Existing APIs should
be valid.  All testing is planned within the functional testing system and via
unit tests.


Documentation Impact
====================

User Documentation
------------------

Documentation will be contributed which identifies how to configure the
driver.  This will include configuring the dependencies specified above.

Documentation will be done on wiki, specifically at a minimum to the following
page: http://docs.openstack.org/trunk/config-reference/content/section_compute-hypervisors.html

Interlock is planned to be done with the OpenStack documentation team.


Developer Documentation
-----------------------

No developer documentation additions are anticipated.  If the existing
developer documentation is updated to reflect more hypervisor specific items,
this driver will follow suit.


References
==========

* PowerVM REST API Specification (may require newer versions as they
  become available): http://ibm.co/1lThV9R

* PowerVM Virtualization Introduction and Configuration:
  http://www.redbooks.ibm.com/abstracts/sg247940.html

* PowerVM Best Practices: http://www.redbooks.ibm.com/abstracts/sg248062.html
