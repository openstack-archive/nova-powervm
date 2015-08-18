==========================================
Support for PowerVM Performance Monitoring
==========================================

Include the URL of your launchpad blueprint:

https://blueprints.launchpad.net/ceilometer/+spec/example
https://blueprints.launchpad.net/python-ceilometerclient/+spec/example

The IBM PowerVM hypervisor provides virtualization on POWER hardware.
PowerVM customers can see benefits in their environments by making use
of OpenStack. This blueprint outlines implementing Ceilometer-compatible
compute agent plugins. These, along with a Nova driver and Neutron agent
defined in other blueprints, will provide capability for PowerVM
customers to natively monitor utilization and statistics for instances
running on OpenStack managed systems.

Problem description
===================

PowerVM supports a variety of Performance Monitoring interfaces within
the platform, providing virtual machine and system monitoring data.
Today, however, Ceilometer does not collect metrics for virtual machines
running on the PowerVM platform. With the proposal of a PowerVM Nova
driver it is necessary to extend support for PowerVM into Ceilometer as
well.

This blueprint will outlines implementing Ceilometer-compatible compute
agent plugins for the PowerVM hypervisor. With this update the
Ceilometer compute agent will run on each PowerVM compute node and will
poll against the PowerVM REST API to retrieve data on running instances.

Proposed change
===============

This blueprint, along with associated blueprints in Nova and Neutron,
plans to bring the PowerVM hypervisor into the OpenStack community.

The Ceilometer compute agent provides a framework that allows
hypervisors to integrate support for gathering instance statistics and
utilization details into Ceilometer. The proposed change plans to build
upon the work that the Ceilometer community has done within the compute
agent by adding support for gathering instance data from the PowerVM
Performance Monitoring infrastructure.

A new compute agent inspector will be added for PowerVM. This will
follow the existing inspector specification and poll PowerVM Performance
Monitoring to retrieve instance monitoring data. Data on instance cpu,
network, memory, and disk usage will be returned by the inspector when
called by the associated pollster. Interactions with PowerVM Performance
Monitoring will occur using the PowerVM REST API stack via an open
source python wrapper to be defined.

These changes are planned to be developed in StackForge under a powervm
project. A separate blueprint will be proposed for the ‘L’ release of
OpenStack to promote it to the Ceilometer project. This will allow for a
longer period of time to show functional testing and drive maturity.

Until the promotion to the core Ceilometer project is complete, these
changes will be marked experimental.

Alternatives
------------

Alternatives include writing a PowerVM-specific compute agent or
separate PowerVM-specific compute agent plugins. These solutions do not
follow the existing Ceilometer standard for hypervisor instance
monitoring, would not fit within broader project goals, and would end up
re-implementing logic.

Data model impact
-----------------

None

REST API impact
---------------

None

Security impact
---------------

None

Pipeline impact
---------------

This change extends the existing Ceilometer compute agent and should
have no Pipeline impact.

Other end user impact
---------------------

None to the deployer.

For the Kilo release of OpenStack, the administrator will need to obtain
the change from StackForge (and understand its experimental status).

Performance/Scalability Impacts
-------------------------------

This change should have no impacts to performance or scalability of the
system. The amount of data being gathered and processed will be similar
to that of the existing libvirt, vmware, and hyper-v inspectors.

Other deployer impact
---------------------

The cloud administrator will need to refer to documentation on how to
configure OpenStack for use with a PowerVM hypervisor.

Assuming the PowerVM REST API and Python wrapper library are available,
no additional configuration should be required.

Developer impact
----------------

None

Implementation
==============

Assignee(s)
-----------

Primary assignee: adreznec

Other contributors: thorst kyleh dwarcher

Ongoing maintainer: adreznec

Work Items
----------

-  Create a PowerVM-specific inspector in the
   /ceilometer/compute/virt/powervm folder. Stub out the methods.

-  Create a PowerVM helper utility for interfacing with the PowerVM REST
   API python wrapper.

-  Implement inspector calls to match the virt inspector abstraction,
   making calls against the PowerVM REST API to collect cpu, vnic, disk,
   and memory statistics.

-  Provide extensive unit tests (part of other work items).

-  Implement a functional automation server that listens for incoming
   change set commits from the community and provides a non-gating vote
   (+1 or -1) on the change.

Future lifecycle
================

Ongoing maintenance of the PowerVM compute agent inspector will be
handled by the IBM OpenStack team over the course of the next two
release cycles. In the Kilo timeframe this change will remain in
StackForge, with a target of merging in the ‘L’ timeframe and continued
maintenance and updates as the Ceilometer architecture requires.

Dependencies
============

-  The Ceilometer compute agent.

-  Will utilize the PowerVM REST API specification for management. Will
   utilize future versions of this specification as it becomes
   available: http://ibm.co/1lThV9R

-  Will build on top of a new open source python binding to previously
   noted PowerVM REST API. This will be a prerequisite to utilizing the
   driver.

Testing
=======

Tempest Tests
-------------

Since the tempest tests should be implementation-agnostic, the existing
tempest tests should be able to run against the PowerVM polling code
without issue. This blueprint does not foresee any changes based off
this work.

Thorough unit tests will be created with the agent to validate specific
functions within this implementation.

Functional Tests
----------------

A third party functional test environment will be created. It will
monitor for incoming neutron change sets. Once it detects a new change
set, it will utilize the existing lifecycle API tests. A non-gating vote
(+1 or -1) will be provided with information (logs) based on the result.

API Tests
---------

The REST APIs are not planned to change as part of this. Existing APIs
should be valid. All testing is planned within the functional testing
system and via unit tests.

Documentation Impact
====================

No documentation additions are anticipated. If the existing developer
documentation is updated to reflect more hypervisor-specific items, this
agent will follow suit.

References
==========

-  Ceilometer Architecture:
   http://docs.openstack.org/developer/ceilometer/architecture.html

-  PowerVM REST API Initial Specification (may require a newer version
   as they become available): http://ibm.co/1lThV9R

-  PowerVM Virtualization Introduction and Configuration:
   http://www.redbooks.ibm.com/abstracts/sg247940.html?Open

-  PowerVM Best Practices:
   http://www.redbooks.ibm.com/abstracts/sg248062.html?Open

