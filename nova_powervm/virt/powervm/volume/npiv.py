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

from oslo_log import log as logging

from nova.compute import task_states
from nova.i18n import _LI
from pypowervm.jobs import wwpn as pvm_wwpn
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.virt.powervm.volume import driver as v_driver

LOG = logging.getLogger(__name__)

WWPN_SYSTEM_METADATA_KEY = 'npiv_adpt_wwpns'

TASK_STATES_FOR_DISCONNECT = [task_states.DELETING, task_states.SPAWNING]


class NPIVVolumeAdapter(v_driver.PowerVMVolumeAdapter):
    """The NPIV implementation of the Volume Adapter.

    NPIV stands for N_Port ID Virtualization.  It is a means of providing
    more efficient connections between virtual machines and Fibre Channel
    backed SAN fabrics.

    From a management level, the main difference is that the Virtual Machine
    will have its own WWPNs and own Virtual FC adapter.  The Virtual I/O
    Server only passes through communication directly to the VM itself.
    """

    def connect_volume(self, adapter, host_uuid, vios_uuid, vm_uuid, vios_name,
                       instance, connection_info):
        """Connects the volume.

        :param adapter: The pypowervm adapter.
        :param host_uuid: The pypowervm UUID of the host.
        :param vios_uuid: The pypowervm UUID of the VIOS.
        :param vm_uuid: The powervm UUID of the VM.
        :param vios_name: The name of the VIOS.
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
        # # List of connector's WWPNs
        c_wwpns = connection_info['data']['initiator_target_map'].keys()

        # Read the VIOS and determine if there is already a NPIV mapping in
        # place for these WWPNs.
        vios_resp = adapter.read(pvm_vios.VIOS.schema_type, root_id=vios_uuid,
                                 xag=[pvm_vios.XAGEnum.VIOS_FC_MAPPING])
        vios_w = pvm_vios.VIOS.wrap(vios_resp)

        existing_fc_maps = vios_w.vfc_mappings
        for existing_fc_map in existing_fc_maps:
            # If there is no client adapter, then it is not a mapping to
            # utilize
            if existing_fc_map.client_adapter is None:
                continue

            # Get the set of existing WWPNs
            e_wwpns = existing_fc_map.client_adapter.wwpns

            # If the existing WWPNs match off the initiator_target_map, no
            # new mapping is required.
            if self._wwpn_match(e_wwpns, c_wwpns):
                return

        # TODO(thorst) Update ports to non-hardcoded value.
        vfc_map = pvm_vios.VFCMapping.bld(adapter, host_uuid, vm_uuid,
                                          'fcs0', c_wwpns)

        # Run the update to the VIOS
        LOG.info(_LI("Adding NPIV mapping for instance %s") % instance.name)
        existing_fc_maps.append(vfc_map)
        vios_w.update(adapter, xag=[pvm_vios.XAGEnum.VIOS_FC_MAPPING])

    def disconnect_volume(self, adapter, host_uuid, vios_uuid, vm_uuid,
                          instance, connection_info):
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
        """
        # We should only delete the NPIV mappings if we are running through a
        # VM deletion.  VM deletion occurs when the task state is deleting.
        # However, it can also occur during a 'roll-back' of the spawn.
        # Disconnect of the volumes will only be called during a roll back
        # of the spawn.
        if instance.task_state not in TASK_STATES_FOR_DISCONNECT:
            # NPIV should only remove the VFC mapping upon a destroy of the VM
            return

        # List of connector's WWPNs
        c_wwpns = connection_info['data']['initiator_target_map'].keys()

        # Read the VIOS for the existing mappings.
        vios_resp = adapter.read(pvm_vios.VIOS.schema_type, root_id=vios_uuid,
                                 xag=[pvm_vios.XAGEnum.VIOS_FC_MAPPING])
        vios_w = pvm_vios.VIOS.wrap(vios_resp)

        # Find the existing mappings.
        existing_fc_maps = vios_w.vfc_mappings
        lpar_fc_maps = []

        # Find the corresponding FC mapping for this connection_info.  Once
        # found, we remove the mapping and update the VIOS.
        for existing_fc_map in existing_fc_maps:
            # The existing WWPNs
            e_wwpns = existing_fc_map.client_adapter.wwpns

            if self._wwpn_match(c_wwpns, e_wwpns):
                lpar_fc_maps.append(existing_fc_map)
                break

        # Remove each mapping from the existing map set
        for removal_map in lpar_fc_maps:
            vios_w.vfc_mappings.remove(removal_map)

        # Now perform the update of the VIOS
        vios_w.update(adapter, xag=[pvm_vios.XAGEnum.VIOS_FC_MAPPING])

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
