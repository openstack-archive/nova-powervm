..
      Copyright 2015 IBM
      All Rights Reserved.

      Licensed under the Apache License, Version 2.0 (the "License"); you may
      not use this file except in compliance with the License. You may obtain
      a copy of the License at

          http://www.apache.org/licenses/LICENSE-2.0

      Unless required by applicable law or agreed to in writing, software
      distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
      WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
      License for the specific language governing permissions and limitations
      under the License.

Setting Up a Development Environment
====================================

This page describes how to setup a working Python development
environment that can be used in developing Nova-PowerVM.

These instructions assume you're already familiar with
Git and Gerrit, which is a code repository mirror and code review toolset,
however if you aren't please see `this Git tutorial`_ for an introduction
to using Git and `this guide`_ for a tutorial on using Gerrit and Git for
code contribution to OpenStack projects.

.. _this Git tutorial: http://git-scm.com/book/en/Getting-Started
.. _this guide: http://docs.openstack.org/infra/manual/developers.html#development-workflow

Getting the code
----------------

Grab the code::

    git clone git://git.openstack.org/openstack/nova-powervm
    cd nova-powervm

Setting up your environment
---------------------------

The purpose of this project is to provide the 'glue' between OpenStack
Compute (Nova) and PowerVM.  The `pypowervm`_ project is used to control
PowerVM systems.

It is recommended that you clone down the OpenStack Nova project along with
pypowervm into your respective development environment.

Running the tox python targets for tests will automatically clone these down
via the requirements.

Additional project requirements may be found in the requirements.txt file.

.. _pypowervm: https://github.com/powervm/pypowervm
