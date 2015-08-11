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
from taskflow import task

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

    @property
    def min_xags(self):
        """List of pypowervm XAGs needed to support this adapter."""
        return [pvm_vios.VIOS.xags.FC_MAPPING, pvm_vios.VIOS.xags.STORAGE]

    def _connect_volume(self):
        """Connects the volume."""
        # Run the add for each fabric.
        for fabric in self._fabric_names():
            self._add_maps_for_fabric(fabric)

    def _disconnect_volume(self):
        """Disconnect the volume."""
        # We should only delete the NPIV mappings if we are running through a
        # VM deletion.  VM deletion occurs when the task state is deleting.
        # However, it can also occur during a 'roll-back' of the spawn.
        # Disconnect of the volumes will only be called during a roll back
        # of the spawn.
        if self.instance.task_state not in TASK_STATES_FOR_DISCONNECT:
            # NPIV should only remove the VFC mapping upon a destroy of the VM
            return

        # Run the disconnect for each fabric
        for fabric in self._fabric_names():
            self._remove_maps_for_fabric(fabric)

    def wwpns(self):
        """Builds the WWPNs of the adapters that will connect the ports."""
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
            fc_state = self._get_fabric_state(fabric)
            LOG.info(_LI("NPIV wwpns fabric state=%(st)s for "
                         "instance %(inst)s") %
                     {'st': fc_state, 'inst': self.instance.name})

            if (fc_state == FS_UNMAPPED and
                    self.instance.task_state not in [task_states.DELETING]):

                # At this point we've determined that we need to do a mapping.
                # So we go and obtain the mgmt uuid and the VIOS wrappers.
                # We only do this for the first loop through so as to ensure
                # that we do not keep invoking these expensive calls
                # unnecessarily.
                if mgmt_uuid is None:
                    mgmt_uuid = mgmt.get_mgmt_partition(self.adapter).uuid

                    # The VIOS wrappers are also not set at this point.  Seed
                    # them as well.  Will get reused on subsequent loops.
                    vios_wraps = self.tx_mgr.feed

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
                             "instance %s") % self.instance.name)
                port_maps = pvm_vfcm.add_npiv_port_mappings(
                    self.adapter, self.host_uuid, mgmt_uuid, port_maps)

                # Set the fabric meta (which indicates on the instance how
                # the fabric is mapped to the physical port) and the fabric
                # state.
                self._set_fabric_meta(fabric, port_maps)
                self._set_fabric_state(fabric, FS_MGMT_MAPPED)
            else:
                # This specific fabric had been previously set.  Just pull
                # from the meta (as it is likely already mapped to the
                # instance)
                port_maps = self._get_fabric_meta(fabric)

            # Port map is set by either conditional, but may be set to None.
            # If not None, then add the WWPNs to the response.
            if port_maps is not None:
                for mapping in port_maps:
                    resp_wwpns.extend(mapping[1].split())

        # The return object needs to be a list for the volume connector.
        return resp_wwpns

    def _add_maps_for_fabric(self, fabric):
        """Adds the vFC storage mappings to the VM for a given fabric.

        Will check if the Fabric is mapped to the management partition.  If it
        is, then it will remove the mappings and update the fabric state. This
        is because, in order for the WWPNs to be on the fabric (for Cinder)
        before the VM is online, the WWPNs get mapped to the management
        partition.

        This method will remove from the management partition (if needed), and
        then assign it to the instance itself.

        :param fabric: The fabric to add the mappings to.
        """
        npiv_port_maps = self._get_fabric_meta(fabric)
        vios_wraps = self.tx_mgr.feed

        # If currently mapped to the mgmt partition, remove the mappings so
        # that they can be added to the client.
        if self._get_fabric_state(fabric) == FS_MGMT_MAPPED:
            mgmt_uuid = mgmt.get_mgmt_partition(self.adapter).uuid

            # Each port mapping should be removed from the VIOS.
            for npiv_port_map in npiv_port_maps:
                def rm_mgmt_func(vios_w):
                    LOG.info(_LI("Removing NPIV mapping for mgmt partition "
                                 "for instance %(inst)s on VIOS %(vios)s") %
                             {'inst': self.instance.name, 'vios': vios_w.name})
                    pvm_vfcm.remove_maps(vios_w, mgmt_uuid,
                                         port_map=npiv_port_map)

                # This should be added for the appropriate VIOS
                vios_w = pvm_vfcm.find_vios_for_port_map(vios_wraps,
                                                         npiv_port_map)
                self.tx_mgr.wrapper_tasks[vios_w.uuid].add_functor_subtask(
                    rm_mgmt_func)

        # This loop adds the maps from the appropriate VIOS to the client VM
        for npiv_port_map in npiv_port_maps:
            def add_func(vios_w):
                LOG.info(_LI("Adding NPIV mapping for instance %(inst)s for "
                             "Virtual I/O Server %(vios)s"),
                         {'inst': self.instance.name, 'vios': vios_w.name})
                return pvm_vfcm.add_map(vios_w, self.host_uuid, self.vm_uuid,
                                        npiv_port_map)

            vios_w = pvm_vfcm.find_vios_for_port_map(vios_wraps, npiv_port_map)
            self.tx_mgr.wrapper_tasks[vios_w.uuid].add_functor_subtask(
                add_func)

        # After all the mappings, make sure the fabric state is updated.
        def set_state():
            self._set_fabric_state(fabric, FS_INST_MAPPED)
        self.tx_mgr.add_post_execute(task.FunctorTask(set_state,
                                                      name='fab_%s' % fabric))

    def _remove_maps_for_fabric(self, fabric):
        """Removes the vFC storage mappings from the VM for a given fabric.

        :param fabric: The fabric to remove the mappings from.
        """
        npiv_port_maps = self._get_fabric_meta(fabric)
        vios_wraps = self.tx_mgr.feed

        for npiv_port_map in npiv_port_maps:
            def rm_func(vios_w):
                LOG.info(_LI("Removing a NPIV mapping for instance %(inst)s "
                             "for fabric %(fabric)s"),
                         {'inst': self.instance.name, 'fabric': fabric})
                return pvm_vfcm.remove_maps(vios_w, self.vm_uuid,
                                            port_map=npiv_port_map)

            vios_w = pvm_vfcm.find_vios_for_port_map(vios_wraps, npiv_port_map)
            self.tx_mgr.wrapper_tasks[vios_w.uuid].add_functor_subtask(rm_func)

    def host_name(self):
        """Derives the host name that should be used for the storage device.

        :return: The host name.
        """
        return self.instance.name

    def _set_fabric_state(self, fabric, state):
        """Sets the fabric state into the instance's system metadata.
        :param fabric: The name of the fabric
        :param state: state of the fabric which needs to be set

         Possible Valid States:
         FS_UNMAPPED: Initial state unmapped.
         FS_MGMT_MAPPED: Fabric is mapped with the management partition
         FS_INST_MAPPED: Fabric is mapped with the nova instance.
        """
        meta_key = self._sys_fabric_state_key(fabric)
        LOG.info(_LI("Setting Fabric state=%(st)s for instance=%(inst)s") %
                 {'st': state, 'inst': self.instance.name})
        self.instance.system_metadata[meta_key] = state

    def _get_fabric_state(self, fabric):
        """Gets the fabric state from the instance's system metadata.

        :param fabric: The name of the fabric
        :return: The state of the fabric which needs to be set

         Possible Valid States:
         FS_UNMAPPED: Initial state unmapped.
         FS_MGMT_MAPPED: Fabric is mapped with the management partition
         FS_INST_MAPPED: Fabric is mapped with the nova instance.
        """
        meta_key = self._sys_fabric_state_key(fabric)
        if self.instance.system_metadata.get(meta_key) is None:
            self.instance.system_metadata[meta_key] = FS_UNMAPPED

        return self.instance.system_metadata[meta_key]

    def _sys_fabric_state_key(self, fabric):
        """Returns the nova system metadata key for a given fabric."""
        return FABRIC_STATE_METADATA_KEY + '_' + fabric

    def _set_fabric_meta(self, fabric, port_map):
        """Sets the port map into the instance's system metadata.

        The system metadata will store a per-fabric port map that links the
        physical ports to the virtual ports.  This is needed for the async
        nature between the wwpns call (get_volume_connector) and the
        connect_volume (spawn).

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

        self.instance.system_metadata[meta_key] = meta_value

    def _get_fabric_meta(self, fabric):
        """Gets the port map from the instance's system metadata.

        See _set_fabric_meta.

        :param fabric: The name of the fabric.
        :return: The port map (as defined via the derive_npiv_map pypowervm
                 method.
        """
        meta_key = self._sys_meta_fabric_key(fabric)
        if self.instance.system_metadata.get(meta_key) is None:
            return None

        meta_value = self.instance.system_metadata[meta_key]
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
