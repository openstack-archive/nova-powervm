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
from oslo_log import log as logging

from nova_powervm.virt.powervm import vios
from nova_powervm.virt.powervm.volume import driver as v_driver

from pypowervm.tasks import hdisk
from pypowervm.wrappers import virtual_io_server as pvm_vios

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
        # TODO(IBM) Need to find the right Virtual I/O Server via the i_wwpns.
        # This will require querying the full Virtual I/O Server.  This is a
        # temporary work around for single VIOS/single FC port.
        vio_map = vios.get_vios_name_map(adapter, host_uuid)
        vios_name = vio_map.keys()[0]
        vios_uuid = vio_map[vios_name]

        # Get the initiators
        it_map = connection_info['data']['initiator_target_map']

        lun = connection_info['data']['target_lun']
        i_wwpns = it_map.keys()
        t_wwpns = []
        # Build single list of targets
        for it_list in it_map.values():
            t_wwpns.extend(it_list)

        itls = hdisk.build_itls(i_wwpns, t_wwpns, lun)
        status, devname, udid = hdisk.discover_hdisk(adapter, vios_uuid,
                                                     itls)
        vscsi_map = pvm_vios.VSCSIMapping.bld_to_pv(adapter, host_uuid,
                                                    vm_uuid, devname)
        vios.add_vscsi_mapping(adapter, vios_uuid, vios_name, vscsi_map)

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
