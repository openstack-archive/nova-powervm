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

from nova_powervm.virt.powervm.disk import blockdev
from nova_powervm.virt.powervm import media
from nova_powervm.virt.powervm import vios

LOG = logging.getLogger(__name__)


class CreateVolumeForImg(task.Task):
    """The Task to create the volume from an Image in the storage."""

    def __init__(self, block_dvr, context, instance, image_meta,
                 disk_size=0, image_type=blockdev.BOOT_DISK):
        """Create the Task.

        Provides the 'vol_dev_info' for other tasks.  Comes from the block_dvr
        create_volume_from_image method.

        :param block_dvr: The storage driver.
        :param context: The context passed into the spawn method.
        :param instance: The nova instance.
        :param image_meta: The image metadata.
        :param disk_size: The size of volume to create. If the size is
            smaller than the image, the image size will be used.
        :param image_type: The image type. See disk/blockdev.py
        """
        super(CreateVolumeForImg, self).__init__(name='crt_vol_from_img',
                                                 provides='vol_dev_info')
        self.block_dvr = block_dvr
        self.context = context
        self.instance = instance
        self.image_meta = image_meta
        self.disk_size = disk_size
        self.image_type = image_type

    def execute(self):
        LOG.info(_LI('Creating disk for instance: %s') % self.instance.name)
        return self.block_dvr.create_volume_from_image(
            self.context, self.instance, self.image_meta, self.disk_size,
            image_type=self.image_type)

    def revert(self, result, flow_failures):
        # The parameters have to match the execute method, plus the response +
        # failures even if only a subset are used.
        LOG.warn(_LW('Image for instance %s to be deleted') %
                 self.instance.name)
        if result is None or isinstance(result, task_fail.Failure):
            # No result means no volume to clean up.
            return
        # TODO(thorst) no mappings at this point...how to delete.
        # block_dvr.delete_volumes(context, instance, result)


class ConnectVol(task.Task):
    """The task to connect the volume to the instance."""

    def __init__(self, block_dvr, context, instance):
        """Create the Task for the connect volume to instance method.

        Requires LPAR info through requirement of lpar_wrap.

        Requires volume info through requirement of vol_dev_info (provided by
        tf_crt_vol_from_img)

        :param block_dvr: The storage driver.
        :param context: The context passed into the spawn method.
        :param instance: The nova instance.
        """
        super(ConnectVol, self).__init__(name='connect_vol',
                                         requires=['lpar_wrap',
                                                   'vol_dev_info'])
        self.block_dvr = block_dvr
        self.context = context
        self.instance = instance

    def execute(self, lpar_wrap, vol_dev_info):
        LOG.info(_LI('Connecting disk to instance: %s') %
                 self.instance.name)
        self.block_dvr.connect_volume(self.context, self.instance,
                                      vol_dev_info, lpar_wrap.uuid)


class CreateCfgDrive(task.Task):
    """The task to create the configuration drive."""

    def __init__(self, adapter, host_uuid, vios_uuid, instance, injected_files,
                 network_info, admin_pass):
        """Create the Task that creates the config drive.

        Requires the 'lpar_wrap'.
        Provides the 'cfg_drv_vscsi_map' which is an element to later map
        the vscsi drive.

        :param adapter: The adapter for the pypowervm API
        :param host_uuid: The host UUID of the system.
        :param vios_uuid: The VIOS UUID the drive should be put on.
        :param instance: The nova instance
        :param injected_files: A list of file paths that will be injected into
                               the ISO.
        :param network_info: The network_info from the nova spawn method.
        :param admin_pass: Optional password to inject for the VM.
        """
        super(CreateCfgDrive, self).__init__(name='cfg_drive',
                                             requires=['lpar_wrap'],
                                             provides='cfg_drv_vscsi_map')
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.vios_uuid = vios_uuid
        self.instance = instance
        self.injected_files = injected_files
        self.network_info = network_info
        self.ad_pass = admin_pass

    def execute(self, lpar_wrap):
        LOG.info(_LI('Creating Config Drive for instance: %s') %
                 self.instance.name)
        media_builder = media.ConfigDrivePowerVM(self.adapter, self.host_uuid,
                                                 self.vios_uuid)
        vscsi_map = media_builder.create_cfg_drv_vopt(self.instance,
                                                      self.injected_files,
                                                      self.network_info,
                                                      lpar_wrap.uuid,
                                                      admin_pass=self.ad_pass)
        return vscsi_map


class ConnectCfgDrive(task.Task):
    """The task to connect the cfg drive to the instance."""

    def __init__(self, adapter, instance, vios_uuid, vios_name):
        """Create the Task that connects the cfg drive to the instance.

        Requires the 'cfg_drv_vscsi_map'.

        :param adapter: The adapter for the pypowervm API
        :param instance: The nova instance
        :param vios_uuid: The VIOS UUID the drive should be put on.
        :param vios_name: The name of the VIOS that this will be put on.
        """
        name = 'cfg_drive_scsi_connect'
        reqs = ['cfg_drv_vscsi_map']
        super(ConnectCfgDrive, self).__init__(name=name, requires=reqs)
        self.adapter = adapter
        self.instance = instance
        self.vios_uuid = vios_uuid
        self.vios_name = vios_name

    def execute(self, cfg_drv_vscsi_map):
        LOG.info(_LI('Attaching Config Drive to instance: %s') %
                 self.instance.name)
        vios.add_vscsi_mapping(self.adapter, self.vios_uuid, self.vios_name,
                               cfg_drv_vscsi_map.element)


class DeleteVOpt(task.Task):
    """The task to delete the virtual optical."""

    def __init__(self, adapter, host_uuid, vios_uuid, instance, lpar_uuid):
        """Creates the Task to delete the instances virtual optical media.

        :param adapter: The adapter for the pypowervm API
        :param host_uuid: The host UUID of the system.
        :param vios_uuid: The VIOS UUID the media is being deleted from.
        :param instance: The nova instance.
        :param lpar_uuid: The UUID of the lpar that has media.
        """
        super(DeleteVOpt, self).__init__(name='vopt_delete')
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.vios_uuid = vios_uuid
        self.instance = instance
        self.lpar_uuid = lpar_uuid

    def execute(self):
        LOG.info(_LI('Deleting Virtual Optical Media for instance %s')
                 % self.instance.name)
        media_builder = media.ConfigDrivePowerVM(self.adapter, self.host_uuid,
                                                 self.vios_uuid)
        media_builder.dlt_vopt(self.lpar_uuid)


class Detach(task.Task):
    """The task to detach the storage from the instance."""

    def __init__(self, block_dvr, context, instance, lpar_uuid,
                 disk_type=None):
        """Creates the Task to detach the storage adapters.

        Provides the stor_adpt_mappings.  A list of pypowervm
        VSCSIMappings or VFCMappings (depending on the storage adapter).

        :param block_dvr: The StorageAdapter for the VM.
        :param context: The nova context.
        :param instance: The nova instance.
        :param lpar_uuid: The UUID of the lpar.
        :param disk_type: List of disk types to detach. None means deatch all.
        """
        super(Detach, self).__init__(name='detach_storage',
                                     provides='stor_adpt_mappings')
        self.block_dvr = block_dvr
        self.context = context
        self.instance = instance
        self.lpar_uuid = lpar_uuid
        self.disk_type = disk_type

    def execute(self):
        LOG.info(_LI('Detaching disk storage adapters for instance %s')
                 % self.instance.name)
        return self.block_dvr.disconnect_volume(self.context, self.instance,
                                                self.lpar_uuid,
                                                disk_type=self.disk_type)


class Delete(task.Task):
    """The task to delete the backing storage."""

    def __init__(self, block_dvr, context, instance):
        """Creates the Task to delete the storage from the system.

        Requires the stor_adpt_mappings.

        :param block_dvr: The StorageAdapter for the VM.
        :param context: The nova context.
        :param instance: The nova instance.
        """
        req = ['stor_adpt_mappings']
        super(Delete, self).__init__(name='dlt_storage', requires=req)
        self.block_dvr = block_dvr
        self.context = context
        self.instance = instance

    def execute(self, stor_adpt_mappings):
        LOG.info(_LI('Deleting storage for instance %s.') % self.instance.name)
        self.block_dvr.delete_volumes(self.context, self.instance,
                                      stor_adpt_mappings)
