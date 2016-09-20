..
 This work is licensed under a Creative Commons Attribution 3.0 Unported
 License.

 http://creativecommons.org/licenses/by/3.0/legalcode

=================================
Nova support for SR-IOV VIF Types
=================================

https://blueprints.launchpad.net/nova-powervm/+spec/powervm-sriov-nova

This blueprint will address support of SR-IOV in conjunction with SR-IOV
VF attached to VM via PowerVM vNIC into nova-powervm. SR-IOV support
was added to Juno release of OpenStack, this blueprint will fit
this scenario implementation into it.

A separate `blueprint for networking-powervm`_ has been made available for
design elements regarding networking-powervm.

These blueprints will be implemented during Newton cycle of OpenStack
development. Referring to Newton schedule, development should be completed
during newton-3.

Refer to glossary section for explanation of terms.

.. _`blueprint for networking-powervm`: https://review.openstack.org/#/c/322210/

Problem Description
===================
OpenStack PowerVM drivers currently support networking aspect of PowerVM
virtualization using Shared Ethernet Adapter, Open vSwitch and Linux Bridge.
There is a need for supporting SR-IOV ports with redundancy/failover and
migration. It is possible to associate SR-IOV VF to a VM directly, but this path
will not be supported by this design. Such a setup will not provide migration
support anyway. Support for this configuration will be added in future. This
path also does not utilize advantages of hardware level virtualization offered
by SR-IOV architecture.

Users should be able to manage a VM with SR-IOV vNIC as a network interface.
This management should include migration of VM with SR-IOV vNIC attached to it.

PowerVM has a feature called vNIC which can is tied in with SR-IOV. By using
vNIC the following use cases are supported:
- Fail over I/O to a different I/O Server and physical function
- Live Migration with SR-IOV, without significant intervention
The vNIC is exposed to the VM, and the mac address of the client vNIC will
match the neutron port.

In summary, this blueprint will solve support of SR-IOV in nova-powervm for
these scenarios:

1. Ability to attach/detach a SR-IOV VF to a VM as a network interface using
   vNIC intermediary during and after deployment, including migration.
2. Ability to provide redundancy/failover support across VFs from Physical Ports
   within or across SR-IOV cards using vNIC intermediary.
3. Ability to associate a VLAN with vNIC backed by SR-IOV VF.

Ability to associate a SR-IOV VF directly to a VM will be done in future.

Refer to separate `blueprint for networking-powervm`_ for changes in
networking-powervm component. This blueprint will focus on changes to
nova-powervm only.

Use Cases
---------
1. Attach vNIC backed by SR-IOV VF(s) to a VM during boot time
2. Attach vNIC backed by SR-IOV VF(s) to a VM after it is deployed
3. Detach vNIC backed by SR-IOV VF(s) from a VM
4. When a VM with vNIC backed by SR-IOV is deleted, perform detach and cleanup
5. Live migrate a VM if using vNIC backed SR-IOV support
6. Provide redundancy/failover support of vNIC backed by SR-IOV VF attached to
   a VM during both deploy and post deploy scenarios.

Proposed changes
================
The changes will be made in two areas:

1. **Compute virt driver.**
PowerVM compute driver is in nova.virt.powervm.driver.PowerVMDriver and it will
be enhanced for SR-IOV vNIC support.  A dictionary is maintained in virt driver
vif code to map between vif type and vif driver class. Based on vif type of vif
object that needs to be plugged, appropriate vif driver will be invoked. This
dictionary will be modified to include a new vif driver class and its vif type
(pvm_sriov).

The PCI Claims process expects to be able to "claim" a VF from the
``pci_passthrough_devices`` list each time a vNIC is plugged, and return it to
the pool on unplug.  Thus the ``get_available_resource`` API will be enhanced to
populate this device list with a suitable number of VFs.

2. **VIF driver.**
PowerVM VIF driver is in nova_powervm.virt.powervm.vif.PvmVifDriver. A VIF
driver to attach network interface via vNIC (PvmVnicSriovVifDriver) and plug/
unplug methods will be implemented. Plug and unplug methods will use pypowervm
code to create VF/vNIC server/vNIC clients and attach/detach them. Neutron port
carries binding:vif_type and binding:vnic_type attributes. The vif type for this
implementation will be pvm_sriov. The vnic_type will be 'direct'.

A VIF driver (PvmVFSriovVifDriver) for directly attached to VM will get
implemented in future.

Deployment of VM with SR-IOV vNIC will involve picking Physical Port(s),
VIOS(es) and a VM and invoking pypowervm library. Similarly, attachment of the
same to an existing VM will be implemented.  RMC will be required. Evacuate and
migration of VM will be supported with changes to compute virt driver and VIF
driver via pypowervm library.

Physical Port information will be derived from port label attribute of physical
ports on SR-IOV adapters. Port label attribute of physical ports will have to be
updated with 'physical network' names during configuration of the environment.
During attachment of SR-IOV backed vNIC to a VM, physical network attribute of
neutron network will be matched with port labels of physical ports to gather a
list of physical ports.

**Failover/redundancy:** VIF plug during deploy (or attach of network interface
to a VM) will pass more than one Physical port and VIOS(es) (as stated above in
deploy scenario) to pypowervm library to create vNIC on VIOS with redundancy. It
should be noted that failover is handled automatically by the platform when a
vNIC is backed by multiple VFs. The redundancy level will be controlled by an
``AGENT`` option ``vnic_required_vfs`` in the ML2 configuration file (see the
`blueprint for networking-powervm`_).  It will have a default of 2. 

**Quality of Service:** Each VF backing a vNIC can be configured with a capacity
value, dictating the minimum percentage of the physical port's total bandwidth
that will be available to that VF.  The ML2 configuration file allows a
``vnic_vf_capacity`` option in the ``AGENT`` section to set the capacity for all
vNIC-backing VFs.  If omitted, the platform defaults to the capacity granularity
for each physical port.  See the `blueprint for networking-powervm`_ for
details of the configuration option; and see section 1.3.3 of the `IBM Power
Systems SR-IOV Technical Overview and Introduction
<https://www.redbooks.ibm.com/redpapers/pdfs/redp5065.pdf>`_ for details on VF
capacity.

For future implementation of VF - VM direct attach of SR-IOV to a VM, the
request will include physical network name. PvmVFSriovVifDriver can lookup
devname(s) associated with it from port label, get physical port information
and create a SR-IOV logical port on the corresponding VM.
Or may include a configuration option to allow the user to dictate how many
ports to attach. Using NIB technique, users can setup redundancy.

For VF - vNIC - VM attach of SR-IOV port to a VM, the corresponding neutron
network object will include physical network name, PvmVnicSriovVifDriver can
lookup devname(s) associated with it from port label, get physical port
information. Along with adapter ID and physical port ID, VIOS information will
be added and a VNIC dedicated port on the corresponding VM will be created.

For migration scenario, physical network names should match on source and
destination compute nodes, and accordingly in the physical port labels. On the
destination, vNICs will be rebuilt based on the SR-IOV port configuration.  The
platform decides how to reconstruct the vNIC on the destination in terms of
number and distribution of backing VFs, etc.

Alternatives
------------
None

Security impact
---------------
None

Other end user impact
---------------------
None

Performance impact
------------------
Since the number of VMs deployed on the host will depend on number of VFs
offered by SR-IOV cards in the environment, scale tests will be limited in
density of VMs.

Deployer impact
---------------
1. SR-IOV cards must be configured in ``Sriov`` mode.  This can be done via the
   ``pvmctl`` command, e.g.:

  ``pvmctl sriov update -i phys_loc=U78C7.001.RCH0004-P1-C1 -s mode=Sriov``

2. SR-IOV physical ports must be labeled with the name of the neutron physical
   network to which they are cabled.  This can be done via the ``pvmctl``
   command, e.g.:

  ``pvmctl sriov update --port-loc U78C7.001.RCH0004-P1-C1-T1 -s label=prod_net``

3. The ``pci_passthrough_whitelist`` option in the nova configuration file must
   include entries for each neutron physical network to be enabled for vNIC.
   Only the ``physical_network`` key is required.  For example:

  ``pci_passthrough_whitelist = [{"physical_network": "default"}, {"physical_network": "prod_net"}]``

Configuration is also required on the networking side - see the `blueprint for
networking-powervm`_ for details.

**To deploy a vNIC to a VM,** the neutron port(s) must be pre-created with vnic
type ``direct``, e.g.:

  ``neutron port-create --vnic-type direct``

Developer impact
----------------
None

Dependencies
------------

#. SR-IOV cards and SR-IOV-capable hardware
#. Updated levels of system firmware and the Virtual I/O Server operating system
#. An updated version of Novalink PowerVM feature
#. pypowervm library - https://github.com/powervm/pypowervm

Implementation
==============

Assignee(s)
-----------
- Eric Fried (efried)
- Sridhar Venkat (svenkat)
- Eric Larese (erlarese)
- Esha Seth (eshaseth)
- Drew Thorstensen (thorst)

Work Items
----------
nova-powervm changes:

- Updates to PowerVM compute driver to support attachment of SR-IOV VF via vNIC.
- VIF driver for SR-IOV VF connected to VM via vNIC.
- Migration of VM with SR-IOV VF connected to VM via vNIC. This involves live
  migration, cold migration and evacuation.
- Failover/redundancy support for SR-IOV VF(s) connected to VM via vNIC(s).

VIF driver for SR-IOV VF connected to VM directly will be a future work item.

Testing
=======
1. Unit test
All developed code will accompany structured unit test around them. These
tests validate granular function logic.

2. Function test
Function test will be performed along with CI infrastructure.  Changes
implemented for this blueprint will be tested via CI framework that exists
and used by IBM team. CI framework needs to be enhanced with SR-IOV hardware.
The tests can be executed in batch mode, probably as nightly jobs.

Documentation impact
====================
All use-cases need to be documented in developer docs that accompany
nova-powervm.

References
==========
1. This blog describes how to work with SR-IOV and vNIC (without redundancy/
   failover) using HMC interface: http://chmod666.org/index.php/a-first-look-at-sriov-vnic-adapters/

2. These describe vNIC and its usage with SR-IOV.

   - https://www.ibm.com/developerworks/community/wikis/home?lang=en_us#!/wiki/Power%20Systems/page/vNIC%20-%20Introducing%20a%20New%20PowerVM%20Virtual%20Networking%20Technology
   - https://www.ibm.com/developerworks/community/wikis/home?lang=en_us#!/wiki/Power%20Systems/page/Introduction%20to%20SR-IOV%20FAQs
   - https://www.ibm.com/developerworks/community/wikis/home?lang=en_us#!/wiki/Power%20Systems/page/Introduction%20to%20vNIC%20FAQs
   - https://www.ibm.com/developerworks/community/wikis/home?lang=en#!/wiki/Power%20Systems/page/vNIC%20Frequently%20Asked%20Questions

3. These describe SR-IOV in OpenStack.

   - https://wiki.openstack.org/wiki/Nova-neutron-sriov
   - http://docs.openstack.org/mitaka/networking-guide/adv-config-sriov.html

4. This blueprint addresses SR-IOV attach/detach function in nova: https://review.openstack.org/#/c/139910/

5. networking-powervm blueprint for same work: https://review.openstack.org/#/c/322210/

6. This is a detailed description of SR-IOV implementation in PowerVM: https://www.redbooks.ibm.com/redpapers/pdfs/redp5065.pdf

7. This provides a overall view of SR-IOV support in nova: https://blueprints.launchpad.net/nova/+spec/pci-passthrough-sriov

8. Attach/detach of SR-IOV ports to VM with respect to libvirt. Provided here
   for comparison purposes: https://review.openstack.org/#/c/139910/

9. SR-IOV PCI passthrough reference: https://wiki.openstack.org/wiki/SR-IOV-Passthrough-For-Networking

10. pypowervm: https://github.com/powervm/pypowervm

Glossary
========
:SR-IOV: Single Root I/O Virtualization, used for virtual environments where VMs
  need direct access to network interface without any hypervisor overheads.

:Physical Port: Represents Physical port in SR-IOV adapter. This is not same
  as Physical Function. A Physical Port can have many physical functions
  associated with it. To clarify further, if a Physical Port supports RCoE, then
  it will have two Physical Functions. In other words, one Physical Function per
  protocol that port supports.

:Virtual Function (VF): Represents Virtual port belonging to a Physical Port
  (PF). Either directly or indirectly (using vNIC) a Virtual Function (VF) is
  connected to a VM. This is otherwise called SR-IOV logical port.

:Dedicated SR-IOV: This is equivalent to any regular ethernet card and it
  can be used with SEA. A logical port of a physical port can be assigned as a
  backing device for SEA.

:Shared SR-IOV: A VF to VM is not supported in Newton release. But an SR-IOV
  card in sriov mode is what we will be used for vNIC as described in this
  blueprint. Also, a SR-IOV in Sriov mode can have a promiscous VF assigned to
  the VIOS and configured for SEA(said configuration to be done outside of the
  auspices of OpenStack), which can then be used just like any other SEA
  configuration, and is supported (as described in next item below).

:Shared Ethernet Adapter: Alternate technique to provide network interface to a
  VM.

  This involves attachment to a physical interface on PowerVM host and one or
  many virtual interfaces that are connected to VMs. A VF of PF in SR-IOV based
  environment can be a physical interface to Shared Ethernet Adapter. Existing
  support for this configuration in nova-powervm and networking-powervm will
  continue.

:vNIC: A vNIC is an intermediary between VF of PF and VM. This resides on VIOS
  and connects to a VF one one end and vNIC client adapter inside a VM.  This is
  mainly to support migration of VMs across hosts.

:vNIC failover/redundancy: Multiple vNIC servers (connected to as many VFs that
  belong to as many PFs either on same SR-IOV card or across) connected to same
  VM as one network interface. Failure of one vNIC/VF/PF path will result in
  activation of other such path.

:VIOS: A partition in PowerVM systems dedicated for i/o operations. In the
  context of this blueprint, vNIC server will be created on VIOS. For redundancy
  management purposes, a specific PowerVM system may employ more than one VIOS
  partitions.

:VM migration types:

    - **Live Migration:** migration of VM while both host and VM are alive.
    - **Cold Migration:** migration of VM while host is alive and VM is down.
    - **Evacuation:** migration of VM while hots is down (VM is down as well).
    - **Rebuild:** recreation of a VM.

:pypowervm: A python library that runs on the PowerVM management VM and allows
  virtualization control of the system. This is similar to the python library
  for libvirt.

History
=======

============    ===========
Release Name    Description
============    ===========
Newton          Introduced
============    ===========
