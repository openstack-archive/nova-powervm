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

from oslo_config import cfg
from oslo_log import log as logging

from nova_powervm.virt.powervm import vios
from nova_powervm.virt.powervm.volume import driver as v_driver

import pypowervm.exceptions as pexc
from pypowervm.tasks import hdisk
from pypowervm.tasks import scsi_mapper as tsk_map
from pypowervm.wrappers import virtual_io_server as pvm_vios

import six

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class VscsiVolumeAdapter(v_driver.FibreChannelVolumeAdapter):
    """The vSCSI implementation of the Volume Adapter.

    vSCSI is the internal mechanism to link a given hdisk on the Virtual
    I/O Server to a Virtual Machine.  This volume driver will take the
    information from the driver and link it to a given virtual machine.
    """

    def __init__(self):
        super(VscsiVolumeAdapter, self).__init__()
        self._pfc_wwpns = None

    def connect_volume(self, adapter, host_uuid, vm_uuid, instance,
                       connection_info):
        """Connects the volume.

        :param adapter: The pypowervm adapter.
        :param host_uuid: The pypowervm UUID of the host.
        :param vm_uuid: The powervm UUID of the VM.
        :param instance: The nova instance that the volume should connect to.
        :param connection_info: Comes from the BDM.  Example connection_info:
                {
                'driver_volume_type':'fibre_channel',
                'serial':u'10d9934e-b031-48ff-9f02-2ac533e331c8',
                'data':{
                   'initiator_target_map':{
                      '21000024FF649105':['500507680210E522'],
                      '21000024FF649104':['500507680210E522'],
                      '21000024FF649107':['500507680210E522'],
                      '21000024FF649106':['500507680210E522']
                   },
                   'target_discovered':False,
                   'qos_specs':None,
                   'volume_id':'10d9934e-b031-48ff-9f02-2ac533e331c8',
                   'target_lun':0,
                   'access_mode':'rw',
                   'target_wwn':'500507680210E522'
                }
        """

        # Get the initiators
        it_map = connection_info['data']['initiator_target_map']
        volume_id = connection_info['data']['volume_id']
        lun = connection_info['data']['target_lun']
        hdisk_found = False

        i_wwpns = it_map.keys()
        t_wwpns = []
        # Build single list of target wwpns
        for it_list in it_map.values():
            t_wwpns.extend(it_list)

        # Get VIOS map
        vio_map = vios.get_vios_name_map(adapter, host_uuid)

        # Iterate through host vios list to find valid hdisks and map to VM.
        # TODO(IBM): The VIOS should only include the intersection with
        # defined SCG targets when they are available.
        for vios_name, vios_uuid in vio_map.iteritems():
            # TODO(IBM): Investigate if i_wwpns passed to discover_hdisk
            # should be intersection with VIOS pfc_wwpns
            itls = hdisk.build_itls(i_wwpns, t_wwpns, lun)
            status, device_name, udid = hdisk.discover_hdisk(adapter,
                                                             vios_uuid, itls)
            if device_name is not None and status in [
                    hdisk.LUA_STATUS_DEVICE_AVAILABLE,
                    hdisk.LUA_STATUS_FOUND_ITL_ERR]:
                LOG.info(_LI('Discovered %(hdisk)s on vios %(vios)s for '
                         'volume %(volume_id)s. Status code: %(status)s.') %
                         {'hdisk': device_name, 'vios': vios_name,
                          'volume_id': volume_id, 'status': str(status)})
                self._add_mapping(adapter, host_uuid, vm_uuid, vios_uuid,
                                  device_name)
                connection_info['data']['target_udid'] = udid
                LOG.info(_LI('Device attached: %s'), device_name)
                hdisk_found = True
            elif status == hdisk.LUA_STATUS_DEVICE_IN_USE:
                LOG.warn(_LW('Discovered device %(dev)s for volume %(volume)s '
                             'on %(vios)s is in use Errorcode: %(status)s.'),
                         {'dev': device_name, 'volume': volume_id,
                          'vios': vios_name, 'status': str(status)})
        # A valid hdisk was not found so log and exit
        if not hdisk_found:
            msg = (_LW('Failed to discover valid hdisk on %(vios)s '
                       'for volume %(volume_id)s. status: '
                       '%(status)s.') % {'vios': vios_name,
                                         'volume_id': volume_id,
                                         'status': str(status)})
            LOG.warn(msg)
            if device_name is None:
                device_name = 'None'
            ex_args = {'backing_dev': device_name,
                       'instance_name': instance.name,
                       'reason': six.text_type(msg)}
            raise pexc.VolumeAttachFailed(**ex_args)

    def disconnect_volume(self, adapter, host_uuid, vm_uuid, instance,
                          connection_info):
        """Disconnect the volume.

        :param adapter: The pypowervm adapter.
        :param host_uuid: The pypowervm UUID of the host.
        :param vm_uuid: The powervm UUID of the VM.
        :param instance: The nova instance that the volume should disconnect
                         from.
        :param connection_info: Comes from the BDM.  Example connection_info:
                {
                'driver_volume_type':'fibre_channel',
                'serial':u'10d9934e-b031-48ff-9f02-2ac533e331c8',
                'data':{
                   'initiator_target_map':{
                      '21000024FF649105':['500507680210E522'],
                      '21000024FF649104':['500507680210E522'],
                      '21000024FF649107':['500507680210E522'],
                      '21000024FF649106':['500507680210E522']
                   },
                   'target_discovered':False,
                   'qos_specs':None,
                   'volume_id':'10d9934e-b031-48ff-9f02-2ac533e331c8',
                   'target_lun':0,
                   'access_mode':'rw',
                   'target_wwn':'500507680210E522'
                }
        """
        # TODO(IBM) Need to find the right Virtual I/O Server via the i_wwpns.
        # This will require querying the full Virtual I/O Server.  This is a
        # temporary work around for single VIOS/single FC port.
        #         vio_map = vios.get_vios_name_map(adapter, host_uuid)
        #         vios_name = vio_map.keys()[0]
        #         vios_uuid = vio_map[vios_name]

        pass

    def wwpns(self, adapter, host_uuid, instance):
        """Builds the WWPNs of the adapters that will connect the ports.

        :param adapter: The pypowervm API adapter.
        :param host_uuid: The UUID of the host for the pypowervm adapter.
        :param instance: The nova instance.
        :returns: The list of WWPNs that need to be included in the zone set.
        """
        if self._pfc_wwpns is None:
            self._pfc_wwpns = vios.get_physical_wwpns(adapter, host_uuid)
        return self._pfc_wwpns

    def host_name(self, adapter, host_uuid, instance):
        """Derives the host name that should be used for the storage device.

        :param adapter: The pypowervm API adapter.
        :param host_uuid: The UUID of the host for the pypowervm adapter.
        :param instance: The nova instance.
        :returns: The host name.
        """
        return CONF.host

    def _add_mapping(self, adapter, host_uuid, vm_uuid, vios_uuid,
                     device_name):
        """This method builds the vscsi map and adds the mapping to
        the given VIOS.

        :param adapter: The pypowervm API adapter.
        :param host_uuid: The UUID of the target host
        :param vm_uuid" The UUID of the VM instance
        :param vios_uuid: The UUID of the vios for the pypowervm adapter.
        :param device_name: The The hdisk device name
        """
        vscsi_map = pvm_vios.VSCSIMapping.bld_to_pv(adapter, host_uuid,
                                                    vm_uuid, device_name)
        tsk_map.add_vscsi_mapping(adapter, vios_uuid, vscsi_map)