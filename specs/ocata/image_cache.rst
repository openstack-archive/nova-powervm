..
 This work is licensed under a Creative Commons Attribution 3.0 Unported
 License.

 http://creativecommons.org/licenses/by/3.0/legalcode

========================================
Image Cache Support for localdisk driver
========================================

https://blueprints.launchpad.net/nova-powervm/+spec/image-cache-powervm

The image cache allows for a nova driver to pull an image from glance once,
then use a local copy of that image for future VM creation.  This saves
bandwidth between the compute host and glance.  It also improves VM
deployment speed and reduces the stress on the overall infrastructure.


Problem description
===================

Deploy times on PowerVM can be high when using the localdisk driver.  This is
partially due to not having linked clones.  The image cache offers a way to
reduce those deploy times by transferring the image to the host once, and then
subsequent deploys will reuse that image rather than streaming from glance.

There are complexities with this of course.  The cached images take up disk space,
but the overall image cache from core Nova takes that into account.  The value
of using the nova image cache design is that it has hooks in the code to help solve
these problems.


Use Cases
---------

 - As an end user, subsequent deploys of the same image should go faster


Proposed change
===============

Create a subclass of nova.virt.imagecache.ImageManager in the nova-powervm
project. It should implement the necessary methods of the cache:
 - _scan_base_images
 - _age_and_verify_cached_images
 - _get_base
 - update

The nova-powervm driver will need to be updated to utilize the cache.  This
includes:
 - Implementing the manage_image_cache method
 - Adding the has_imagecache capability

The localdisk driver within nova-powervm will be updated to have the
following logic.  It will check the volume group backing the instance.  If the
volume group has a disk with the name 'i_<partial uuid of image>', it will
simply copy that disk into a new disk named after the UUID of the instance.
Otherwise, it will create a disk with the name 'i_<partial uuid of image>'
that contains the image.

The image cache manager's purpose is simply to clean out old images that are
not needed by any instances anymore.

Further extension, not part of this blueprint, can be done to manage overall
disk space in the volume group to make sure that the image cache is not
overwhelming the backing disks.

Alternatives
------------

 - Leave as is, all deploys potentially slow
 - Implement support for linked clones.  This is an eventual goal, but
   the image cache is still needed in this case as it will also manage the
   root disk image.


Security impact
---------------

None


End user impact
---------------

None


Performance Impact
------------------

Performance of subsequent deploys of the same image should be faster.
The deploys will have improved image copy times and reduced network
bandwith requirements.

Performance of single deploys using different images will be slower.


Deployer impact
---------------

This change will take effect without any deployer impact immediately after
merging.  The deployer will not need to take any specific upgrade actions to
make use of it; however the deployer may need to tune the image cache to make
sure it is not using too much disk space.

A conf option may be added to force the image cache off if deemed necessary.
This will be based off of operator feedback in the event that we need a way
to reduce disk usage.


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

* Implement the image cache code for the PowerVM driver

* Include support for the image cache in the PowerVM driver.  Tolerate it
  for other disk drivers, such as SSP.


Dependencies
============

None


Testing
=======

* Unit tests for all code

* Deployment tests in local environments to verify speed increases


Documentation Impact
====================

The deployer docs will be updated to reflect this.


References
==========

None


History
=======

Optional section intended to be used each time the spec is updated to describe
new design.

.. list-table:: Revisions
   :header-rows: 1

   * - Release Name
     - Description
   * - Newton
     - Introduced
