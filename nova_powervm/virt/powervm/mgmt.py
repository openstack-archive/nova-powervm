# Copyright 2015 IBM Corp.
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

"""Utilities related to the PowerVM management partition.

The management partition is a special LPAR that runs the PowerVM REST API
service.  It itself appears through the REST API as a LogicalPartition of type
aixlinux, but with the is_mgmt_partition property set to True.

The PowerVM Nova Compute service runs on the management partition.
"""
from nova.i18n import _
from pypowervm.wrappers import logical_partition as pvm_lpar


def get_mgmt_partition(adapter):
    """Get the LPAR wrapper for this host's management partition.

    :param adapter: The adapter for the pypowervm API.
    """
    wraps = pvm_lpar.LPAR.search(adapter, is_mgmt_partition=True)
    if len(wraps) != 1:
        raise Exception(_("Unable to find a single management partition."))
    return wraps[0]
