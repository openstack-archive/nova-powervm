========================
Installing with Devstack
========================

1. Download DevStack::

    $ git clone https://git.openstack.org/openstack-dev/devstack /opt/stack/devstack

2. Modify DevStack's local.conf to pull in this project by adding::

    [[local|localrc]]
    enable_plugin nova-powervm http://git.openstack.org/openstack/nova-powervm

2a. See the file "local.conf.example" in nova-powervm/devstack for reference
    on using this driver with the corresponding networking-powervm and
    ceilometer-powervm drivers. Following the example file will enable all
    three plugins, resulting an all-in-one powervm devstack node.

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
