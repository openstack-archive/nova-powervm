..
 This work is licensed under a Creative Commons Attribution 3.0 Unported
 License.

 http://creativecommons.org/licenses/by/3.0/legalcode

==============================================
Allow dynamic enable/disable of SRR capability
==============================================

Include the URL of your launchpad blueprint:

https://blueprints.launchpad.net/nova-powervm/+spec/srr-capability-dynamic-toggle

Currently to enable or disable the SRR capability on the VM we need to have
the VM in shutoff state. We should be able to toggle this field dynamically
so that the shutdown of a VM is not needed.

Problem description
===================

The simplified remote restart (SRR) capability governs whether a VM can be
rebuilt (remote restarted) on a different host when the host on which the
VM resides is down. Currently this attribute can be changed only when the VM
is in shut-off state. This blueprint addresses that by enabling toggle
of simplified remote restart capability dynamically (while the VM is still
active).


Use Cases
---------

The end user would like to :
- Enable the srr capability on the VM without shutting it down so that any
workloads on the VM are unaffected.
- Disable the srr capability for a VM which need not be rebuilt to another
host while the VM is still up and running.


Proposed change
===============
The SRR capability is a VM level attribute and can be changed using
the resize operation. In case of a resize operation for an active VM
- Check if the hypervisor supports dynamic toggle of srr capability.
- If it is supported proceed with updating of srr capability if it has been
changed.
- Throw a warning if update of srr capability is not supported.


Alternatives
------------

None


Security impact
---------------

None


End user impact
---------------

None


Performance Impact
------------------

The change is srr capability is not likely to happen very frequently so this
should not have a major impact. When the change happens the impact on the
performance of any other component (the VM, the compute service, the REST
service, etc.) should be negligible.


Deployer impact
---------------

End user will be able to dynamically the toggle the srr capability for the
VM. The changes can be utilized immediately once they are deployed.


Developer impact
----------------

None

Implementation
==============

Assignee(s)
-----------


Primary assignee:
  manasmandlekar

Other contributors:
  shyvenug

Work Items
----------
NA

Dependencies
============

Need to work with PowerVM platform team to ensure that the srr toggle
capability is exposed for the Compute driver to consume.


Testing
=======

The testing of the change requires full Openstack environment with
Compute resources configured.
- Ensure srr state for VM can be toggled when it is up and running.
- Ensure srr state for VM can be toggled when it is shut-off.
- Perform rebuild operations to ensure that the capability is indeed
getting utilized.


Documentation Impact
====================

None


References
==========

None


History
=======

.. list-table:: Revisions
   :header-rows: 1

   * - Release Name
     - Description
   * - Pike
     - Introduced
