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

from nova.i18n import _LI, _LW

from oslo_log import log as logging
from taskflow import task
from taskflow.types import failure as task_fail

from nova_powervm.virt.powervm.disk import driver as disk_dvr
from nova_powervm.virt.powervm import media
from nova_powervm.virt.powervm import vios

LOG = logging.getLogger(__name__)


class ConnectVolume(task.Task):
    """The task to connect a volume to an instance."""

    def __init__(self, adapter, vol_drv, instance, connection_info, host_uuid):
        """Create the task.

        Requires LPAR info through requirement of lpar_wrap.

        :param adapter: The pypowervm adapter.
        :param vol_drv: The volume driver (see volume folder).  Ties the
                        storage to a connection type (ex. vSCSI or NPIV).
        :param instance: The nova instance.
        :param connection_info: The connection info from the block device
                                mapping.
        :param host_uuid: The pypowervm UUID of the host.
        """
        self.adapter = adapter
        self.vol_drv = vol_drv
        self.instance = instance
        self.connection_info = connection_info
        self.vol_id = self.connection_info['data']['volume_id']
        self.host_uuid = host_uuid

        super(ConnectVolume, self).__init__(name='connect_vol_%s' %
                                            self.vol_id,
                                            requires=['lpar_wrap'])

    def execute(self, lpar_wrap):
        LOG.info(_LI('Connecting volume %(vol)s to instance %(inst)s') %
                 {'vol': self.vol_id, 'inst': self.instance.name})
        return self.vol_drv.connect_volume(self.adapter, self.host_uuid,
                                           lpar_wrap.uuid, self.instance,
                                           self.connection_info)

    def revert(self, lpar_wrap, result, flow_failures):
        # The parameters have to match the execute method, plus the response +
        # failures even if only a subset are used.
        if result is None or isinstance(result, task_fail.Failure):
            # No result means no disk to clean up.
            return

        LOG.warn(_LW('Volume %(vol)s for instance %(inst)s to be '
                     'disconnected') %
                 {'vol': self.vol_id, 'inst': self.instance.name})

        return self.vol_drv.disconnect_volume(self.adapter, self.host_uuid,
                                              lpar_wrap.uuid, self.instance,
                                              self.connection_info)


class DisconnectVolume(task.Task):
    """The task to disconnect a volume from an instance."""

    def __init__(self, adapter, vol_drv, instance, connection_info,
                 host_uuid, vm_uuid):
        """Create the task.

        Requires LPAR info through requirement of lpar_wrap.

        :param adapter: The pypowervm adapter.
        :param vol_drv: The volume driver (see volume folder).  Ties the
                        storage to a connection type (ex. vSCSI or NPIV).
        :param instance: The nova instance.
        :param connection_info: The connection info from the block device
                                mapping.
        :param host_uuid: The pypowervm UUID of the host.
        :param vm_uuid: The pypowervm UUID of the VM.
        """
        self.adapter = adapter
        self.vol_drv = vol_drv
        self.instance = instance
        self.connection_info = connection_info
        self.vol_id = self.connection_info['data']['volume_id']
        self.host_uuid = host_uuid
        self.vm_uuid = vm_uuid

        super(DisconnectVolume, self).__init__(name='disconnect_vol_%s' %
                                               self.vol_id)

    def execute(self):
        LOG.info(_LI('Disconnecting volume %(vol)s from instance %(inst)s') %
                 {'vol': self.vol_id, 'inst': self.instance.name})
        return self.vol_drv.disconnect_volume(self.adapter, self.host_uuid,
                                              self.vm_uuid, self.instance,
                                              self.connection_info)

    def revert(self, result, flow_failures):
        # The parameters have to match the execute method, plus the response +
        # failures even if only a subset are used.
        if result is None or isinstance(result, task_fail.Failure):
            # No result means no disk to clean up.
            return

        LOG.warn(_LW('Volume %(vol)s for instance %(inst)s to be '
                     're-connected') %
                 {'vol': self.vol_id, 'inst': self.instance.name})
        return self.vol_drv.connect_volume(self.adapter, self.host_uuid,
                                           self.vm_uuid, self.instance,
                                           self.connection_info)


class CreateDiskForImg(task.Task):
    """The Task to create the disk from an image in the storage."""

    def __init__(self, disk_dvr, context, instance, image_meta,
                 disk_size=0, image_type=disk_dvr.BOOT_DISK):
        """Create the Task.

        Provides the 'disk_dev_info' for other tasks.  Comes from the disk_dvr
        create_disk_from_image method.

        :param disk_dvr: The storage driver.
        :param context: The context passed into the driver method.
        :param instance: The nova instance.
        :param image_meta: The image metadata.
        :param disk_size: The size of disk to create. If the size is smaller
                          than the image, the image size will be used.
        :param image_type: The image type. See disk/driver.py
        """
        super(CreateDiskForImg, self).__init__(name='crt_disk_from_img',
                                               provides='disk_dev_info')
        self.disk_dvr = disk_dvr
        self.context = context
        self.instance = instance
        self.image_meta = image_meta
        self.disk_size = disk_size
        self.image_type = image_type

    def execute(self):
        LOG.info(_LI('Creating disk for instance: %s') % self.instance.name)
        return self.disk_dvr.create_disk_from_image(
            self.context, self.instance, self.image_meta, self.disk_size,
            image_type=self.image_type)

    def revert(self, result, flow_failures):
        # The parameters have to match the execute method, plus the response +
        # failures even if only a subset are used.
        LOG.warn(_LW('Image for instance %s to be deleted') %
                 self.instance.name)
        if result is None or isinstance(result, task_fail.Failure):
            # No result means no disk to clean up.
            return
        # TODO(thorst) no mappings at this point...how to delete.
        # disk_dvr.delete_disks(context, instance, result)


class ConnectDisk(task.Task):
    """The task to connect the disk to the instance."""

    def __init__(self, disk_dvr, context, instance):
        """Create the Task for the connect disk to instance method.

        Requires LPAR info through requirement of lpar_wrap.

        Requires disk info through requirement of disk_dev_info (provided by
        crt_disk_from_img)

        :param disk_dvr: The disk driver.
        :param context: The context passed into the spawn method.
        :param instance: The nova instance.
        """
        super(ConnectDisk, self).__init__(name='connect_disk',
                                          requires=['lpar_wrap',
                                                    'disk_dev_info'])
        self.disk_dvr = disk_dvr
        self.context = context
        self.instance = instance

    def execute(self, lpar_wrap, disk_dev_info):
        LOG.info(_LI('Connecting disk to instance: %s') %
                 self.instance.name)
        self.disk_dvr.connect_disk(self.context, self.instance, disk_dev_info,
                                   lpar_wrap.uuid)


class CreateAndConnectCfgDrive(task.Task):
    """The task to create the configuration drive."""

    def __init__(self, adapter, host_uuid, instance, injected_files,
                 network_info, admin_pass):
        """Create the Task that create and connect the config drive.

        Requires the 'lpar_wrap'.
        Provides the 'cfg_drv_vscsi_map' which is an element to later map
        the vscsi drive.

        :param adapter: The adapter for the pypowervm API
        :param host_uuid: The host UUID of the system.
        :param instance: The nova instance
        :param injected_files: A list of file paths that will be injected into
                               the ISO.
        :param network_info: The network_info from the nova spawn method.
        :param admin_pass: Optional password to inject for the VM.
        """
        super(CreateAndConnectCfgDrive, self).__init__(name='cfg_drive',
                                                       requires=['lpar_wrap'])
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.instance = instance
        self.injected_files = injected_files
        self.network_info = network_info
        self.ad_pass = admin_pass
        self.mb = None

    def execute(self, lpar_wrap):
        LOG.info(_LI('Creating Config Drive for instance: %s') %
                 self.instance.name)
        self.mb = media.ConfigDrivePowerVM(self.adapter, self.host_uuid)
        vscsi_map = self.mb.create_cfg_drv_vopt(self.instance,
                                                self.injected_files,
                                                self.network_info,
                                                lpar_wrap.uuid,
                                                admin_pass=self.ad_pass)

        LOG.info(_LI('Connecting Config Drive to instance: %s') %
                 self.instance.name)
        vios.add_vscsi_mapping(self.adapter, self.mb.vios_uuid,
                               self.mb.vios_name, vscsi_map)

    def revert(self, lpar_wrap, result, flow_failures):
        # The parameters have to match the execute method, plus the response +
        # failures even if only a subset are used.

        # No media builder, nothing to do
        if self.mb is None:
            return

        # TODO(IBM) Call into pypowervm to remove the scsi map

        # Delete the virtual optical media
        self.mb.dlt_vopt(lpar_wrap.uuid)


class DeleteVOpt(task.Task):
    """The task to delete the virtual optical."""

    def __init__(self, adapter, host_uuid, instance, lpar_uuid):
        """Creates the Task to delete the instances virtual optical media.

        :param adapter: The adapter for the pypowervm API
        :param host_uuid: The host UUID of the system.
        :param instance: The nova instance.
        :param lpar_uuid: The UUID of the lpar that has media.
        """
        super(DeleteVOpt, self).__init__(name='vopt_delete')
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.instance = instance
        self.lpar_uuid = lpar_uuid

    def execute(self):
        LOG.info(_LI('Deleting Virtual Optical Media for instance %s')
                 % self.instance.name)
        media_builder = media.ConfigDrivePowerVM(self.adapter, self.host_uuid)
        media_builder.dlt_vopt(self.lpar_uuid)


class DetachDisk(task.Task):
    """The task to detach the disk storage from the instance."""

    def __init__(self, disk_dvr, context, instance, lpar_uuid,
                 disk_type=None):
        """Creates the Task to detach the storage adapters.

        Provides the stor_adpt_mappings.  A list of pypowervm
        VSCSIMappings or VFCMappings (depending on the storage adapter).

        :param disk_dvr: The DiskAdapter for the VM.
        :param context: The nova context.
        :param instance: The nova instance.
        :param lpar_uuid: The UUID of the lpar.
        :param disk_type: List of disk types to detach. None means deatch all.
        """
        super(DetachDisk, self).__init__(name='detach_storage',
                                         provides='stor_adpt_mappings')
        self.disk_dvr = disk_dvr
        self.context = context
        self.instance = instance
        self.lpar_uuid = lpar_uuid
        self.disk_type = disk_type

    def execute(self):
        LOG.info(_LI('Detaching disk storage adapters for instance %s')
                 % self.instance.name)
        return self.disk_dvr.disconnect_image_disk(self.context, self.instance,
                                                   self.lpar_uuid,
                                                   disk_type=self.disk_type)


class DeleteDisk(task.Task):
    """The task to delete the backing storage."""

    def __init__(self, disk_dvr, context, instance):
        """Creates the Task to delete the disk storage from the system.

        Requires the stor_adpt_mappings.

        :param disk_dvr: The DiskAdapter for the VM.
        :param context: The nova context.
        :param instance: The nova instance.
        """
        req = ['stor_adpt_mappings']
        super(DeleteDisk, self).__init__(name='dlt_storage', requires=req)
        self.disk_dvr = disk_dvr
        self.context = context
        self.instance = instance

    def execute(self, stor_adpt_mappings):
        LOG.info(_LI('Deleting storage disk for instance %s.') %
                 self.instance.name)
        self.disk_dvr.delete_disks(self.context, self.instance,
                                   stor_adpt_mappings)
