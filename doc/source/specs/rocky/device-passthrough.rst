..
 This work is licensed under a Creative Commons Attribution 3.0 Unported
 License.

 http://creativecommons.org/licenses/by/3.0/legalcode

==================
Device Passthrough
==================

https://blueprints.launchpad.net/nova-powervm/+spec/device-passthrough

Provide a generic way to identify hardware devices such as GPUs and attach them
to VMs.

Problem description
===================

Deployers want to be able to attach accelerators and other adapters to their
VMs. Today in Nova this is possible only in very restricted circumstances. The
goal of this blueprint is to enable generic passthrough of devices for
consumers of the nova-powervm driver.

While these efforts may enable more, and should be extensible going forward,
the primary goal for the current release is to pass through entire physical
GPUs. That is, we are not attempting to pass through:

* Physical functions, virtual functions, regions, etc. I.e. granularity smaller
  than "whole adapter". This requires device type-specific support at the
  platform level to perform operations such as discovery/inventorying,
  configuration, and attach/detach.
* Devices with "a wire out the back" - i.e. those which are physically
  connected to anything (networks, storage, etc.) external to the host. These
  will require the operator to understand and be able to specify/select
  specific connection parameters for proper placement.

Use Cases
---------
As an admin, I wish to be able to configure my host and flavors to allow
passthrough of whole physical GPUs to VMs.

As a user, I wish to make use of appropriate flavors to create VMs with GPUs
attached.

Proposed change
===============

Device Identification and Whitelisting
--------------------------------------
The administrator can identify and allow (explicitly) or deny (by omission)
passthrough of devices by way of a YAML file per compute host.

.. note:: **Future:** We may someday figure out a way to support a config file
          on the controller. This would allow e.g. cloud-wide whitelisting and
          specification for particular device types by vendor/product ID, which
          could then be overridden (or not) by the files on the compute nodes.

The path to the config will be hardcoded as ``/etc/nova/inventory.yaml``.

The file shall contain paragraphs, each of which will:

* Identify zero or more devices based on information available on the
  ``IOSlot`` NovaLink REST object. In pypowervm, given a ManagedSystem wrapper
  ``sys_w``, a list of ``IOSlot`` wrappers is available via
  ``sys_w.asio_config.io_slots``. See `identification`_. Any device not
  identified by any paragraph in the file is denied for passthrough. But see
  the `allow`_ section for future plans around supporting explicit denials.
* Name the resource class to associate with the resource provider inventory unit
  by which the device will be exposed in the driver. If not specified,
  ``CUSTOM_IOSLOT`` is used. See `resource_class`_.
* List traits to include on the resource provider in addition to those generated
  automatically. See `traits`_.

A `formal schema`_ is proposed for review.

.. _formal schema: https://review.openstack.org/#/c/579289/3/nova_powervm/virt/powervm/passthrough_schema.yaml

Here is a summary description of each section.

Name
~~~~
Each paragraph will be introduced by a key which is a human-readable name for
the paragraph. The name has no programmatic significance other than to separate
paragraphs. Each paragraph's name must be unique within the file.

identification
~~~~~~~~~~~~~~
Each paragraph will have an ``identification`` section, which is an object
containing one or more keys corresponding to ``IOSlot`` properties, as follows:

 ================  ====================  =====================================
 YAML key          IOSlot property       Description
 ================  ====================  =====================================
 vendor_id         pci_vendor_id         \X{4} (four uppercase hex digits)
 device_id         pci_dev_id            \X{4}            "
 subsys_vendor_id  pci_subsys_vendor_id  \X{4}            "
 subsys_device_id  pci_subsys_dev_id     \X{4}            "
 class             pci_class             \X{4}            "
 revision_id       pci_rev_id            \X{2} (two uppercase hex digits)
 drc_index         drc_index             \X{8} (eight uppercase hex digits)
 drc_name          drc_name              String (physical location code)
 ================  ====================  =====================================

The values are expected to match those produced by ``pvmctl ioslot list -d
<property>`` for a given property.

The ``identification`` section is required, and must contain at least one of
the above keys.

When multiple keys are provided in a paragraph, they are matched with ``AND``
logic.

.. note:: It is a stretch goal of this blueprint to allow wildcards in (some
          of) the values.  E.g. ``drc_name: U78CB.001.WZS0JZB-P1-*`` would
          allow everything on the ``P1`` planar of the ``U78CB.001.WZS0JZB``
          enclosure. If we get that far, a spec amendment will be proposed with
          the specifics (what syntax, which fields, etc.).

allow
~~~~~
.. note:: The ``allow`` section will not be supported initially, but is
          documented here because we thought through what it should look like.
          In the initial implementation, any device encompassed by a paragraph
          is allowed for passthrough.

Each paragraph will support a boolean ``allow`` keyword.

If omitted, the default is ``true`` - i.e. devices identified by this
paragraph's ``identification`` section are permitted for passthrough. (Note,
however, that devices not encompassed by the union of all the
``identification`` paragraphs in the file are denied for passthrough.)

If ``allow`` is ``false``, the only other section allowed is
``identification``, since the rest don't make sense.

A given device can only be represented once across all ``allow=true``
paragraphs (implicit or explicit); an "allowed" device found more than once
will result in an error.

A given device can be represented zero or more times across all ``allow=false``
paragraphs.

We will first apply the ``allow=true`` paragraphs to construct a preliminary
list of devices; and then apply each ``allow=false`` paragraph and remove
explicitly denied devices from that list.

.. note:: Again, we're not going to support the ``allow`` section at all
          initially. It will be a stretch goal to add it as part of this
          release, or it may be added in a subsequent release.

resource_class
~~~~~~~~~~~~~~
If ``allow`` is omitted or ``true``, an optional ``resource_class`` key is
supported. Its string value allows the author to designate the resource class
to be used for the inventory unit representing the device on the resource
provider. If omitted, ``CUSTOM_IOSLOT`` will be used as the default.

.. note:: **Future:** We may be able to get smarter about dynamically
          defaulting the resource class based on inspecting the device
          metadata. For now, we have to rely on the author of the config file
          to tell us what kind of device we're looking at.

traits
~~~~~~
If ``allow`` is omitted or ``true``, an optional ``traits`` subsection is
supported. Its value is an array of strings, each of which is the name of a
trait to be added to the resource providers of each device represented by this
paragraph. If the ``traits`` section is included, it must have at least one
value in the list. (If no additional traits are desired, omit the section.)

The values must be valid trait names (either standard from ``os-traits`` or
custom, matching ``CUSTOM_[A-Z0-9_]*``). These will be in addition to the
traits automatically added by the driver - see `Generated Traits`_ below.
Traits which conflict with automatically-generated traits will result in an
error: the driver must be the single source of truth for the traits it
generates.

Traits may be used to indicate any static attribute of a device - for example,
a capability (``CUSTOM_CAPABILITY_WHIZBANG``) not otherwise indicated by
`Generated Traits`_.

Resource Providers
------------------
The driver shall create nested resource providers, one per device (slot), as
children of the compute node provider generated by Nova.

.. TODO: Figure out how NVLink devices appear and how to handle them - ideally
   by hiding them and automatically attaching them with their corresponding
   device.

The provider name shall be generated as ``PowerVM IOSlot %(drc_index)08X`` e.g.
``PowerVM IOSlot 1C0FFEE1``. We shall let the placement service generate the
UUID. This naming scheme allows us to identify the full set of providers we
"own". This includes identifying providers we may have created on a previous
iteration (potentially in a different process) which now need to be purged
(e.g. because the slot no longer exists on the system). It also helps us
provide a clear migration path in the future, if, for example, Cyborg takes
over generating these providers. It also paves the way for providers
corresponding to things smaller than a slot; e.g. PFs might be namespaced
``PowerVM PF %(drc_index)08X``.

Inventory
~~~~~~~~~
Each device RP shall have an inventory of::

  total: 1
  reserved: 0
  min_unit: 1
  max_unit: 1
  step_size: 1
  allocation_ratio: 1.0

of the `resource_class`_ specified in the config file for the paragraph
matching this device (``CUSTOM_IOSLOT`` by default).

.. note:: **Future:** Some day we will provide SR-IOV VFs, vGPUs, FPGA
          regions/functions, etc. At that point we will conceivably have
          inventory of multiple units of multiple resource classes, etc.

Generated Traits
~~~~~~~~~~~~~~~~
The provider for a device shall be decorated with the following
automatically-generated traits:

* ``CUSTOM_POWERVM_IOSLOT_VENDOR_ID_%(vendor_id)04X``
* ``CUSTOM_POWERVM_IOSLOT_DEVICE_ID_%(device_id)04X``
* ``CUSTOM_POWERVM_IOSLOT_SUBSYS_VENDOR_ID_%(subsys_vendor_id)04X``
* ``CUSTOM_POWERVM_IOSLOT_SUBSYS_DEVICE_ID_%(subsys_device_id)04X``
* ``CUSTOM_POWERVM_IOSLOT_CLASS_%(class)04X``
* ``CUSTOM_POWERVM_IOSLOT_REVISION_ID_%(revision_id)02X``
* ``CUSTOM_POWERVM_IOSLOT_DRC_INDEX_%(drc_index)08X``
* ``CUSTOM_POWERVM_IOSLOT_DRC_NAME_%(drc_name)s`` where ``drc_name`` is
  normalized via ``os_traits.normalize_name``.

In addition, the driver shall decorate the provider with any `traits`_
specified in the config file paragraph identifying this device. If that
paragraph specifies any of the above generated traits, an exception shall be
raised (we'll blow up the compute service).

update_provider_tree
~~~~~~~~~~~~~~~~~~~~
The above provider tree structure/data shall be provided to Nova by overriding
the ``ComputeDriver.update_provider_tree`` method. The algorithm shall be as
follows:

* Parse the config file.
* Discover devices (``GET /ManagedSystem``, pull out
  ``.asio_config.io_slots``).
* Merge the config data with the discovered devices to produce a list of
  devices to pass through, along with inventory of the appropriate resource
  class name, and traits (generated and specified).
* Ensure the tree contains entries according to this calculated passthrough
  list, with appropriate inventory and traits.
* Set-subtract the names of the providers in the calculated passthrough list
  from those in the provider tree whose names are prefixed with ``PowerVM
  IOSlot`` and delete the resulting "orphans".

This is in addition to the standard ``update_provider_tree`` contract of
ensuring appropriate ``VCPU``, ``MEMORY_MB``, and ``DISK_GB`` resources on the
compute node provider.

.. note:: It is a stretch goal of this blueprint to implement caching and/or
          other enhancements to the above algorithm to optimize performance by
          minimizing the need to call PowerVM REST and/or process whitelist
          files every time.

Flavor Support
--------------
Existing Nova support for generic resource specification via flavor extra specs
should "just work". For example, a flavor requesting two GPUs might look like::

  resources:VCPU=1
  resources:MEMORY_MB=2048
  resources:DISK_GB=100
  resources1:CUSTOM_GPU=1
  traits1:CUSTOM_POWERVM_IOSLOT_VENDOR_ID_G00D=required
  traits1:CUSTOM_POWERVM_IOSLOT_PRODUCT_ID_F00D=required
  resources2:CUSTOM_GPU=1
  traits2:CUSTOM_POWERVM_IOSLOT_DRC_INDEX_1C0FFEE1=required

PowerVMDriver
-------------

spawn
~~~~~
During ``spawn``, we will query placement to retrieve the resource provider
records listed in the ``allocations`` parameter. Any provider names which are
prefixed with ``PowerVM IOSlot`` will be parsed to extract the DRC index (the
last eight characters of the provider name). The corresponding slots will be
extracted from the ``ManagedSystem`` payload and added to the
``LogicalPartition`` payload for the instance as it is being created.

destroy
~~~~~~~
IOSlots are detached automatically when we ``DELETE`` the ``LogicalPartition``,
so no changes should be required here.

Live Migration
~~~~~~~~~~~~~~
Since we can't migrate the state of an active GPU, we will block live migration
of a VM with an attached IOSlot.

.. _`Cold Migration`:

Cold Migration, Rebuild, Remote Restart
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
We should get these for free, but need to make sure they're tested.

Hot plug/unplug
~~~~~~~~~~~~~~~
This is not in the scope of the current effort. For now, attaching/detaching
devices to/from existing VMs can only be accomplished via resize (`Cold
Migration`_).

Alternatives
------------
Use Nova's PCI passthrough subsystem. We've all agreed this sucks and is not
the way forward.

Use oslo.config instead of a YAML file. Experience with the
``[pci]passthrough_whitelist`` has led us to conclude that config format is too
restrictive/awkward. The direction for Nova (as discussed in the Queens PTG in
Denver) will be toward some kind of YAML format; we're going to be the pioneers
on this front.

Security impact
---------------
It is the operator's responsibility to ensure that the passthrough YAML config
file has appropriate permissions, and lists only devices which do not
themselves pose a security risk if attached to a malicious VM.

End user impact
---------------
Users get acceleration for their workloads \o/

Performance Impact
------------------

Discovery
~~~~~~~~~
For the `update_provider_tree`_ flow, we're adding the step of loading and
parsing the passthrough YAML config file. This should be negligible compared to
e.g.  retrieving the ``ManagedSystem`` object (which we're already doing, so no
impact there).

spawn/destroy
~~~~~~~~~~~~~
There's no impact from the community side. It may take longer to create or
destroy a LogicalPartition with attached IOSlots.

Deployer impact
---------------
None.

Developer impact
----------------
None.

Upgrade impact
--------------
None.

Implementation
==============

Assignee(s)
-----------
Primary assignee:
  efried

Other contributors:
  edmondsw, mdrabe

Work Items
----------
See `Proposed change`_.


Dependencies
============
os-traits 0.9.0 to pick up the ``normalize_name`` method.

Testing
=======
Testing this in the CI will be challenging, given that we are not likely to
score GPUs for all of our nodes.

We will likely need to rely on manual testing and PowerVC to cover the code
paths described under `PowerVMDriver`_ with a handful of various device
configurations.


Documentation Impact
====================
* Add a section to our support matrix for generic device passthrough.
* User documentation for:
  * How to build the passthrough YAML file.
  * How to construct flavors accordingly.

References
==========
None.


History
=======

.. list-table:: Revisions
   :header-rows: 1

   * - Release Name
     - Description
   * - Rocky
     - Introduced
