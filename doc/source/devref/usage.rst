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

Usage
=====
- Configure the PowerVM system for `NovaLink`_
- Install the ceilometer-powervm plugin on the `NovaLink`_ VM on the PowerVM
  Server.
- Set the hypervisor_inspector in the ceilometer.conf to "powervm"
- Start the ceilometer-agent-compute on the compute server

.. _NovaLink: http://www-01.ibm.com/common/ssi/cgi-bin/ssialias?infotype=AN&subtype=CA&htmlfid=897/ENUS215-262&appname=USN