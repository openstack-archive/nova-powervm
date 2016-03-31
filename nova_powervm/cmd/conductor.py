# Copyright 2016 IBM Corp.
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


"""Starter script for PowerVM Nova Conductor service."""

from nova.cmd import conductor

from nova_powervm import objects


def main():
    # The only reason we need this module is to ensure the PowerVM version of
    # the migrate data object is registered.  See:
    # http://eavesdrop.openstack.org/irclogs/%23openstack-nova/
    #     %23openstack-nova.2016-02-24.log.html
    objects.register_all()
    conductor.main()
