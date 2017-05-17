..
 This work is licensed under a Creative Commons Attribution 3.0 Unported
 License.

 http://creativecommons.org/licenses/by/3.0/legalcode

================================
Linux Bridge and OVS VIF Support
================================

`Launchpad BluePrint`_

.. _`Launchpad BluePrint` : https://blueprints.launchpad.net/nova-powervm/+spec/powervm-addl-vif-types

Currently the PowerVM driver requires a PowerVM specific Neutron agent.  This
blueprint will add support for additional agent types - specifically the
Open vSwitch and Linux Bridge agents provided by Neutron.

Problem description
===================

PowerVM has support for virtualizing an Ethernet port using the Virtual I/O
Server and Shared Ethernet.  This is provided using networking-powervm
Shared Ethernet Agent.  This agent provides key PowerVM use cases such as I/O
redundancy.

There are a subset of operators that have asked for VIF support in line with
other hypervisors.  This would be support for the Neutron Linux Bridge Agent
and Open vSwitch agent.  While these agents do not provide use cases such as
I/O redundancy, they do enable operators to utilize common upstream networking
solutions when deploying PowerVM with OpenStack


Use Cases
---------

An operator should be able to deploy an environment using Linux Bridge or
Open vSwitch Neutron agents.  In order to do this, the physical I/O must be
assigned to the NovaLink partition on the PowerVM system (the partition with
virtualization admin authority).

A user should be able to do the standard VIF use cases with either of these
agents:
 - Add NIC
 - Remove NIC
 - Security Groups
 - Multiple Network Types (Flat, VLAN, vxlan)
 - Bandwidth limiting

The existing Neutron agents should be used without any changes from PowerVM.
All of the changes that should occur will be in nova-powervm.  Any limitations
of the agents themselves will be limitations to the PowerVM implementation.

There is one exception to the use case support.  The Open vSwitch support will
enable live migration.  There is no plan for Linux Bridges live migration
support.


Proposed change
===============

 - Create a parent VIF driver for NovaLink based I/O.  This will hold the code
   that is common between the Linux Bridge VIFs and OVS VIFs.  There will be
   common code due to both needing to run on the NovaLink management VM.

 - The VIF drivers should create a Trunk VEA on the NovaLink partition for
   each VIF.  It will be given a unique channel of communication to the VM.
   The device will be named according to the Neutron device name.

 - The OVS VIF driver will use the nova linux_net code to set the metadata on
   the trunk adapter.

 - Live migration will suspend the VIF on the target host until it has been
   treated.  Treating means ensuring that the communication to the VM is on
   a unique channel (its own VLAN on a vSwitch).

 - A private PowerVM virtual switch named 'NovaLinkVEABridge' will be created
   to support the private communication between the trunk adapters and the
   VMs.

 - Live migration on the source will need to clean up the remaining trunk
   adapter for Open vSwitch that is left around on the management VM.

It should be noted that Hybrid VIF plugging will not be supported.  Instead,
PowerVM will use the conntrack integration in Ubuntu 16.04/OVS 2.5 to support
the OVSFirewallDriver.  As of OVS 2.5, that allows the firewall function
without needing Hybrid VIF Plugging.

Alternatives
------------

None.


Security impact
---------------

None.


End user impact
---------------

None.


Performance Impact
------------------

Performance will not be impacted for the deployment of VMs.  However, the
end user performance may change as it is a new networking technology.  Both
the Linux Bridge and Open vSwitch support should operate with similar
performance characteristics as other platforms that support these technologies.


Deployer impact
---------------

The deployer will need to do the following:
* Attach an Ethernet I/O Card to the NovaLink partition.  Configure the ports
  in accordance with the Open vSwitch or Linux Bridge Neutron Agent's
  requirements.

* Run the agent on their NovaLink management VM.

No major changes are anticipated outside of this.  The Shared Ethernet
Adapter Neutron agent will not work in conjunction with this on the same
system.


Developer impact
----------------

None

Implementation
==============

Assignee(s)
-----------

Primary assignee:
  thorst

Other contributors:
  kriskend
  tjakobs

Work Items
----------

See Proposed Change


Dependencies
============

* NovaLink core changes will be needed with regard to the live migration flows.
  This requires NovaLink 1.0.0.3 or later.


Testing
=======

Testing will be done on live systems.  Future work will be done to integrate
into the PowerVM Third-Party CI, however this will not be done initially as the
LB and OVS agents are heavily tested.  The SEA Agent continues to need to be
tested.


Documentation Impact
====================

Deployer documentation will be built around how to configure this.


References
==========

`Neutron Networking Guide`_

.. _`Neutron Networking Guide`: http://docs.openstack.org/newton/networking-guide/
