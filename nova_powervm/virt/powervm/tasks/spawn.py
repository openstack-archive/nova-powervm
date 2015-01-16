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

        if result is None or result.entry is None:
            # No response, nothing to do
            LOG.info(_LI('Instance %s not found on host.  No update needed.') %
                     instance.name)
            return

        # Wrap to the actual delete.
        lpar = pvm_lpar.LogicalPartition(result.entry)
        vm.dlt_lpar(adapter, lpar.get_uuid())
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
        if result is None:
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
                                 lpar.get_uuid())

    return task.FunctorTask(
        _task, name='connect_vol',
        inject={'block_dvr': block_dvr, 'context': context,
                'instance': instance},
        requires=['lpar_crt_resp', 'vol_dev_info'])


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
        power.power_on(adapter, host_uuid, lpar_crt_resp.entry)

    def _revert(adapter, host_uuid, instance, lpar_crt_resp, result,
                flow_failures):
        LOG.info(_LI('Powering off instance: %s') % instance.name)
        power.power_off(adapter, lpar_crt_resp.entry, host_uuid,
                        force_immediate=True)

    return task.FunctorTask(
        _task, revert=_revert, name='pwr_lpar',
        inject={'adapter': adapter, 'host_uuid': host_uuid,
                'instance': instance},
        requires=['lpar_crt_resp'])
