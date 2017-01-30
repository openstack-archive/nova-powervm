..
 This work is licensed under a Creative Commons Attribution 3.0 Unported
 License.

 http://creativecommons.org/licenses/by/3.0/legalcode

===============
File I/O Driver
===============

https://blueprints.launchpad.net/nova-powervm/+spec/file-io-driver

The PowerVM driver currently uses logical volumes for localdisk ephemeral
storage.  This blueprint will add support for using file-backed disks as a
localdisk ephemeral storage option.


Problem description
===================

The PowerVM driver only supports logical volumes for localdisk ephemeral
storage.  It does not currently support storage that is presented as a file.


Use Cases
---------

* As a user, I want to have the instance ephemeral storage backed by a file.


Proposed change
===============

Add nova_powervm/virt/powervm/disk/fileio.py.  This would extend the existing
disk driver.  Use the DISK_DRIVER powervm conf option to select file I/O.
Will utilize the nova.conf option instances_path.


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

Performance may change as the backing storage methods of VMs will be different.


Deployer impact
---------------

The deployer must set the DISK_DRIVER conf option to fileio and ensure that
the instances_path conf option is set in order to utilize the changes described
in the blueprint.


Developer impact
----------------

None


Implementation
==============

Assignee(s)
-----------

Primary assignee:
  tjakobs

Other contributors:
  None

Work Items
----------

* Create a nova-powervm fileio driver.  Create associated UT.


Dependencies
============

Novalink 1.0.0.5


Testing
=======

* Unit tests for all code

* Manual test will be driven using a File I/O ephemeral disk.


Documentation Impact
====================


Will update the nova-powervm dev-ref to include File I/O as an additional
ephemeral disk option.


References
==========

None
