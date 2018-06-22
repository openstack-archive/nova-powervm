# Copyright IBM Corp. and contributors
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
from taskflow import task

from nova_powervm import conf as cfg
from nova_powervm.virt.powervm import exception as p_exc
from nova_powervm.virt.powervm.i18n import _
from nova_powervm.virt.powervm import vm

from pypowervm import const as pvm_const
from pypowervm.tasks import client_storage as pvm_c_stor
from pypowervm.tasks import hdisk
from pypowervm.tasks import scsi_mapper as tsk_map
from pypowervm.utils import transaction as tx
from pypowervm.wrappers import storage as pvm_stor
from pypowervm.wrappers import virtual_io_server as pvm_vios


CONF = cfg.CONF
LOG = logging.getLogger(__name__)
UDID_KEY = 'target_UDID'
DEVNAME_KEY = 'target_devname'


class VscsiVolumeAdapter(object):
    """VscsiVolumeAdapter that connects a Cinder volume to a VM.

    This volume adapter is a generic adapter for volume types that use PowerVM
    vSCSI to host the volume to the VM.
    """

    def _connect_volume(self, slot_mgr):
        """Connects the volume.

        :param connect_volume_to_vio: Function to connect a volume to the vio.
                                      :param vios_w: Vios wrapper.
                                      :return: True if mapping was created.
        :param slot_mgr: A NovaSlotManager.  Used to delete the client slots
                         used when a volume is detached from the VM
        """

        # Its about to get weird.  The transaction manager has a list of
        # VIOSes.  We could use those, but they only have SCSI mappings (by
        # design).  They do not have storage (super expensive).
        #
        # We need the storage xag when we are determining which mappings to
        # add to the system.  But we don't want to tie it to the stg_ftsk.  If
        # we do, every retry, every etag gather, etc... takes MUCH longer.
        #
        # So we get the VIOSes with the storage xag here, separately, to save
        # the stg_ftsk from potentially having to run it multiple times.
        connect_ftsk = tx.FeedTask(
            'connect_volume_to_vio', pvm_vios.VIOS.getter(
                self.adapter, xag=[pvm_const.XAG.VIO_STOR,
                                   pvm_const.XAG.VIO_SMAP]))

        # Find valid hdisks and map to VM.
        connect_ftsk.add_functor_subtask(
            self._connect_volume_to_vio, slot_mgr, provides='vio_modified',
            flag_update=False)

        ret = connect_ftsk.execute()

        # Check the number of VIOSes
        vioses_modified = 0
        for result in ret['wrapper_task_rets'].values():
            if result['vio_modified']:
                vioses_modified += 1

        partition_id = vm.get_vm_id(self.adapter, self.vm_uuid)

        # Update the slot information
        def set_slot_info():
            vios_wraps = self.stg_ftsk.feed
            for vios_w in vios_wraps:
                scsi_map = pvm_c_stor.udid_to_scsi_mapping(
                    vios_w, self._get_udid(), partition_id)
                if not scsi_map:
                    continue
                slot_mgr.register_vscsi_mapping(scsi_map)

        self._validate_vios_on_connection(vioses_modified)
        self.stg_ftsk.add_post_execute(task.FunctorTask(
            set_slot_info, name='hdisk_slot_%s' % self._get_udid()))

    def _validate_vios_on_connection(self, num_vioses_found):
        """Validates that the correct number of VIOSes were discovered.

        Certain environments may have redundancy requirements.  For PowerVM
        this is achieved by having multiple Virtual I/O Servers.  This method
        will check to ensure that the operator's requirements for redundancy
        have been met.  If not, a specific error message will be raised.

        :param num_vioses_found: The number of VIOSes the hdisk was found on.
        """
        # Is valid as long as the vios count exceeds the conf value.
        if num_vioses_found >= CONF.powervm.vscsi_vios_connections_required:
            return

        # Should have a custom message based on zero or 'some but not enough'
        # I/O Servers.
        if num_vioses_found == 0:
            msg = (_('Failed to discover valid hdisk on any Virtual I/O '
                     'Server for volume %(volume_id)s.') %
                   {'volume_id': self.volume_id})
        else:
            msg = (_('Failed to discover the hdisk on the required number of '
                     'Virtual I/O Servers.  Volume %(volume_id)s required '
                     '%(vios_req)d Virtual I/O Servers, but the disk was only '
                     'found on %(vios_act)d Virtual I/O Servers.') %
                   {'volume_id': self.volume_id, 'vios_act': num_vioses_found,
                    'vios_req': CONF.powervm.vscsi_vios_connections_required})
        ex_args = {'volume_id': self.volume_id, 'reason': msg,
                   'instance_name': self.instance.name}
        raise p_exc.VolumeAttachFailed(**ex_args)

    def _add_append_mapping(self, vios_uuid, device_name, lpar_slot_num=None,
                            lua=None, target_name=None, udid=None,
                            tag=None):
        """Update the stg_ftsk to append the mapping to the VIOS.

        :param vios_uuid: The UUID of the vios for the pypowervm adapter.
        :param device_name: The hdisk device name.
        :param lpar_slot_num: (Optional, Default:None) If specified, the client
                              lpar slot number to use on the mapping.  If left
                              as None, it will use the next available slot
                              number.
        :param lua: (Optional.  Default: None) Logical Unit Address to set on
                    the TargetDevice.  If None, the LUA will be assigned by the
                    server.  Should be specified for all of the VSCSIMappings
                    for a particular bus, or none of them.
        :param target_name: (Optional.  Default: None) Name to set on the
                            TargetDevice.  If None, it will be assigned by the
                            server.
        :param udid: (Optional.  Default: None) Universal Disk IDentifier of
                     the physical volume to attach.  Used to resolve name
                     conflicts.
        :param tag: String tag to set on the physical volume.
        """
        def add_func(vios_w):
            LOG.info("Adding vSCSI mapping to Physical Volume %(dev)s on "
                     "vios %(vios)s.",
                     {'dev': device_name, 'vios': vios_w.name},
                     instance=self.instance)
            pv = pvm_stor.PV.bld(self.adapter, device_name, udid=udid,
                                 tag=tag)
            v_map = tsk_map.build_vscsi_mapping(
                self.host_uuid, vios_w, self.vm_uuid, pv,
                lpar_slot_num=lpar_slot_num, lua=lua, target_name=target_name)
            return tsk_map.add_map(vios_w, v_map)
        self.stg_ftsk.wrapper_tasks[vios_uuid].add_functor_subtask(add_func)

    def _get_udid(self):
        """This method will return the hdisk udid stored in connection_info.

        :return: The target_udid associated with the hdisk
        """
        try:
            return self.connection_info['data'][UDID_KEY]
        except (KeyError, ValueError):
            # It's common to lose our specific data in the BDM.  The connection
            # information can be 'refreshed' by operations like LPM and resize
            LOG.info('Failed to retrieve target_UDID key from BDM for volume '
                     'id %s', self.volume_id, instance=self.instance)
            return None

    def _set_udid(self, udid):
        """This method will set the hdisk udid in the connection_info.

        :param udid: The hdisk target_udid to be stored in system_metadata
        """
        self.connection_info['data'][UDID_KEY] = udid

    def _add_remove_mapping(self, vm_uuid, vios_uuid, device_name, slot_mgr):
        """Adds a subtask to remove the storage mapping.

        :param vm_uuid: The UUID of the VM instance
        :param vios_uuid: The UUID of the vios for the pypowervm adapter.
        :param device_name: The The hdisk device name.
        :param slot_mgr: A NovaSlotManager.  Used to delete the client slots
                         used when a volume is detached from the VM.
        """
        def rm_func(vios_w):
            LOG.info("Removing vSCSI mapping from physical volume %(dev)s "
                     "on vios %(vios)s",
                     {'dev': device_name, 'vios': vios_w.name},
                     instance=self.instance)
            removed_maps = tsk_map.remove_maps(
                vios_w, vm_uuid,
                tsk_map.gen_match_func(pvm_stor.PV, names=[device_name]))
            for rm_map in removed_maps:
                slot_mgr.drop_vscsi_mapping(rm_map)
            return removed_maps
        self.stg_ftsk.wrapper_tasks[vios_uuid].add_functor_subtask(rm_func)

    def _add_remove_hdisk(self, vio_wrap, device_name,
                          stg_ftsk=None):
        """Adds a post-mapping task to remove the hdisk from the VIOS.

        This removal is only done after the mapping updates have completed.
        This method is also used during migration to remove hdisks that remain
        on the source host after the VM is migrated to the destination.

        :param vio_wrap: The Virtual I/O Server wrapper to remove the disk
                         from.
        :param device_name: The hdisk name to remove.
        :param stg_ftsk: The feed task to add to. If None, then self.stg_ftsk
        """
        def rm_hdisk():
            LOG.info("Removing hdisk %(hdisk)s from Virtual I/O Server "
                     "%(vios)s", {'hdisk': device_name, 'vios': vio_wrap.name},
                     instance=self.instance)
            try:
                # Attempt to remove the hDisk
                hdisk.remove_hdisk(self.adapter, CONF.host, device_name,
                                   vio_wrap.uuid)
            except Exception:
                # If there is a failure, log it, but don't stop the process
                LOG.exception("There was an error removing the hdisk "
                              "%(disk)s from Virtual I/O Server %(vios)s.",
                              {'disk': device_name, 'vios': vio_wrap.name},
                              instance=self.instance)

        # Check if there are not multiple mapping for the device
        if not self._check_host_mappings(vio_wrap, device_name):
            name = 'rm_hdisk_%s_%s' % (vio_wrap.name, device_name)
            stg_ftsk = stg_ftsk or self.stg_ftsk
            stg_ftsk.add_post_execute(task.FunctorTask(rm_hdisk, name=name))
        else:
            LOG.info("hdisk %(disk)s is not removed from Virtual I/O Server "
                     "%(vios)s because it has existing storage mappings",
                     {'disk': device_name, 'vios': vio_wrap.name},
                     instance=self.instance)

    def _check_host_mappings(self, vios_wrap, device_name):
        """Checks if the given hdisk has multiple mappings

        :param vio_wrap: The Virtual I/O Server wrapper to remove the disk
                         from.
        :param device_name: The hdisk name to remove.

        :return: True if there are multiple instances using the given hdisk
        """
        vios_scsi_mappings = next(v.scsi_mappings for v in self.stg_ftsk.feed
                                  if v.uuid == vios_wrap.uuid)
        mappings = tsk_map.find_maps(
            vios_scsi_mappings, None,
            tsk_map.gen_match_func(pvm_stor.PV, names=[device_name]))

        LOG.debug("%(num)d storage mapping(s) found for %(dev)s on VIOS "
                  "%(vios)s", {'num': len(mappings), 'dev': device_name,
                               'vios': vios_wrap.name}, instance=self.instance)
        # the mapping is still present as the task feed removes it later
        return len(mappings) > 1

    def _cleanup_volume(self, udid=None, devname=None):
        """Cleanup the hdisk associated with this udid."""

        if not udid and not devname:
            LOG.warning('Could not remove hdisk for volume %s', self.volume_id,
                        instance=self.instance)
            return

        LOG.info('Removing hdisk for udid: %s', udid, instance=self.instance)

        def find_hdisk_to_remove(vios_w):
            if devname is None:
                device_name = vios_w.hdisk_from_uuid(udid)
            else:
                device_name = devname
            if device_name is None:
                return
            LOG.info('Adding deferred task to remove %(hdisk)s from VIOS '
                     '%(vios)s.', {'hdisk': device_name, 'vios': vios_w.name},
                     instance=self.instance)
            self._add_remove_hdisk(vios_w, device_name,
                                   stg_ftsk=rmv_hdisk_ftsk)

        # Create a feed task to get the vios, find the hdisk and remove it.
        rmv_hdisk_ftsk = tx.FeedTask(
            'find_hdisk_to_remove', pvm_vios.VIOS.getter(
                self.adapter, xag=[pvm_const.XAG.VIO_STOR]))
        # Find vios hdisks for this udid to remove.
        rmv_hdisk_ftsk.add_functor_subtask(
            find_hdisk_to_remove, flag_update=False)
        rmv_hdisk_ftsk.execute()
