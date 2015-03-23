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
from pypowervm.tasks import wwpn as pvm_wwpn
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.virt import powervm
from nova_powervm.virt.powervm.volume import driver as v_driver

LOG = logging.getLogger(__name__)
CONF = cfg.CONF

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
            npiv_port_mappings.extend(self._get_fabric_meta(instance, fabric))

        # Now that we've collapsed all of the varying fabrics' port mappings
        # into one list, we can call down into pypowervm to add them in one
        # action.
        #
        # This method should no-op if the mappings are already attached to
        # the instance...so it won't duplicate the settings every time an
        # attach volume is called.
        LOG.info(_LI("Adding NPIV mapping for instance %s") % instance.name)
        pvm_wwpn.add_npiv_port_mappings(adapter, host_uuid, vm_uuid,
                                        npiv_port_mappings)

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
        LOG.info(_LI("Removing NPIV mapping for instance %s") % instance.name)
        pvm_wwpn.remove_npiv_port_mappings(adapter, host_uuid,
                                           npiv_port_mappings)

    def wwpns(self, adapter, host_uuid, instance):
        """Builds the WWPNs of the adapters that will connect the ports.

        :param adapter: The pypowervm API adapter.
        :param host_uuid: The UUID of the host for the pypowervm adapter.
        :param instance: The nova instance.
        :returns: The list of WWPNs that need to be included in the zone set.
        """
        # Check to see if all of the fabrics have had the metadata set.
        # We check against all of the fabrics in case a fabric was later
        # added to the system.
        wwpn_keys = []
        bad_fabric = False
        for fabric in self._fabric_names():
            metas = self._get_fabric_meta(instance, fabric)
            if metas is None:
                bad_fabric = True
                break

            # Must have been set.  Extend the wwpn keys with each element.
            wwpn_keys.extend([meta_val[1] for meta_val in metas])

        # A 'bad_fabric' is when we had one fabric that was missing.  So
        # we just kick off a rebuild.
        if not bad_fabric:
            return wwpn_keys

        # At this point, the WWPNs need to be logged into the fabric.  But
        # before that can be done, the mapping needs to be derived between
        # the logical ports and the physical ports.
        #
        # This should be done on a per-fabric basis.
        vios_resp = adapter.read(pvm_vios.VIOS.schema_type,
                                 xag=[pvm_vios.XAGEnum.VIOS_FC_MAPPING])
        vios_wraps = pvm_vios.VIOS.wrap(vios_resp)
        resp_wwpns = []
        for fabric in self._fabric_names():
            v_port_wwpns = self._build_wwpns_for_fabric(adapter, host_uuid)
            resp_wwpns.extend(v_port_wwpns)

            # Derive the virtual to physical port mapping
            port_map = pvm_wwpn.derive_npiv_map(vios_wraps,
                                                self._fabric_ports(fabric),
                                                v_port_wwpns)

            self._set_fabric_meta(instance, fabric, port_map)

            # Every loop through, we reverse the vios wrappers.  This is done
            # so that if Fabric A only has 1 port, it goes on the first VIOS.
            # Then Fabric B would put its port on a different VIOS.  As a form
            # of multi pathing (so that your paths were not restricted to a
            # single VIOS).
            vios_wraps.reverse()

            # TODO(IBM) Need to log the WWPNs in temporarily here.

        # The return object needs to be a list for the volume connector.
        return resp_wwpns

    def host_name(self, adapter, host_uuid, instance):
        """Derives the host name that should be used for the storage device.

        :param adapter: The pypowervm API adapter.
        :param host_uuid: The UUID of the host for the pypowervm adapter.
        :param instance: The nova instance.
        :returns: The host name.
        """
        return instance.name

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

    def _build_wwpns_for_fabric(self, adapter, host_uuid):
        """Builds all of the WWPNs that are needed for a given fabric."""
        wwpns = []
        i = 0
        while i < self._ports_per_fabric():
            wwpns.extend(pvm_wwpn.build_wwpn_pair(adapter, host_uuid))
            i += 1
        return wwpns

    def _fabric_names(self):
        """Returns a list of the fabric names."""
        return powervm.NPIV_FABRIC_WWPNS.keys()

    def _fabric_ports(self, fabric_name):
        """Returns a list of WWPNs for the fabric's physical ports."""
        return powervm.NPIV_FABRIC_WWPNS[fabric_name]

    def _ports_per_fabric(self):
        """Returns the number of virtual ports that should be used per fabric.
        """
        return CONF.npiv.ports_per_fabric
