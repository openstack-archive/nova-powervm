..
 This work is licensed under a Creative Commons Attribution 3.0 Unported
 License.

 http://creativecommons.org/licenses/by/3.0/legalcode

=========================
File I/O Cinder Connector
=========================

https://blueprints.launchpad.net/nova-powervm/+spec/file-io-cinder-connector

There are several Cinder drivers that support having the file system mounted
locally and then connecting in to the VM as a volume (ex. GPFS, NFS, etc...).
There is the ability to support this type of volume in PowerVM, if the user
has mounted the file system to the NovaLink.  This blueprint adds support to
the PowerVM driver to support such Cinder volumes.


Problem description
===================

The PowerVM driver supports Fibre Channel and iSCSI based volumes.  It does not
currently support volumes that are presented on a file system as files.

The recent release of PowerVM NovaLink has added support for this in the REST
API.  This blueprint looks to take advantage of that support.


Use Cases
---------

* As a user, I want to attach a volume that is backed by a file based Cinder
  volume (ex. NFS or GPFS).

* As a user, I want to detach a volume that is backed by a file based Cinder
  volume (ex. NFS or GPFS).


Proposed change
===============

Add nova_powervm/virt/powervm/volume/fileio.py.  This would extend the existing
volume drivers.  It would store the LUN ID on the scsi bus.

This does not support traditional VIOS.  Like the iSCSI change, it would
require running through the NovaLink partition.


Alternatives
------------

None


Security impact
---------------

None.

One may consider the permission of the file presented by Cinder.  The Cinder
driver's BDM will provide a path to a file.  The hypervisor will map that file
as the root user.  So file permissions of the volume should not be a concern.
This seems consistent with the other hypervisors utilizing these types of
Cinder drivers.

End user impact
---------------

None


Performance Impact
------------------

None


Deployer impact
---------------

Deployer must set up the backing Cinder driver and connect the file systems to
the NovaLink partition in their environment.


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
  shyama

Work Items
----------

* Create a nova-powervm fileio cinder volume connector.  Create associated UT.

* Validate with the GPFS cinder backend.


Dependencies
============

* pypowervm 1.0.0.4 or higher


Testing
=======

Unit Testing is obvious.

Manual testing will be driven via connecting to a GPFS back-end.

CI environments will be evaluated to determine if there is a way to add this
to the current CI infrastructure.


Documentation Impact
====================

None.  Will update the nova-powervm dev-ref to reflect that 'file I/O drivers'
are supported, but the support matrix doesn't go into the detail of what cinder
drivers work with nova drivers.


References
==========

* pypowervm add storage element to scsi mapping: https://github.com/powervm/pypowervm/blob/release/1.0.0.4/pypowervm/tasks/scsi_mapper.py#L49

* pypowervm file storage element: https://github.com/powervm/pypowervm/blob/release/1.0.0.4/pypowervm/wrappers/storage.py#L689
