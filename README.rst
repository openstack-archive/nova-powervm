==========================================
Support for PowerVM Performance Monitoring
==========================================

The IBM PowerVM hypervisor provides virtualization on POWER hardware.
PowerVM customers can see benefits in their environments by making use
of OpenStack. This project implements a Ceilometer-compatible compute
inspector.  This inspector, along with the PowerVM Nova driver and Neutron
agent, provides capability for PowerVM customers to natively monitor
utilization and statistics for instances running on OpenStack-managed systems.


Problem Description
===================

PowerVM supports a variety of performance monitoring interfaces within
the platform, providing virtual machine and system monitoring data.
Ceilometer-powervm implements a Ceilometer-based compute inspector for the
PowerVM hypervisor.

Inspector Description
=====================

The Ceilometer compute agent provides an inspector framework that allows
hypervisors to integrate support for gathering instance statistics and
utilization details into Ceilometer. This project provides a standard
Ceilometer virt inspector that pulls its data from the PowerVM Performance and
Capacity Monitoring (PCM) infrastructure.

This inspector retrieves instance monitoring data for cpu, network, memory, and
disk usage. Interactions with PowerVM PCM occur using the PowerVM REST API
stack through `pypowervm`_, an open source python project.

This inspector requires that the PowerVM system be configured for management
via `NovaLink`_.

.. _pypowervm: https://github.com/powervm/pypowervm
.. _NovaLink: http://www-01.ibm.com/common/ssi/cgi-bin/ssialias?infotype=AN&subtype=CA&htmlfid=897/ENUS215-262&appname=USN


End User Impact
---------------

The users of the cloud are able to see the metrics for their virtual machines.
As PowerVM deals with 'disk buses' rather than specific disks, the hard disk
data is reported at a 'per bus' level (i.e. each SCSI or Virtual Fibre Channel
bus).

Performance/Scalability Impacts
-------------------------------

None.

Other deployer impact
---------------------

The cloud administrator needs to install the ceilometer-powervm project on
their PowerVM compute node.  It must be installed on the `NovaLink`_ virtual
machine on the PowerVM system.

The cloud administrator needs to configure their 'hypervisor_inspector' as
powervm.

No other configuration is required.

Developer impact
----------------

None

Implementation
==============

Assignee(s)
-----------

Primary assignee: thorst

Ongoing maintainer: thorst


Future lifecycle
================

Ongoing maintenance of the PowerVM compute inspector will be handled by the IBM
OpenStack team.

Dependencies
============

-  The Ceilometer compute agent.

-  The `pypowervm`_ library.

-  A `NovaLink`_ enabled PowerVM system.

References
==========

-  Ceilometer Architecture:
   http://docs.openstack.org/developer/ceilometer/architecture.html

-  pypowervm: https://github.com/powervm/pypowervm

-  NovaLink: http://www-01.ibm.com/common/ssi/cgi-bin/ssialias?infotype=AN&subtype=CA&htmlfid=897/ENUS215-262&appname=USN

-  PowerVM REST API Initial Specification (may require a newer version
   as they become available): http://ibm.co/1lThV9R

-  PowerVM Virtualization Introduction and Configuration:
   http://www.redbooks.ibm.com/abstracts/sg247940.html?Open

-  PowerVM Best Practices:
   http://www.redbooks.ibm.com/abstracts/sg248062.html?Open
