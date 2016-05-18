# Copyright 2015, 2016 IBM Corp.
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

import copy
from oslo_concurrency import lockutils
from oslo_log import log as logging
from taskflow import task

from nova.compute import task_states
from oslo_serialization import jsonutils
from pypowervm import const as pvm_const
from pypowervm.tasks import client_storage as pvm_c_stor
from pypowervm.tasks import vfc_mapper as pvm_vfcm

from nova_powervm import conf as cfg
from nova_powervm.conf import powervm as pvm_cfg
from nova_powervm.virt.powervm import exception as exc
from nova_powervm.virt.powervm.i18n import _
from nova_powervm.virt.powervm.i18n import _LE
from nova_powervm.virt.powervm.i18n import _LI
from nova_powervm.virt.powervm.i18n import _LW
from nova_powervm.virt.powervm.volume import driver as v_driver

LOG = logging.getLogger(__name__)
CONF = cfg.CONF

WWPN_SYSTEM_METADATA_KEY = 'npiv_adpt_wwpns'
FABRIC_STATE_METADATA_KEY = 'fabric_state'
FS_UNMAPPED = 'unmapped'
FS_MIGRATING = 'migrating'
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

    @classmethod
    def min_xags(cls):
        """List of pypowervm XAGs needed to support this adapter."""
        # Storage are so physical FC ports are available
        # FC mapping is for the connections between VIOS and client VM
        return [pvm_const.XAG.VIO_FMAP, pvm_const.XAG.VIO_STOR]

    @classmethod
    def vol_type(cls):
        """The type of volume supported by this type."""
        return 'npiv'

    def _connect_volume(self, slot_mgr):
        """Connects the volume.

        :param slot_mgr: A NovaSlotManager.  Used to store/retrieve the client
                         slots used when a volume is attached to the VM
        """
        # Run the add for each fabric.
        for fabric in self._fabric_names():
            self._add_maps_for_fabric(fabric, slot_mgr)

    def _disconnect_volume(self, slot_mgr):
        """Disconnect the volume.

        :param slot_mgr: A NovaSlotManager.  Used to delete the client slots
                         used when a volume is detached from the VM
        """
        # We should only delete the NPIV mappings if we are running through a
        # VM deletion.  VM deletion occurs when the task state is deleting.
        # However, it can also occur during a 'roll-back' of the spawn.
        # Disconnect of the volumes will only be called during a roll back
        # of the spawn. We also want to check that the instance is on this
        # host. If it isn't then we can remove the mappings because this is
        # being called as the result of an evacuation clean up.
        if (self.instance.task_state not in TASK_STATES_FOR_DISCONNECT and
           self.instance.host in [None, CONF.host]):
            # NPIV should only remove the VFC mapping upon a destroy of the VM
            return

        # Run the disconnect for each fabric
        for fabric in self._fabric_names():
            self._remove_maps_for_fabric(fabric)

    def pre_live_migration_on_source(self, mig_data):
        """Performs pre live migration steps for the volume on the source host.

        Certain volume connectors may need to pass data from the source host
        to the target.  This may be required to determine how volumes connect
        through the Virtual I/O Servers.

        This method gives the volume connector an opportunity to update the
        mig_data (a dictionary) with any data that is needed for the target
        host during the pre-live migration step.

        Since the source host has no native pre_live_migration step, this is
        invoked from check_can_live_migrate_source in the overall live
        migration flow.

        :param mig_data: A dictionary that the method can update to include
                         data needed by the pre_live_migration_at_destination
                         method.
        """
        fabrics = self._fabric_names()
        vios_wraps = self.stg_ftsk.feed

        for fabric in fabrics:
            npiv_port_maps = self._get_fabric_meta(fabric)
            if not npiv_port_maps:
                continue

            client_slots = []
            for port_map in npiv_port_maps:
                vfc_map = pvm_vfcm.find_vios_for_vfc_wwpns(
                    vios_wraps, port_map[1].split())[1]
                client_slots.append(vfc_map.client_adapter.lpar_slot_num)

            # Set the client slots into the fabric data to pass to the
            # destination. Only strings can be stored.
            mig_data['src_npiv_fabric_slots_%s' % fabric] = (
                jsonutils.dumps(client_slots))

    def pre_live_migration_on_destination(self, mig_data):
        """Perform pre live migration steps for the volume on the target host.

        This method performs any pre live migration that is needed.

        Certain volume connectors may need to pass data from the source host
        to the target.  This may be required to determine how volumes connect
        through the Virtual I/O Servers.

        This method will be called after the pre_live_migration_on_source
        method.  The data from the pre_live call will be passed in via the
        mig_data.  This method should put its output into the dest_mig_data.

        :param mig_data: Dict of migration data for the destination server.
                         If the volume connector needs to provide
                         information to the live_migration command, it
                         should be added to this dictionary.
        """
        vios_wraps = self.stg_ftsk.feed

        # Need to first derive the port mappings that can be passed back
        # to the source system for the live migration call.  This tells
        # the source system what 'vfc mappings' to pass in on the live
        # migration command.
        for fabric in self._fabric_names():
            slots = jsonutils.loads(
                mig_data['src_npiv_fabric_slots_%s' % fabric])
            fabric_mapping = pvm_vfcm.build_migration_mappings_for_fabric(
                vios_wraps, self._fabric_ports(fabric), slots)
            mig_data['dest_npiv_fabric_mapping_%s' % fabric] = (
                jsonutils.dumps(fabric_mapping))
            # Reverse the vios wrapper so that the other fabric will get the
            # on the second vios.
            vios_wraps.reverse()

        # Collate all of the individual fabric mappings into a single element.
        full_map = []
        for key, value in mig_data.items():
            if key.startswith('dest_npiv_fabric_mapping_'):
                full_map.extend(jsonutils.loads(value))
        mig_data['vfc_lpm_mappings'] = jsonutils.dumps(full_map)

    def post_live_migration_at_destination(self, mig_vol_stor):
        """Perform post live migration steps for the volume on the target host.

        This method performs any post live migration that is needed.  Is not
        required to be implemented.

        :param mig_vol_stor: An unbounded dictionary that will be passed to
                             each volume adapter during the post live migration
                             call.  Adapters can store data in here that may
                             be used by subsequent volume adapters.
        """
        vios_wraps = self.stg_ftsk.feed

        # This method will run on the target host after the migration is
        # completed.  Right after this the instance.save is invoked from the
        # manager.  Given that, we need to update the order of the WWPNs.
        # The first WWPN is the one that is logged into the fabric and this
        # will now indicate that our WWPN is logged in.
        LOG.debug('Post live migrate volume store: %s' % mig_vol_stor,
                  instance=self.instance)
        for fabric in self._fabric_names():
            # We check the mig_vol_stor to see if this fabric has already been
            # flipped.  If so, we can continue.
            fabric_key = '%s_flipped' % fabric
            if mig_vol_stor.get(fabric_key, False):
                continue

            # Must not be flipped, so execute the flip
            npiv_port_maps = self._get_fabric_meta(fabric)
            new_port_maps = []
            for port_map in npiv_port_maps:
                # Flip the WPWNs
                c_wwpns = port_map[1].split()
                c_wwpns.reverse()
                LOG.debug('Flipping WWPNs, ports: %s wwpns: %s' %
                          (port_map, c_wwpns), instance=self.instance)
                # Get the new physical WWPN.
                vfc_map = pvm_vfcm.find_vios_for_vfc_wwpns(vios_wraps,
                                                           c_wwpns)[1]
                p_wwpn = vfc_map.backing_port.wwpn

                # Build the new map.
                new_map = (p_wwpn, " ".join(c_wwpns))
                new_port_maps.append(new_map)
            self._set_fabric_meta(fabric, new_port_maps)
            self._set_fabric_state(fabric, FS_INST_MAPPED)

            # Store that this fabric is now flipped.
            mig_vol_stor[fabric_key] = True

    def _is_initial_wwpn(self, fc_state, fabric):
        """Determines if the invocation to wwpns is for a general method.

        A 'general' method would be a spawn (with a volume) or a volume attach
        or detach.

        :param fc_state: The state of the fabric.
        :param fabric: The name of the fabric.
        :return: True if the invocation appears to be for a spawn/volume
                 action. False otherwise.
        """
        # Easy fabric state check.  If its a state other than unmapped, it
        # can't be an initial WWPN
        if fc_state != FS_UNMAPPED:
            return False

        # Easy state check.  This is important in case of a rollback failure.
        # If it is deleting or migrating, it definitely is not an initial WWPN
        if self.instance.task_state in [task_states.DELETING,
                                        task_states.MIGRATING]:
            return False

        # Next, we have to check the fabric metadata.  Having metadata
        # indicates that we have been on at least a single host.  However,
        # a VM could be rescheduled.  In that case, the 'physical WWPNs' won't
        # match.  So if any of the physical WWPNs are not supported by this
        # host, we know that it is 'initial' for this host.
        port_maps = self._get_fabric_meta(fabric)
        if len(port_maps) > 0 and self._hosts_wwpn(port_maps):
            return False

        # At this point, it should be correct.
        LOG.info(_LI("Instance %(inst)s has not yet defined a WWPN on "
                     "fabric %(fabric)s.  Appropriate WWPNs will be "
                     "generated."),
                 {'inst': self.instance.name, 'fabric': fabric})
        return True

    def _hosts_wwpn(self, port_maps):
        """Determines if this system hosts the port maps.

        Hosting the port map will be determined if one of the physical WWPNs
        is hosted by one of the VIOSes.

        :param port_maps: The list of port mappings for the given fabric.
        """
        vios_wraps = self.stg_ftsk.feed
        if port_maps:
            for port_map in port_maps:
                for vios_w in vios_wraps:
                    for pfc_port in vios_w.pfc_ports:
                        if pfc_port.wwpn == port_map[0]:
                            return True
        return False

    def _is_migration_wwpn(self, fc_state):
        """Determines if the WWPN call is occurring during a migration.

        This determines if it is on the target host.

        :param fc_state: The fabrics state.
        :return: True if the instance appears to be migrating to this host.
                 False otherwise.
        """
        return (fc_state == FS_INST_MAPPED and
                self.instance.host != CONF.host)

    def _configure_wwpns_for_migration(self, fabric):
        """Configures the WWPNs for a migration.

        During a NPIV migration, the WWPNs need to be flipped.  This is because
        the second WWPN is what will be logged in on the source system.  So by
        flipping them, we indicate that the 'second' wwpn is the new one to
        log in.

        Another way to think of it is, this code should always return the
        correct WWPNs for the system that the workload will be running on.

        This WWPNs invocation is done on the target server prior to the
        actual migration call.  It is used to build the volume connector.
        Therefore this code simply flips the ports around.

        :param fabric: The fabric to configure.
        :return: An updated port mapping.
        """
        if self._get_fabric_state(fabric) == FS_MIGRATING:
            # If the fabric is migrating, just return the existing port maps.
            # They've already been flipped.
            return self._get_fabric_meta(fabric)

        # When we migrate...flip the WWPNs around.  This is so the other
        # WWPN logs in on the target fabric.  If this code is hit, the flip
        # hasn't yet occurred (read as first volume on the instance).
        port_maps = self._get_fabric_meta(fabric)
        client_wwpns = []
        for port_map in port_maps:
            c_wwpns = port_map[1].split()
            c_wwpns.reverse()
            client_wwpns.extend(c_wwpns)

        # Now derive the mapping to the VIOS physical ports on this system
        # (the destination)
        port_mappings = pvm_vfcm.derive_npiv_map(
            self.stg_ftsk.feed, self._fabric_ports(fabric), client_wwpns)

        # This won't actually get saved by the process.  The instance save will
        # only occur after the 'post migration'.  But if there are multiple
        # volumes, their WWPNs calls will subsequently see the data saved
        # temporarily here, and therefore won't "double flip" the wwpns back
        # to the original.
        self._set_fabric_meta(fabric, port_mappings)
        self._set_fabric_state(fabric, FS_MIGRATING)

        # Return the mappings
        return port_mappings

    @lockutils.synchronized('npiv_wwpns')
    def wwpns(self):
        """Builds the WWPNs of the adapters that will connect the ports."""
        # Refresh the instance.  It could have been updated by a concurrent
        # call from another thread to get the wwpns.
        self.instance.refresh()
        vios_wraps = self.stg_ftsk.feed
        resp_wwpns = []

        # If this is the first time to query the WWPNs for the instance, we
        # need to generate a set of valid WWPNs.  Loop through the configured
        # FC fabrics and determine if these are new, part of a migration, or
        # were already configured.
        for fabric in self._fabric_names():
            fc_state = self._get_fabric_state(fabric)
            LOG.info(_LI("NPIV wwpns fabric state=%(st)s for "
                         "instance %(inst)s") %
                     {'st': fc_state, 'inst': self.instance.name})

            if self._is_initial_wwpn(fc_state, fabric):
                # Get a set of WWPNs that are globally unique from the system.
                v_wwpns = pvm_vfcm.build_wwpn_pair(
                    self.adapter, self.host_uuid,
                    pair_count=self._ports_per_fabric())

                # Derive the virtual to physical port mapping
                port_maps = pvm_vfcm.derive_npiv_map(
                    vios_wraps, self._fabric_ports(fabric), v_wwpns)

                # the fabric is mapped to the physical port) and the fabric
                # state.
                self._set_fabric_meta(fabric, port_maps)
                self._set_fabric_state(fabric, FS_UNMAPPED)
                self.instance.save()
            elif self._is_migration_wwpn(fc_state):
                # The migration process requires the 'second' wwpn from the
                # fabric to be used.
                port_maps = self._configure_wwpns_for_migration(fabric)
            else:
                # This specific fabric had been previously set.  Just pull
                # from the meta (as it is likely already mapped to the
                # instance)
                port_maps = self._get_fabric_meta(fabric)

            # Every loop through, we reverse the vios wrappers.  This is
            # done so that if Fabric A only has 1 port, it goes on the
            # first VIOS.  Then Fabric B would put its port on a different
            # VIOS.  This servers as a form of multi pathing (so that your
            # paths are not restricted to a single VIOS).
            vios_wraps.reverse()

            # Port map is set by either conditional, but may be set to None.
            # If not None, then add the WWPNs to the response.
            if port_maps is not None:
                for mapping in port_maps:
                    # Only add the first WWPN.  That is the one that will be
                    # logged into the fabric.
                    resp_wwpns.append(mapping[1].split()[0])

        # The return object needs to be a list for the volume connector.
        return resp_wwpns

    def _add_maps_for_fabric(self, fabric, slot_mgr):
        """Adds the vFC storage mappings to the VM for a given fabric.

        :param fabric: The fabric to add the mappings to.
        :param slot_mgr: A NovaSlotManager.  Used to store/retrieve the client
                         slots used when a volume is attached to the VM
        """
        npiv_port_maps = self._get_fabric_meta(fabric)
        vios_wraps = self.stg_ftsk.feed
        volume_id = self.connection_info['data']['volume_id']

        # This loop adds the maps from the appropriate VIOS to the client VM
        slot_ids = copy.deepcopy(slot_mgr.build_map.get_vfc_slots(
            fabric, len(npiv_port_maps)))
        for npiv_port_map in npiv_port_maps:
            vios_w = pvm_vfcm.find_vios_for_port_map(vios_wraps, npiv_port_map)
            if vios_w is None:
                LOG.error(_LE("Mappings were not able to find a proper VIOS. "
                              "The port mappings were %s."), npiv_port_maps)
                raise exc.VolumeAttachFailed(
                    volume_id=volume_id, instance_name=self.instance.name,
                    reason=_("Unable to find a Virtual I/O Server that "
                             "hosts the NPIV port map for the server."))
            ls = [LOG.info, _LI("Adding NPIV mapping for instance %(inst)s "
                                "for Virtual I/O Server %(vios)s."),
                  {'inst': self.instance.name, 'vios': vios_w.name}]

            # Add the subtask to add the specific map.
            slot_num = slot_ids.pop()
            self.stg_ftsk.wrapper_tasks[vios_w.uuid].add_functor_subtask(
                pvm_vfcm.add_map, self.host_uuid, self.vm_uuid, npiv_port_map,
                lpar_slot_num=slot_num, logspec=ls)

        # Store the client slot number for the NPIV mapping (for rebuild
        # scenarios)
        def set_vol_meta():
            vios_wraps = self.stg_ftsk.feed
            port_maps = self._get_fabric_meta(fabric)
            for port_map in port_maps:
                # The port map is [ 'phys_wwpn', 'client_wwpn1 client_wwpn2' ]
                # We only need one of the two client wwpns.
                vios_w = pvm_vfcm.find_vios_for_port_map(vios_wraps, port_map)
                c_wwpns = port_map[1].split()
                vfc_mapping = pvm_c_stor.c_wwpn_to_vfc_mapping(vios_w,
                                                               c_wwpns[0])

                # If there is no mapping, then don't add it.  It means that
                # the client WWPN is hosted on a different VIOS.
                if vfc_mapping is None:
                    continue

                # However, by this point we know that it is hosted on this
                # VIOS.  So the vfc_mapping will have the client adapter
                slot_mgr.register_vfc_mapping(vfc_mapping, fabric)

        self.stg_ftsk.add_post_execute(task.FunctorTask(
            set_vol_meta, name='fab_slot_%s_%s' % (fabric, volume_id)))

        # After all the mappings, make sure the fabric state is updated.
        def set_state():
            self._set_fabric_state(fabric, FS_INST_MAPPED)
        self.stg_ftsk.add_post_execute(task.FunctorTask(
            set_state, name='fab_%s_%s' % (fabric, volume_id)))

    def _remove_maps_for_fabric(self, fabric):
        """Removes the vFC storage mappings from the VM for a given fabric.

        :param fabric: The fabric to remove the mappings from.
        """
        npiv_port_maps = self._get_fabric_meta(fabric)
        if not npiv_port_maps:
            # If no mappings exist, exit out of the method.
            return

        vios_wraps = self.stg_ftsk.feed

        for npiv_port_map in npiv_port_maps:
            ls = [LOG.info, _LI("Removing a NPIV mapping for instance "
                                "%(inst)s for fabric %(fabric)s."),
                  {'inst': self.instance.name, 'fabric': fabric}]
            vios_w = pvm_vfcm.find_vios_for_port_map(vios_wraps, npiv_port_map)

            if vios_w is not None:
                # Add the subtask to remove the specific map
                task_wrapper = self.stg_ftsk.wrapper_tasks[vios_w.uuid]
                task_wrapper.add_functor_subtask(
                    pvm_vfcm.remove_maps, self.vm_uuid,
                    port_map=npiv_port_map, logspec=ls)
            else:
                LOG.warning(_LW("No storage connections found between the "
                                "Virtual I/O Servers and FC Fabric "
                                "%(fabric)s."), {'fabric': fabric})

    def host_name(self):
        """Derives the host name that should be used for the storage device.

        :return: The host name.
        """
        host = CONF.host if len(CONF.host) < 20 else CONF.host[:20]
        return host + '_' + self.instance.name

    def _set_fabric_state(self, fabric, state):
        """Sets the fabric state into the instance's system metadata.

        :param fabric: The name of the fabric
        :param state: state of the fabric which needs to be set

         Possible Valid States:
         FS_UNMAPPED: Initial state unmapped.
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

        The system metadata will store per-fabric port maps that link the
        physical ports to the virtual ports.  This is needed for the async
        nature between the wwpns call (get_volume_connector) and the
        connect_volume (spawn).

        :param fabric: The name of the fabric.
        :param port_map: The port map (as defined via the derive_npiv_map
                         pypowervm method).
        """

        # We will store the metadata in comma-separated strings with up to 4
        # three-token pairs. Each set of three comprises the Physical Port
        # WWPN followed by the two Virtual Port WWPNs:
        # Ex:
        # npiv_wwpn_adpt_A:
        #     "p_wwpn1,v_wwpn1,v_wwpn2,p_wwpn2,v_wwpn3,v_wwpn4,..."
        # npiv_wwpn_adpt_A_2:
        #     "p_wwpn5,v_wwpn9,vwwpn_10,p_wwpn6,..."

        meta_elems = []
        for p_wwpn, v_wwpn in port_map:
            meta_elems.append(p_wwpn)
            meta_elems.extend(v_wwpn.split())

        LOG.info(_LI("Fabric %(fabric)s wwpn metadata will be set to "
                     "%(meta)s for instance %(inst)s"),
                 {'fabric': fabric, 'meta': ",".join(meta_elems),
                  'inst': self.instance.name})

        # Clear out the original metadata.  We may be reducing the number of
        # keys (ex. reschedule) so we need to just delete what we had before
        # we add something new.
        meta_key_root = self._sys_meta_fabric_key(fabric)
        for key in tuple(self.instance.system_metadata.keys()):
            if key.startswith(meta_key_root):
                del self.instance.system_metadata[key]

        # Build up the mapping for the new keys.
        fabric_id_iter = 1
        meta_key = meta_key_root
        key_len = len(meta_key)

        for key in range(self._get_num_keys(port_map)):
            start_elem = 12 * (fabric_id_iter - 1)
            meta_value = ",".join(meta_elems[start_elem:start_elem + 12])
            self.instance.system_metadata[meta_key] = meta_value
            # If this is not the first time through, replace the end else cat
            if fabric_id_iter > 1:
                fabric_id_iter += 1
                meta_key = meta_key.replace(meta_key[key_len:],
                                            "_%s" % fabric_id_iter)
            else:
                fabric_id_iter += 1
                meta_key = meta_key + "_%s" % fabric_id_iter

    def _get_fabric_meta(self, fabric):
        """Gets the port map from the instance's system metadata.

        See _set_fabric_meta.

        :param fabric: The name of the fabric.
        :return: The port map (as defined via the derive_npiv_map pypowervm
                 method.
        """
        meta_key = self._sys_meta_fabric_key(fabric)

        if self.instance.system_metadata.get(meta_key) is None:
            # If no mappings exist, log a warning.
            LOG.warning(_LW("No NPIV mappings exist for instance %(inst)s on "
                            "fabric %(fabric)s.  May not have connected to "
                            "the fabric yet or fabric configuration was "
                            "recently modified."),
                        {'inst': self.instance.name, 'fabric': fabric})
            return []

        wwpns = self.instance.system_metadata[meta_key]
        key_len = len(meta_key)
        iterator = 2
        meta_key = meta_key + "_" + str(iterator)
        while self.instance.system_metadata.get(meta_key) is not None:
            meta_value = self.instance.system_metadata[meta_key]
            wwpns += "," + meta_value
            iterator += 1
            meta_key = meta_key.replace(meta_key[key_len:],
                                        "_" + str(iterator))

        wwpns = wwpns.split(",")

        # Rebuild the WWPNs into the natural structure.
        return [(p, ' '.join([v1, v2])) for p, v1, v2
                in zip(wwpns[::3], wwpns[1::3], wwpns[2::3])]

    def _sys_meta_fabric_key(self, fabric):
        """Returns the nova system metadata key for a given fabric."""
        return WWPN_SYSTEM_METADATA_KEY + '_' + fabric

    def _fabric_names(self):
        """Returns a list of the fabric names."""
        return pvm_cfg.NPIV_FABRIC_WWPNS.keys()

    def _fabric_ports(self, fabric_name):
        """Returns a list of WWPNs for the fabric's physical ports."""
        return pvm_cfg.NPIV_FABRIC_WWPNS[fabric_name]

    def _ports_per_fabric(self):
        """Returns the number of virtual ports to be used per fabric."""
        return CONF.powervm.ports_per_fabric

    def _get_num_keys(self, port_map):
        """Returns the number of keys we need to generate"""
        # Keys will have up to 4 mapping pairs so we determine based on that
        if len(port_map) % 4 > 0:
            return int(len(port_map) / 4 + 1)
        else:
            return int(len(port_map) / 4)
