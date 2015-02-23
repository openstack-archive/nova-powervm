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

from nova.i18n import _LI
from nova.i18n import _LW
from pypowervm.jobs import power
from pypowervm.wrappers import logical_partition as pvm_lpar

from oslo_log import log as logging
from taskflow import task
from taskflow.types import failure as task_fail

from nova_powervm.virt.powervm import media
from nova_powervm.virt.powervm import vios
from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)


class CreateVM(task.Task):
    """The task for the Create VM step of the spawn."""

    def __init__(self, adapter, host_uuid, instance, flavor):
        """Creates the Task for the Create VM step of the spawn.

        Provides the 'lpar_crt_response' for other tasks.

        :param adapter: The adapter for the pypowervm API
        :param host_uuid: The host UUID
        :param instance: The nova instance.
        :param flavor: The nova flavor.
        """
        super(CreateVM, self).__init__(name='crt_lpar',
                                       provides='lpar_crt_resp')
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.instance = instance
        self.flavor = flavor

    def execute(self):
        LOG.info(_LI('Creating instance: %s') % self.instance.name)
        resp = vm.crt_lpar(self.adapter, self.host_uuid, self.instance,
                           self.flavor)
        return resp

    def revert(self, result, flow_failures):
        # The parameters have to match the execute method, plus the response +
        # failures even if only a subset are used.
        LOG.warn(_LW('Instance %s to be undefined off host') %
                 self.instance.name)

        if isinstance(result, task_fail.Failure):
            # No response, nothing to do
            LOG.info(_LI('Create failed.  No delete of LPAR needed for '
                         'instance %s') % self.instance.name)
            return

        if result is None or result.entry is None:
            # No response, nothing to do
            LOG.info(_LI('Instance %s not found on host.  No update needed.') %
                     self.instance.name)
            return

        # Wrap to the actual delete.
        lpar = pvm_lpar.LogicalPartition(result.entry)
        vm.dlt_lpar(self.adapter, lpar.uuid)
        LOG.info(_LI('Instance %s removed from system') % self.instance.name)


class CreateVolumeForImg(task.Task):
    """The Task to create the volume from an Image in the storage."""

    def __init__(self, block_dvr, context, instance, image_meta,
                 block_device_info, flavor):
        """Create the Task.

        Provides the 'vol_dev_info' for other tasks.  Comes from the block_dvr
        create_volume_from_image method.

        :param block_dvr: The storage driver.
        :param context: The context passed into the spawn method.
        :param instance: The nova instance.
        :param image_meta: The image metadata.
        :param block_device_info: Information about block devices to be
                                  attached to the instance.
        :param flavor: The flavor for the instance to be spawned.
        """
        super(CreateVolumeForImg, self).__init__(name='crt_vol_from_img',
                                                 provides='vol_dev_info')
        self.block_dvr = block_dvr
        self.context = context
        self.instance = instance
        self.image_meta = image_meta
        self.block_device_info = block_device_info
        self.flavor = flavor

    def execute(self):
        LOG.info(_LI('Creating disk for instance: %s') % self.instance.name)
        return self.block_dvr.create_volume_from_image(self.context,
                                                       self.instance,
                                                       self.image_meta,
                                                       self.flavor.root_gb)

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

        Requires LPAR info through requirement of lpar_crt_resp (provided by
        tf_crt_lpar).

        Requires volume info through requirement of vol_dev_info (provided by
        tf_crt_vol_from_img)

        :param block_dvr: The storage driver.
        :param context: The context passed into the spawn method.
        :param instance: The nova instance.
        """
        super(ConnectVol, self).__init__(name='connect_vol',
                                         requires=['lpar_crt_resp',
                                                   'vol_dev_info'])
        self.block_dvr = block_dvr
        self.context = context
        self.instance = instance

    def execute(self, lpar_crt_resp, vol_dev_info):
        LOG.info(_LI('Connecting boot disk to instance: %s') %
                 self.instance.name)
        lpar = pvm_lpar.LogicalPartition(lpar_crt_resp.entry)
        self.block_dvr.connect_volume(self.context, self.instance,
                                      vol_dev_info, lpar.uuid)


class CreateCfgDrive(task.Task):
    """The task to create the configuration drive."""

    def __init__(self, adapter, host_uuid, vios_uuid, instance, injected_files,
                 network_info, admin_pass):
        """Create the Task that creates the config drive.

        Requires the 'lpar_crt_resp'.
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
                                             requires=['lpar_crt_resp'],
                                             provides='cfg_drv_vscsi_map')
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.vios_uuid = vios_uuid
        self.instance = instance
        self.injected_files = injected_files
        self.network_info = network_info
        self.ad_pass = admin_pass

    def execute(self, lpar_crt_resp):
        LOG.info(_LI('Creating Config Drive for instance: %s') %
                 self.instance.name)
        lpar = pvm_lpar.LogicalPartition(lpar_crt_resp.entry)
        media_builder = media.ConfigDrivePowerVM(self.adapter, self.host_uuid,
                                                 self.vios_uuid)
        vscsi_map = media_builder.create_cfg_drv_vopt(self.instance,
                                                      self.injected_files,
                                                      self.network_info,
                                                      lpar.uuid,
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
                               cfg_drv_vscsi_map._element)


class PowerOnVM(task.Task):
    """The task to power on the instance."""

    def __init__(self, adapter, host_uuid, instance):
        """Create the Task for the power on of the LPAR.

        Obtains LPAR info through requirement of lpar_crt_resp (provided by
        tf_crt_lpar).

        :param adapter: The pypowervm adapter.
        :param host_uuid: The host UUID.
        :param instance: The nova instance.
        """
        super(PowerOnVM, self).__init__(name='pwr_lpar',
                                        requires=['lpar_crt_resp'])
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.instance = instance

    def execute(self, lpar_crt_resp):
        LOG.info(_LI('Powering on instance: %s') % self.instance.name)
        power.power_on(self.adapter,
                       pvm_lpar.LogicalPartition(lpar_crt_resp.entry),
                       self.host_uuid)

    def revert(self, lpar_crt_resp, result, flow_failures):
        LOG.info(_LI('Powering off instance: %s') % self.instance.name)

        if isinstance(result, task_fail.Failure):
            # The power on itself failed...can't power off.
            LOG.debug('Power on failed.  Not performing power off.')
            return

        power.power_off(self.adapter,
                        pvm_lpar.LogicalPartition(lpar_crt_resp.entry),
                        self.host_uuid,
                        force_immediate=True)
