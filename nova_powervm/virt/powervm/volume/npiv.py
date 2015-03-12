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

from pypowervm.jobs import wwpn as pvm_wwpn
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.virt.powervm import vios
from nova_powervm.virt.powervm.volume import driver as v_driver

WWPN_SYSTEM_METADATA_KEY = 'npiv_adpt_wwpns'


class NPIVVolumeAdapter(v_driver.PowerVMVolumeAdapter):
    """The NPIV implementation of the Volume Adapter.

    NPIV stands for N_Port ID Virtualization.  It is a means of providing
    more efficient connections between virtual machines and Fibre Channel
    backed SAN fabrics.

    From a management level, the main difference is that the Virtual Machine
    will have its own WWPNs and own Virtual FC adapter.  The Virtual I/O
    Server only passes through communication directly to the VM itself.
    """

    def connect_volume(self, adapter, host_uuid, vios_uuid, vm_uuid, instance,
                       connection_info, disk_dev):
        """Connects the volume.

        :param adapter: The pypowervm adapter.
        :param host_uuid: The pypowervm UUID of the host.
        :param vios_uuid: The pypowervm UUID of the VIOS.
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
        :param disk_dev: The name of the device on the backing storage device.
        """
        c_wwpns = connection_info['data']['initiator_target_map'].keys()

        # TODO(thorst) Update ports to non-hardcoded value.
        vfc_map = pvm_vios.VFCMapping.bld(adapter, host_uuid, vm_uuid,
                                          'fcs0', c_wwpns)
        # TODO(thorst) change the vios name to proper value
        vios.add_vfc_mapping(adapter, vios_uuid, 'temp', vfc_map)

    def disconnect_volume(self, adapter, host_uuid, vios_uuid, vm_uuid,
                          instance, connection_info, disk_dev):
        """Disconnect the volume.

        :param adapter: The pypowervm adapter.
        :param host_uuid: The pypowervm UUID of the host.
        :param vios_uuid: The pypowervm UUID of the VIOS.
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
        :param disk_dev: The name of the device on the backing storage device.
        """
        vios_resp = adapter.read(
            pvm_vios.VIOS.schema_type, root_id=vios_uuid,
            xag=[pvm_vios.XAGEnum.VIOS_FC_MAPPING])
        vios_w = pvm_vios.VIOS.wrap(vios_resp)

        # Find the existing mappings....
        existing_fc_maps = vios_w.vfc_mappings
        lpar_fc_maps = []

        # Find the corresponding FC mapping for this connection_info
        for existing_fc_map in existing_fc_maps:
            # Set of two WWPNs
            wwpns1 = existing_fc_map.client_adapter.wwpns

            # List of two WWPNs
            wwpns2 = connection_info['data']['initiator_target_map'].keys()
            if self._wwpn_match(wwpns1, wwpns2):
                lpar_fc_maps.append(existing_fc_map)

        # Remove each mapping from the existing map set
        for removal_map in lpar_fc_maps:
            vios_w.vfc_mappings.remove(removal_map)

        # Now perform the update of the VIOS
        adapter.update(vios_w, vios_w.etag, pvm_vios.VIOS.schema_type,
                       root_id=vios_w.uuid,
                       xag=[pvm_vios.XAGEnum.VIOS_FC_MAPPING])

    def _wwpn_match(self, list1, list2):
        """Determines if two sets/lists of WWPNs match."""
        set1 = set([x.lower() for x in list1])
        set2 = set([x.lower() for x in list2])
        return set1 == set2

    def wwpns(self, adapter, host_uuid, instance):
        """Builds the WWPNs of the adapters that will connect the ports.

        :param adapter: The pypowervm API adapter.
        :param host_uuid: The UUID of the host for the pypowervm adapter.
        :param instance: The nova instance.
        :returns: The list of WWPNs that need to be included in the zone set.
        """
        # TODO(IBM) Rather than look if in system metadata, could perhaps
        # check if the instance exists on the hypervisor and look up WWPNs
        # straight from system.

        # The WWPNs should be persisted on the instance metadata.
        wwpn_key = instance.system_metadata.get(WWPN_SYSTEM_METADATA_KEY)
        if wwpn_key is not None:
            return wwpn_key.split(' ')

        # If it was not on the instance metadata, we should generate the new
        # WWPNs and then put it on the instance metadata.
        wwpns = list(pvm_wwpn.build_wwpn_pair(adapter, host_uuid))
        instance.system_metadata[WWPN_SYSTEM_METADATA_KEY] = " ".join(wwpns)

        # The return object needs to be a list for the volume connector.
        return wwpns

    def host_name(self, adapter, host_uuid, instance):
        """Derives the host name that should be used for the storage device.

        :param adapter: The pypowervm API adapter.
        :param host_uuid: The UUID of the host for the pypowervm adapter.
        :param instance: The nova instance.
        :returns: The host name.
        """
        return instance.name
