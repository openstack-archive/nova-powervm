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
from nova.openstack.common import log as logging
from pypowervm.jobs import power
from pypowervm.wrappers import logical_partition as lpar_w

from taskflow import task

from nova_powervm.virt.powervm import media
from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)


def tf_pwr_off_lpar(adapter, host_uuid, lpar_uuid, instance):
    """Creates the Task to power off an LPAR.

    :param adapter: The adapter for the pypowervm API
    :param host_uuid: The host UUID
    :param lpar_uuid: The UUID of the lpar that has media.
    :param instance: The nova instance.
    """
    def _task(adapter, host_uuid, lpar_uuid, instance):
        LOG.info(_LI('Powering off instance %s for deletion')
                 % instance.name)
        resp = adapter.read(lpar_w.LPAR_ROOT, lpar_uuid)
        lpar = lpar_w.LogicalPartition.load_from_response(resp)
        power.power_off(adapter, lpar, host_uuid, force_immediate=True)

    return task.FunctorTask(
        _task, name='pwr_off_lpar',
        inject={'adapter': adapter, 'host_uuid': host_uuid,
                'lpar_uuid': lpar_uuid, 'instance': instance})


def tf_dlt_vopt(adapter, host_uuid, vios_uuid, instance, lpar_uuid):
    """Creates the Task to delete the instances virtual optical media.

    :param adapter: The adapter for the pypowervm API
    :param host_uuid: The host UUID of the system.
    :param vios_uuid: The VIOS UUID the media is being deleted from.
    :param instance: The nova instance.
    :param lpar_uuid: The UUID of the lpar that has media.
    """
    def _task(adapter, host_uuid, vios_uuid, instance, lpar_uuid):
        LOG.info(_LI('Deleting Virtual Optical Media for instance %s')
                 % instance.name)
        media_builder = media.ConfigDrivePowerVM(adapter, host_uuid,
                                                 vios_uuid)
        media_builder.dlt_vopt(lpar_uuid)

    return task.FunctorTask(
        _task, name='vopt_delete',
        inject={'adapter': adapter, 'host_uuid': host_uuid,
                'vios_uuid': vios_uuid, 'instance': instance,
                'lpar_uuid': lpar_uuid})


def tf_detach_storage(block_dvr, context, instance, lpar_uuid):
    """Creates the Task to detach the storage adapters.

    Provides the stor_adpt_mappings.  A list of pypowervm VirtualSCSIMappings
    or VirtualFCMappings (depending on the storage adapter).

    :param block_dvr: The StorageAdapter for the VM.
    :param context: The nova context.
    :param instance: The nova instance.
    :param lpar_uuid: The UUID of the lpar..
    """
    def _task(block_dvr, context, instance, lpar_uuid):
        LOG.info(_LI('Detaching disk storage adapters for instance %s')
                 % instance.name)
        return block_dvr.disconnect_image_volume(context, instance, lpar_uuid)

    return task.FunctorTask(
        _task, name='detach_storage',
        provides='stor_adpt_mappings',
        inject={'block_dvr': block_dvr, 'context': context,
                'instance': instance, 'lpar_uuid': lpar_uuid})


def tf_delete_storage(block_dvr, context, instance):
    """Creates the Task to delete the storage from the system.

    Requires the stor_adpt_mappings.

    :param block_dvr: The StorageAdapter for the VM.
    :param context: The nova context.
    :param instance: The nova instance.
    """
    def _task(block_dvr, context, instance, stor_adpt_mappings):
        LOG.info(_LI('Deleting storage for instance %s.') % instance.name)
        block_dvr.delete_volumes(context, instance, stor_adpt_mappings)

    return task.FunctorTask(
        _task, name='dlt_storage',
        requires=['stor_adpt_mappings'],
        inject={'block_dvr': block_dvr, 'context': context,
                'instance': instance})


def tf_dlt_lpar(adapter, lpar_uuid, instance):
    """Create the Task to delete the VM from the system.

    :param adapter: The adapter for the pypowervm API.
    :param lpar_uuid: The VM's PowerVM UUID.
    :param instance: The nova instance.
    """
    def _task(adapter, lpar_uuid, instance):
        LOG.info(_LI('Deleting instance %s from system.') % instance.name)
        vm.dlt_lpar(adapter, lpar_uuid)

    return task.FunctorTask(
        _task, name='dlt_lpar',
        inject={'adapter': adapter, 'lpar_uuid': lpar_uuid,
                'instance': instance})
