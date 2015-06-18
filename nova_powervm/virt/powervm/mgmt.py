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
import glob
from nova import exception
from nova.i18n import _LI
from nova.storage import linuxscsi
import os
from os import path

from oslo_log import log as logging

from pypowervm.wrappers import logical_partition as pvm_lpar

from nova_powervm.virt.powervm import exception as npvmex

LOG = logging.getLogger(__name__)


def get_mgmt_partition(adapter):
    """Get the LPAR wrapper for this host's management partition.

    :param adapter: The adapter for the pypowervm API.
    """
    wraps = pvm_lpar.LPAR.search(adapter, is_mgmt_partition=True)
    if len(wraps) != 1:
        raise npvmex.ManagementPartitionNotFoundException(count=len(wraps))
    return wraps[0]


def discover_vscsi_disk(mapping):
    """Bring a mapped device into the management partition and find its name.

    Based on a VSCSIMapping, scan the appropriate virtual SCSI host bus,
    causing the operating system to discover the mapped device.  Find and
    return the path of the newly-discovered device based on its UDID in the
    mapping.

    Note: scanning the bus will cause the operating system to discover *all*
    devices on that bus.  However, this method will only return the path for
    the specific device from the input mapping, based on its UDID.

    :param mapping: The pypowervm.wrappers.virtual_io_server.VSCSIMapping
                    representing the mapping of the desired disk to the
                    management partition.
    :return: The udev-generated ("/dev/sdX") name of the discovered disk.
    """
    # TODO(IBM): Support for other host platforms.

    # Calculate the Linux slot number from the client adapter slot number.
    lslot = 0x30000000 | mapping.client_adapter.slot_number
    # We'll match the device ID based on the UDID, which is actually the last
    # 32 chars of the field we get from PowerVM.
    udid = mapping.backing_storage.udid[-32:]

    LOG.info(_LI("Trying to discover VSCSI disk with UDID %(udid)s on slot "
                 "%(slot)x."), {'udid': udid, 'slot': lslot})

    # Find the special file to scan the bus, and scan it.
    # This glob should yield exactly one result, but use the loop just in case.
    for scanpath in glob.glob(
            '/sys/bus/vio/devices/%x/host*/scsi_host/host*/scan' % lslot):
        # echo '- - -' | sudo tee -a /path/to/scan
        linuxscsi.echo_scsi_command(scanpath, '- - -')

    # Now see if our device showed up.  If so, we can reliably match it based
    # on its Linux ID, which ends with the disk's UDID.
    dpathpat = '/dev/disk/by-id/*%s' % udid
    disks = glob.glob(dpathpat)
    if len(disks) != 1:
        raise npvmex.UniqueDiskDiscoveryException(path_pattern=dpathpat,
                                                  count=len(disks))

    # The by-id path is a symlink.  Resolve to the /dev/sdX path
    dpath = path.realpath(disks[0])
    LOG.info(_LI("Discovered VSCSI disk with UDID %(udid)s on slot %(slot)x "
                 "at path %(devname)s."),
             {'udid': udid, 'slot': lslot, 'devname': dpath})
    return dpath


def remove_block_dev(devpath):
    """Remove a block device from the management partition.

    This method causes the operating system of the management partition to
    delete the device special files associated with the specified block device.

    :param devpath: Any path to the block special file associated with the
                    device to be removed.
    :raise InvalidDevicePath: If the specified device or its 'delete' special
                              file cannot be found.
    :raise DeviceDeletionException: If the deletion was attempted, but the
                                    device special file is still present
                                    afterward.
    """
    # Resolve symlinks, if any, to get to the /dev/sdX path
    devpath = path.realpath(devpath)
    try:
        os.stat(devpath)
    except OSError:
        raise exception.InvalidDevicePath(path=devpath)
    devname = devpath.rsplit('/', 1)[-1]
    delpath = '/sys/block/%s/device/delete' % devname
    try:
        os.stat(delpath)
    except OSError:
        raise exception.InvalidDevicePath(path=delpath)
    LOG.debug("Deleting block device %(devpath)s from the management "
              "partition via special file %(delpath)s.",
              {'devpath': devpath, 'delpath': delpath})
    linuxscsi.echo_scsi_command(delpath, '1')
    try:
        os.stat(devpath)
        raise npvmex.DeviceDeletionException(devpath=devpath)
    except OSError:
        # Device special file is absent, as expected
        pass
