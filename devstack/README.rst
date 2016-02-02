========================
Installing with DevStack
========================

What is DevStack?
--------------------------

DevStack is a script to quickly create an OpenStack development environment.

Find out more `here <http://docs.openstack.org/developer/devstack/>`_.


What are DevStack plugins?
--------------------------

DevStack plugins act as project-specific extensions of DevStack. They allow external projects to
execute code directly in the DevStack run, supporting configuration and installation changes as
part of the normal local.conf and stack.sh execution. For NovaLink, we have DevStack plugins for
each of our three projects - nova-powervm, networking-powervm, and ceilometer-powervm. These
plugins, with the appropriate local.conf settings for your environment, will allow you to simply
clone down DevStack, configure, run stack.sh, and end up with a working OpenStack/Novalink PowerVM
environment with no other scripting required.

More details can be `found here. <http://docs.openstack.org/developer/devstack/plugins.html>`_


How to use the NovaLink DevStack plugins:
-----------------------------------------

1. Download DevStack::

    $ git clone https://git.openstack.org/openstack-dev/devstack /opt/stack/devstack

2. Set up your local.conf file to pull in our projects:
    1. If you have an existing DevStack local.conf, modify it to pull in this project by adding::

        [[local|localrc]]
        enable_plugin nova-powervm http://git.openstack.org/openstack/nova-powervm

     and following the instructions for networking-powervm and ceilometer-powervm
     as needed for your environment.

    2. If you're setting up DevStack for the first time, example files are available
       in the nova-powervm project to provide reference on using this driver with the
       corresponding networking-powervm and ceilometer-powervm drivers. Following these
       example files will enable the appropriate drivers and services for each node type.
       Example config files for all-in-one, compute, and control nodes
       `can be found here. <https://github.com/openstack/nova-powervm/tree/master/devstack>`_

       The nova-powervm project provides three different sample local.conf files as a
       starting point for devstack.

       * local.conf.aio

          * Runs on the NovaLink VM of the PowerVM system
          * Provides a full 'all in one' devstack VM

       * local.conf.control

          * Can run on any devstack capable machine (POWER or x86)
          * Provides the controller node for devstack.  Typically paired with the local.conf.compute

       * local.conf.compute

          * Runs on the NovaLink VM of the PowerVM system
          * Provides the compute node for a devstack.  Typically paired with the local.conf.control

3. See our devrefs and plugin references for the configuration options for each driver,
   then configure the installation in local.conf as needed for your environment.

    * nova-powervm
        * http://nova-powervm.readthedocs.org/en/latest/devref/index.html
        * https://github.com/openstack/nova-powervm/blob/master/devstack/README.rst

    * networking-powervm
        * http://docs.openstack.org/developer/networking-powervm/devref/index.html
        * https://github.com/openstack/networking-powervm/blob/master/devstack/README.rst

    * ceilometer-powervm
        * http://ceilometer-powervm.readthedocs.org/en/latest/devref/index.html
        * https://github.com/openstack/ceilometer-powervm/blob/master/devstack/README.rst

4. For nova-powervm, changing the DISK_DRIVER settings for your environment will be required.
   The default configuration for other settings will be sufficient for most installs. ::

        [[local|localrc]]
        ...
        DISK_DRIVER =
        VOL_GRP_NAME =
        CLUSTER_NAME =

        [[post-config|$NOVA_CONF]]
        [powervm]
        ...

5. A few notes:

   * By default this will pull in the latest/trunk versions of all the projects. If you want to
     run a stable version instead, you can either check out that stable branch in the DevStack
     repo (git checkout stable/liberty) which is the preferred method, or you can do it on a
     project by project basis in the local.conf file as needed.

   * If you need any special services enabled for your environment, you can also specify those
     in your local.conf file. In our example files we demonstrate enabling and disabling services
     (n-cpu, q-agt, etc) required for our drivers.

6. Run ``stack.sh`` from DevStack::

    $ cd /opt/stack/devstack
    $ FORCE=yes ./stack.sh

   ``FORCE=yes`` is needed on Ubuntu 15.10 since only Ubuntu LTS releases are officially supported
   by DevStack. If you're running a control only node on a different, supported OS version you can
   skip using ``FORCE=yes``.

7. At this point DevStack will run through stack.sh, and barring any DevStack issues, you should
   end up with a standard link to your Horizon portal at the end of the stack run. Congratulations!
