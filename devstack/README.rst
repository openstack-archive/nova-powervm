========================
Installing with Devstack
========================

1. Download DevStack::

    $ git clone https://git.openstack.org/openstack-dev/devstack /opt/stack/devstack

2. Modify DevStack's local.conf to pull in this project by adding::

    [[local|localrc]]
    enable_plugin nova-powervm http://git.openstack.org/openstack/nova-powervm

   Example files are available in the nova-powervm project to provide
   reference on using this driver with the corresponding networking-powervm
   and ceilometer-powervm drivers. Following these example files will enable
   the appropriate drivers and services for each node type. Example config
   files for all-in-one, compute, and control nodes `can be found here. <https://github.com/openstack/nova-powervm/tree/master/devstack>`_

3. See nova-powervm/doc/source/devref/usage.rst, review the configuration options,
   then configure the installation in local.conf as needed for your environment.
   Besides DISK_DRIVER, the default config will be sufficient for most installs. ::

    [[local|localrc]]
    ...
    DISK_DRIVER =
    VOL_GRP_NAME =
    CLUSTER_NAME =

    [[post-config|$NOVA_CONF]]
    [powervm]
    ...

4. Run ``stack.sh`` from devstack::

    $ cd /opt/stack/devstack
    $ ./stack.sh
