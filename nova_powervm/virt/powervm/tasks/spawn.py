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
from nova.openstack.common import log as logging
from pypowervm.jobs import power
from pypowervm.wrappers import logical_partition as pvm_lpar

from taskflow import task
from taskflow.types import failure as task_fail

from nova_powervm.virt.powervm import media
from nova_powervm.virt.powervm import vios
from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)


def tf_crt_lpar(adapter, host_uuid, instance, flavor):
    """Creates the Task for the crt_lpar step of the spawn.

    Provides the 'lpar_crt_response' for other tasks.

    :param adapter: The adapter for the pypowervm API
    :param host_uuid: The host UUID
    :param instance: The nova instance.
    :param flavor: The nova flavor.
    """
    def _task(adapter, host_uuid, instance, flavor):
        """Thin wrapper to the crt_lpar in vm."""
        LOG.info(_LI('Creating instance: %s') % instance.name)
        resp = vm.crt_lpar(adapter, host_uuid, instance, flavor)
        return resp

    def _revert(adapter, host_uuid, instance, flavor, result, flow_failures):
        # The parameters have to match the crt method, plus the response +
        # failures even if only a subset are used.

        LOG.warn(_LW('Instance %s to be undefined off host') % instance.name)

        if isinstance(result, task_fail.Failure):
            # No response, nothing to do
            LOG.info(_LI('Create failed.  No delete of LPAR needed for '
                         'instance %s') % instance.name)
            return

        if result is None or result.entry is None:
            # No response, nothing to do
            LOG.info(_LI('Instance %s not found on host.  No update needed.') %
                     instance.name)
            return

        # Wrap to the actual delete.
        lpar = pvm_lpar.LogicalPartition(result.entry)
        vm.dlt_lpar(adapter, lpar.uuid)
        LOG.info(_LI('Instance %s removed from system') % instance.name)

    return task.FunctorTask(
        _task, revert=_revert, name='crt_lpar',
        inject={'adapter': adapter, 'host_uuid': host_uuid,
                'instance': instance, 'flavor': flavor},
        provides='lpar_crt_resp')


def tf_crt_vol_from_img(block_dvr, context, instance, image_meta):
    """Create the Task for the 'create_volume_from_image' in the storage.

    Provides the 'vol_dev_info' for other tasks.  Comes from the block_dvr
    create_volume_from_image method.

    :param block_dvr: The storage driver.
    :param context: The context passed into the spawn method.
    :param instance: The nova instance.
    :param image_meta: The image metadata.
    """

    def _task(block_dvr, context, instance, image_meta):
        LOG.info(_LI('Creating disk for instance: %s') % instance.name)
        return block_dvr.create_volume_from_image(context, instance,
                                                  image_meta)

    def _revert(block_dvr, context, instance, image_meta, result,
                flow_failures):
        # The parameters have to match the crt method, plus the response +
        # failures even if only a subset are used.
        LOG.warn(_LW('Image for instance %s to be deleted') % instance.name)
        if result is None or isinstance(result, task_fail.Failure):
            # No result means no volume to clean up.
            return
        block_dvr.delete_volume(context, result)

    return task.FunctorTask(
        _task, revert=_revert, name='crt_vol_from_img',
        inject={'block_dvr': block_dvr, 'context': context,
                'instance': instance, 'image_meta': image_meta},
        provides='vol_dev_info')


def tf_connect_vol(block_dvr, context, instance):
    """Create the Task for the connect volume to instance method.

    Requires LPAR info through requirement of lpar_crt_resp (provided by
    tf_crt_lpar).

    Requires volume info through requirement of vol_dev_info (provided by
    tf_crt_vol_from_img)

    :param block_dvr: The storage driver.
    :param context: The context passed into the spawn method.
    :param instance: The nova instance.
    """

    def _task(block_dvr, context, instance, lpar_crt_resp, vol_dev_info):
        LOG.info(_LI('Connecting boot disk to instance: %s') % instance.name)
        lpar = pvm_lpar.LogicalPartition(lpar_crt_resp.entry)
        block_dvr.connect_volume(context, instance, vol_dev_info,
                                 lpar.uuid)

    return task.FunctorTask(
        _task, name='connect_vol',
        inject={'block_dvr': block_dvr, 'context': context,
                'instance': instance},
        requires=['lpar_crt_resp', 'vol_dev_info'])


def tf_cfg_drive(adapter, host_uuid, vios_uuid, instance, injected_files,
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

    def _task(adapter, host_uuid, vios_uuid, instance, injected_files,
              network_info, admin_pass, lpar_crt_resp):
        LOG.info(_LI('Creating Config Drive for instance: %s') % instance.name)
        lpar = pvm_lpar.LogicalPartition(lpar_crt_resp.entry)
        media_builder = media.ConfigDrivePowerVM(adapter, host_uuid, vios_uuid)
        vscsi_map = media_builder.create_cfg_drv_vopt(instance, injected_files,
                                                      network_info, lpar.uuid,
                                                      admin_pass=admin_pass)
        return vscsi_map

    # TODO(IBM) Need a revert here.
    return task.FunctorTask(
        _task, name='cfg_drive',
        inject={'adapter': adapter, 'host_uuid': host_uuid,
                'vios_uuid': vios_uuid, 'instance': instance,
                'injected_files': injected_files,
                'network_info': network_info, 'admin_pass': admin_pass},
        requires=['lpar_crt_resp'],
        provides='cfg_drv_vscsi_map')


def tf_connect_cfg_drive(adapter, instance, vios_uuid, vios_name):
    """Create the Task that connects the cfg drive to the instance.

    Requires the 'cfg_drv_vscsi_map'.

    :param adapter: The adapter for the pypowervm API
    :param instance: The nova instance
    :param vios_uuid: The VIOS UUID the drive should be put on.
    :param vios_name: The name of the VIOS that this will be put on.
    """
    def _task(adapter, instance, vios_uuid, vios_name, cfg_drv_vscsi_map):
        LOG.info(_LI('Attaching Config Drive to instance: %s') % instance.name)
        vios.add_vscsi_mapping(adapter, vios_uuid, vios_name,
                               cfg_drv_vscsi_map._element)

    # TODO(IBM) Need a revert here.
    return task.FunctorTask(
        _task, name='cfg_drive_scsi_connect',
        inject={'adapter': adapter, 'instance': instance,
                'vios_uuid': vios_uuid, 'vios_name': vios_name},
        requires=['cfg_drv_vscsi_map'])


def tf_power_on(adapter, host_uuid, instance):
    """Create the Task for the power on of the LPAR.

    Obtains LPAR info through requirement of lpar_crt_resp (provided by
    tf_crt_lpar).

    :param adapter: The pypowervm adapter.
    :param host_uuid: The host UUID.
    :param instance: The nova instance.
    """

    def _task(adapter, host_uuid, instance, lpar_crt_resp):
        LOG.info(_LI('Powering on instance: %s') % instance.name)
        power.power_on(adapter,
                       pvm_lpar.LogicalPartition(lpar_crt_resp.entry),
                       host_uuid)

    def _revert(adapter, host_uuid, instance, lpar_crt_resp, result,
                flow_failures):
        LOG.info(_LI('Powering off instance: %s') % instance.name)

        if isinstance(result, task_fail.Failure):
            # The power on itself failed...can't power off.
            LOG.debug('Power on failed.  Not performing power off.')
            return

        power.power_off(adapter,
                        pvm_lpar.LogicalPartition(lpar_crt_resp.entry),
                        host_uuid,
                        force_immediate=True)

    return task.FunctorTask(
        _task, revert=_revert, name='pwr_lpar',
        inject={'adapter': adapter, 'host_uuid': host_uuid,
                'instance': instance},
        requires=['lpar_crt_resp'])
