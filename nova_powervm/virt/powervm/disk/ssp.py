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

from oslo_config import cfg
import oslo_log.log as logging

from nova import image
from nova.i18n import _LI, _LE
import nova_powervm.virt.powervm.disk as disk
from nova_powervm.virt.powervm.disk import driver as disk_drv

import pypowervm.wrappers.storage as pvm_stg

ssp_opts = [
    cfg.StrOpt('ssp_name',
               default='',
               help='Shared Storage Pool to use for storage operations.  If '
                    'none specified, the host is queried; if a single '
                    'Shared Storage Pool is found, it is used.')
]


LOG = logging.getLogger(__name__)
CONF = cfg.CONF
CONF.register_opts(ssp_opts)


class SSPNotFoundByName(disk.AbstractDiskException):
    msg_fmt = _LE("Unable to locate the Shared Storage Pool '%(ssp_name)s' "
                  "for this operation.")


class NoConfigNoSSPFound(disk.AbstractDiskException):
    msg_fmt = _LE('Unable to locate any Shared Storage Pool for this '
                  'operation.')


class TooManySSPsFound(disk.AbstractDiskException):
    msg_fmt = _LE("Unexpectedly found %(ssp_count)d Shared Storage Pools "
                  "matching name '%(ssp_name)s'.")


class NoConfigTooManySSPs(disk.AbstractDiskException):
    msg_fmt = _LE("No ssp_name specified.  Refusing to select one of the "
                  "%(ssp_count)d Shared Storage Pools found.")


class SSPDiskAdapter(disk_drv.DiskAdapter):
    """Provides a disk adapter for Shared Storage Pools.

    Shared Storage Pools are a clustered file system technology that can link
    together Virtual I/O Servers.

    This adapter provides the connection for nova based storage (not Cinder)
    to connect to virtual machines.  A separate Cinder driver for SSPs may
    exist in the future.
    """

    def __init__(self, connection):
        """Initialize the SSPDiskAdapter.

        :param connection: connection information for the underlying driver
        """
        super(SSPDiskAdapter, self).__init__(connection)
        self.adapter = connection['adapter']
        self.host_uuid = connection['host_uuid']
        self.vios_name = connection['vios_name']
        self.vios_uuid = connection['vios_uuid']
        self.ssp_name = CONF.ssp_name
        # Make sure _fetch_ssp_wrap knows it has to bootstrap by name
        self._ssp_wrap = None
        self._fetch_ssp_wrap()  # Sets self._ssp_wrap
        self.image_api = image.API()
        LOG.info(_LI('SSP Storage driver initialized: '
                     'SSP: \'%s\'') % self.ssp_name)

    @property
    def capacity(self):
        """Capacity of the storage in gigabytes."""
        ssp = self._fetch_ssp_wrap()
        return float(ssp.capacity)

    @property
    def capacity_used(self):
        """Capacity of the storage in gigabytes that is used."""
        ssp = self._fetch_ssp_wrap()
        return float(ssp.capacity) - float(ssp.free_space)

    def disconnect_image_disk(self, context, instance, lpar_uuid,
                              disk_type=None):
        """Disconnects the storage adapters from the image disk.

        :param context: nova context for operation
        :param instance: instance to delete the image for.
        :param lpar_uuid: The UUID for the pypowervm LPAR element.
        :param disk_type: The list of disk types to remove or None which means
            to remove all disks from the VM.
        :return: A list of Mappings (either pypowervm VSCSIMappings or
                 VFCMappings)
        """
        raise NotImplementedError()

    def delete_disks(self, context, instance, mappings):
        """Removes the disks specified by the mappings.

        :param context: nova context for operation
        :param instance: instance to delete the image for.
        :param mappings: The mappings that had been used to identify the
                         backing storage.  List of pypowervm VSCSIMappings or
                         VFCMappings. Typically derived from
                         disconnect_image_disk.
        """
        raise NotImplementedError()

    def create_disk_from_image(self, context, instance, image, disk_size,
                               image_type=disk_drv.BOOT_DISK):
        """Creates a disk and copies the specified image to it.

        :param context: nova context used to retrieve image from glance
        :param instance: instance to create the disk for.
        :param image_id: image_id reference used to locate image in glance
        :param disk_size: The size of the disk to create in GB.  If smaller
                          than the image, it will be ignored (as the disk
                          must be at least as big as the image).  Must be an
                          int.
        :param image_type: the image type. See disk constants above.
        :returns: dictionary with the name of the created
                  disk device in 'device_name' key
        """
        raise NotImplementedError()

    def connect_disk(self, context, instance, disk_info, lpar_uuid, **kwds):
        raise NotImplementedError()

    def extend_disk(self, context, instance, disk_info, size):
        """Extends the disk.

        :param context: nova context for operation.
        :param instance: instance to extend the disk for.
        :param disk_info: dictionary with disk info.
        :param size: the new size in gb.
        """
        raise NotImplementedError()

    def _fetch_ssp_wrap(self):
        """Return the SSP EntryWrapper associated with the configured name.

        The SSP EntryWrapper is 'cached' locally.

        In a bootstrap scenario, the ssp_name from the config is used to
        perform a search query.

        Otherwise, the cached wrapper is refreshed.
        """
        try:
            if self._ssp_wrap is None:
                # Not yet loaded.
                # Did config provide a name?
                if self.ssp_name:
                    resp = pvm_stg.SSP.search(self.adapter,
                                              name=self.ssp_name)
                    wraps = pvm_stg.SSP.wrap(resp)
                    if len(wraps) == 0:
                        raise SSPNotFoundByName(ssp_name=self.ssp_name)
                    if len(wraps) > 1:
                        raise TooManySSPsFound(ssp_count=len(wraps),
                                               ssp_name=self.ssp_name)
                else:
                    # Otherwise, pull the entire feed of SSPs and, if exactly
                    # one result, use it.
                    resp = self.adapter.read(pvm_stg.SSP.schema_type)
                    wraps = pvm_stg.SSP.wrap(resp)
                    if len(wraps) == 0:
                        raise NoConfigNoSSPFound()
                    if len(wraps) > 1:
                        raise NoConfigTooManySSPs(ssp_count=len(wraps))
                self._ssp_wrap = wraps[0]
            else:
                # Already loaded.  Refresh.
                self._ssp_wrap = self._ssp_wrap.refresh(self.adapter)
        # TODO(IBM): If the SSP doesn't exist when the driver is loaded, we
        # raise one of the custom exceptions; but if it gets removed at some
        # point while live, we'll (re)raise the 404 HttpError from the REST
        # API.  Do we need a crisper way to distinguish these two scenarios?
        # Do we want to trap the 404 and raise a custom "SSPVanished"?
        except Exception as e:
            LOG.exception(e.message)
            raise e
        return self._ssp_wrap
