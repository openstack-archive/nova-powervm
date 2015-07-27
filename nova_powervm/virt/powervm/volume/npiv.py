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

from nova.compute import task_states
from nova.i18n import _LI
from pypowervm.tasks import vfc_mapper as pvm_vfcm
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.virt import powervm
from nova_powervm.virt.powervm import mgmt
from nova_powervm.virt.powervm.volume import driver as v_driver

LOG = logging.getLogger(__name__)
CONF = cfg.CONF

WWPN_SYSTEM_METADATA_KEY = 'npiv_adpt_wwpns'
FABRIC_STATE_METADATA_KEY = 'fabric_state'
FS_UNMAPPED = 'unmapped'
FS_MGMT_MAPPED = 'mgmt_mapped'
FS_INST_MAPPED = 'inst_mapped'
TASK_STATES_FOR_DISCONNECT = [task_states.DELETING, task_states.SPAWNING]


class NPIVVolumeAdapter(v_driver.FibreChannelVolumeAdapter):
    """The NPIV implementation of the Volume Adapter.

    NPIV stands for N_Port ID Virtualization.  It is a means of providing
    more efficient connections between virtual machines and Fibre Channel
    backed SAN fabrics.

    From a management level, the main difference is that the Virtual Machine
    will have its own WWPNs and own Virtual FC adapter.  The Virtual I/O
    Server only passes through communication directly to the VM itself.
    """

    def connect_volume(self, adapter, host_uuid, vm_uuid, instance,
                       connection_info):
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
        """
        # We need to gather each fabric's port mappings
        npiv_port_mappings = []
        for fabric in self._fabric_names():
            npiv_port_mappings = self._get_fabric_meta(instance, fabric)
            self._remove_npiv_mgmt_mappings(adapter, fabric, host_uuid,
                                            instance, npiv_port_mappings)
            # This method should no-op if the mappings are already attached to
            # the instance...so it won't duplicate the settings every time an
            # attach volume is called.
            LOG.info(_LI("Adding NPIV mapping for instance %s"), instance.name)
            pvm_vfcm.add_npiv_port_mappings(adapter, host_uuid, vm_uuid,
                                            npiv_port_mappings)

            self._set_fabric_state(instance, fabric, FS_INST_MAPPED)

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
        # We should only delete the NPIV mappings if we are running through a
        # VM deletion.  VM deletion occurs when the task state is deleting.
        # However, it can also occur during a 'roll-back' of the spawn.
        # Disconnect of the volumes will only be called during a roll back
        # of the spawn.
        if instance.task_state not in TASK_STATES_FOR_DISCONNECT:
            # NPIV should only remove the VFC mapping upon a destroy of the VM
            return

        # We need to gather each fabric's port mappings
        npiv_port_mappings = []
        for fabric in self._fabric_names():
            npiv_port_mappings.extend(self._get_fabric_meta(instance, fabric))

        # Now that we've collapsed all of the varying fabrics' port mappings
        # into one list, we can call down into pypowervm to remove them in one
        # action.
        LOG.info(_LI("Removing NPIV mapping for instance %s"), instance.name)
        pvm_vfcm.remove_npiv_port_mappings(adapter, host_uuid,
                                           npiv_port_mappings)

    def wwpns(self, adapter, host_uuid, instance):
        """Builds the WWPNs of the adapters that will connect the ports.

        :param adapter: The pypowervm API adapter.
        :param host_uuid: The UUID of the host for the pypowervm adapter.
        :param instance: The nova instance.
        :returns: The list of WWPNs that need to be included in the zone set.
        """
        vios_wraps, mgmt_uuid = None, None
        resp_wwpns = []

        # If this is a new mapping altogether, the WWPNs need to be logged
        # into the fabric so that Cinder can make use of them.  This is a bit
        # of a catch-22 because the LPAR doesn't exist yet.  So a mapping will
        # be created against the mgmt partition and then upon VM creation, the
        # mapping will be moved over to the VM.
        #
        # If a mapping already exists, we can instead just pull the data off
        # of the system metadata from the nova instance.
        for fabric in self._fabric_names():
            fc_state = self._get_fabric_state(instance, fabric)
            LOG.info(_LI("NPIV wwpns fabric state=%(st)s for "
                         "instance %(inst)s") %
                     {'st': fc_state, 'inst': instance.name})

            if (fc_state == FS_UNMAPPED and
                    instance.task_state not in [task_states.DELETING]):

                # At this point we've determined that we need to do a mapping.
                # So we go and obtain the mgmt uuid and the VIOS wrappers.
                # We only do this for the first loop through so as to ensure
                # that we do not keep invoking these expensive calls
                # unnecessarily.
                if mgmt_uuid is None:
                    mgmt_uuid = mgmt.get_mgmt_partition(adapter).uuid

                    # The VIOS wrappers are also not set at this point.  Seed
                    # them as well.  Will get reused on subsequent loops.
                    vios_resp = adapter.read(
                        pvm_vios.VIOS.schema_type,
                        xag=[pvm_vios.VIOS.xags.FC_MAPPING,
                             pvm_vios.VIOS.xags.STORAGE])
                    vios_wraps = pvm_vios.VIOS.wrap(vios_resp)

                # Derive the virtual to physical port mapping
                port_maps = pvm_vfcm.derive_base_npiv_map(
                    vios_wraps, self._fabric_ports(fabric),
                    self._ports_per_fabric())

                # Every loop through, we reverse the vios wrappers.  This is
                # done so that if Fabric A only has 1 port, it goes on the
                # first VIOS.  Then Fabric B would put its port on a different
                # VIOS.  As a form of multi pathing (so that your paths were
                # not restricted to a single VIOS).
                vios_wraps.reverse()

                # Check if the fabrics are unmapped then we need to map it
                # temporarily with the management partition.
                LOG.info(_LI("Adding NPIV Mapping with mgmt partition for "
                             "instance %s") % instance.name)
                port_maps = pvm_vfcm.add_npiv_port_mappings(
                    adapter, host_uuid, mgmt_uuid, port_maps)

                # Set the fabric meta (which indicates on the instance how
                # the fabric is mapped to the physical port) and the fabric
                # state.
                self._set_fabric_meta(instance, fabric, port_maps)
                self._set_fabric_state(instance, fabric, FS_MGMT_MAPPED)
            else:
                # This specific fabric had been previously set.  Just pull
                # from the meta (as it is likely already mapped to the
                # instance)
                port_maps = self._get_fabric_meta(instance, fabric)

            # Port map is set by either conditional, but may be set to None.
            # If not None, then add the WWPNs to the response.
            if port_maps is not None:
                for mapping in port_maps:
                    resp_wwpns.extend(mapping[1].split())

        # The return object needs to be a list for the volume connector.
        return resp_wwpns

    def _remove_npiv_mgmt_mappings(self, adapter, fabric, host_uuid, instance,
                                   npiv_port_map):
        """Remove the fabric from the management partition if necessary.

        Check if the Fabric is mapped to the management partition, if yes then
        remove the mappings and update the fabric state. In order for the WWPNs
        to be on the fabric(for cinder) before the VM is online, the WWPNs get
        mapped to the management partition. This method remove those mappings
        from the management partition so that they can be remapped to the
        actual client VM.

        :param adapter: The pypowervm adapter.
        :param fabric: fabric name
        :param host_uuid: The pypowervm UUID of the host.
        :param instance: The nova instance that the volume should disconnect
                         from.
        :param npiv_port_map: NPIV port mappings needs to be removed.
        """

        if self._get_fabric_state(instance, fabric) == FS_MGMT_MAPPED:
            LOG.info(_LI("Removing NPIV mapping for mgmt partition "
                         "for instance=%s") % instance.name)
            pvm_vfcm.remove_npiv_port_mappings(adapter, host_uuid,
                                               npiv_port_map)
            self._set_fabric_state(instance, fabric, FS_UNMAPPED)
        return

    def host_name(self, adapter, host_uuid, instance):
        """Derives the host name that should be used for the storage device.

        :param adapter: The pypowervm API adapter.
        :param host_uuid: The UUID of the host for the pypowervm adapter.
        :param instance: The nova instance.
        :returns: The host name.
        """
        return instance.name

    def _set_fabric_state(self, instance, fabric, state):
        """Sets the fabric state into the instance's system metadata.
        :param instance: The nova instance
        :param fabric: The name of the fabric
        :param state: state of the fabric whicn needs to be set
         Possible Valid States:-
         FS_UNMAPPED: Initial state unmapped.
         FS_MGMT_MAPPED: Fabric is mapped with the management partition
         FS_INST_MAPPED: Fabric is mapped with the nova instance.
        """

        meta_key = self._sys_fabric_state_key(fabric)
        LOG.info(_LI("Setting Fabric state=%(st)s for instance=%(inst)s") %
                 {'st': state, 'inst': instance.name})
        instance.system_metadata[meta_key] = state

    def _get_fabric_state(self, instance, fabric):
        """Gets the fabric state from the instance's system metadata.
        :param instance: The nova instance
        :param fabric: The name of the fabric
        :Returns state: state of the fabric whicn needs to be set
         Possible Valid States:-
         FS_UNMAPPED: Initial state unmapped.
         FS_MGMT_MAPPED: Fabric is mapped with the management partition
         FS_INST_MAPPED: Fabric is mapped with the nova instance.
        """
        meta_key = self._sys_fabric_state_key(fabric)
        if instance.system_metadata.get(meta_key) is None:
            instance.system_metadata[meta_key] = FS_UNMAPPED

        return instance.system_metadata[meta_key]

    def _sys_fabric_state_key(self, fabric):
        """Returns the nova system metadata key for a given fabric."""
        return FABRIC_STATE_METADATA_KEY + '_' + fabric

    def _set_fabric_meta(self, instance, fabric, port_map):
        """Sets the port map into the instance's system metadata.

        The system metadata will store a per-fabric port map that links the
        physical ports to the virtual ports.  This is needed for the async
        nature between the wwpns call (get_volume_connector) and the
        connect_volume (spawn).

        :param instance: The nova instance.
        :param fabric: The name of the fabric.
        :param port_map: The port map (as defined via the derive_npiv_map
                         pypowervm method).
        """
        # We will store the metadata in a comma-separated string with a
        # multiple of three tokens. Each set of three comprises the Physical
        # Port WWPN followed by the two Virtual Port WWPNs:
        # "p_wwpn1,v_wwpn1,v_wwpn2,p_wwpn2,v_wwpn3,v_wwpn4,..."
        meta_elems = []
        for p_wwpn, v_wwpns in port_map:
            meta_elems.append(p_wwpn)
            meta_elems.extend(v_wwpns.split())

        meta_value = ",".join(meta_elems)
        meta_key = self._sys_meta_fabric_key(fabric)

        instance.system_metadata[meta_key] = meta_value

    def _get_fabric_meta(self, instance, fabric):
        """Gets the port map from the instance's system metadata.

        See _set_fabric_meta.

        :param instance: The nova instance.
        :param fabric: The name of the fabric.
        :return: The port map (as defined via the derive_npiv_map pypowervm
                 method.
        """
        meta_key = self._sys_meta_fabric_key(fabric)
        if instance.system_metadata.get(meta_key) is None:
            return None

        meta_value = instance.system_metadata[meta_key]
        wwpns = meta_value.split(",")

        # Rebuild the WWPNs into the natural structure.
        return [(p, ' '.join([v1, v2])) for p, v1, v2
                in zip(wwpns[::3], wwpns[1::3], wwpns[2::3])]

    def _sys_meta_fabric_key(self, fabric):
        """Returns the nova system metadata key for a given fabric."""
        return WWPN_SYSTEM_METADATA_KEY + '_' + fabric

    def _fabric_names(self):
        """Returns a list of the fabric names."""
        return powervm.NPIV_FABRIC_WWPNS.keys()

    def _fabric_ports(self, fabric_name):
        """Returns a list of WWPNs for the fabric's physical ports."""
        return powervm.NPIV_FABRIC_WWPNS[fabric_name]

    def _ports_per_fabric(self):
        """Returns the number of virtual ports that should be used per fabric.
        """
        return CONF.powervm.ports_per_fabric
