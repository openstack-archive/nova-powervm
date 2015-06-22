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

"""Exceptions specific to the powervm nova driver."""

import abc
from nova import exception as nex
from nova.i18n import _

import six


@six.add_metaclass(abc.ABCMeta)
class AbstractMediaException(nex.NovaException):
    pass


@six.add_metaclass(abc.ABCMeta)
class AbstractDiskException(nex.NovaException):
    pass


class NoMediaRepoVolumeGroupFound(AbstractMediaException):
    msg_fmt = _("Unable to locate the volume group %(vol_grp)s to store the "
                "virtual optical media within.  Unable to create the "
                "media repository.")


class ManagementPartitionNotFoundException(nex.NovaException):
    """Couldn't find exactly one management partition on the system."""
    msg_fmt = _("Expected to find exactly one management partition; found "
                "%(count)d.")


class NoDiskDiscoveryException(nex.NovaException):
    """Failed to discover any disk."""
    msg_fmt = _("Having scanned SCSI bus %(bus)x on the management partition, "
                "disk with UDID %(udid)s failed to appear after %(polls)d "
                "polls over %(timeout)d seconds.")


class UniqueDiskDiscoveryException(nex.NovaException):
    """Expected to discover exactly one disk, but discovered >1."""
    msg_fmt = _("Expected to find exactly one disk on the management "
                "partition at %(path_pattern)s; found %(count)d.")


class DeviceDeletionException(nex.NovaException):
    """Expected to delete a disk, but the disk is still present afterward."""
    msg_fmt = _("Device %(devpath)s is still present on the management "
                "partition after attempting to delete it.  Polled %(polls)d "
                "times over %(timeout)d seconds.")


class InstanceDiskMappingFailed(AbstractDiskException):
    msg_fmt = _("Failed to map boot disk of instance %(instance_name)s to "
                "the management partition from any Virtual I/O Server.")


class VGNotFound(AbstractDiskException):
    msg_fmt = _("Unable to locate the volume group '%(vg_name)s' for this "
                "operation.")


class ClusterNotFoundByName(AbstractDiskException):
    msg_fmt = _("Unable to locate the Cluster '%(clust_name)s' for this "
                "operation.")


class NoConfigNoClusterFound(AbstractDiskException):
    msg_fmt = _('Unable to locate any Cluster for this operation.')


class TooManyClustersFound(AbstractDiskException):
    msg_fmt = _("Unexpectedly found %(clust_count)d Clusters "
                "matching name '%(clust_name)s'.")


class NoConfigTooManyClusters(AbstractDiskException):
    msg_fmt = _("No cluster_name specified.  Refusing to select one of the "
                "%(clust_count)d Clusters found.")
