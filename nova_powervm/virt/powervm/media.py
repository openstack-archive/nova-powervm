# Copyright 2015, 2018 IBM Corp.
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
from nova.api.metadata import base as instance_metadata
from nova.network import model as network_model
from nova.virt import configdrive
import os
import retrying
from taskflow import task

from oslo_log import log as logging
from oslo_utils import excutils

from pypowervm import const as pvm_const
from pypowervm.tasks import scsi_mapper as tsk_map
from pypowervm.tasks import storage as tsk_stg
from pypowervm.tasks import vopt as tsk_vopt
from pypowervm import util as pvm_util
from pypowervm.utils import transaction as pvm_tx
from pypowervm.wrappers import storage as pvm_stg
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm import conf as cfg
from nova_powervm.virt.powervm import vm


LOG = logging.getLogger(__name__)

CONF = cfg.CONF

_LLA_SUBNET = "fe80::/64"

CFG_DRV_PREFIX = "cfg_"
CFG_DRV_SUFFIX = ".iso"


class ConfigDrivePowerVM(object):

    def __init__(self, adapter):
        """Creates the config drive manager for PowerVM.

        :param adapter: The pypowervm adapter to communicate with the system.
        """
        self.adapter = adapter

        # Validate that the virtual optical exists
        self.vios_uuid, self.vg_uuid = tsk_vopt.validate_vopt_repo_exists(
            self.adapter,
            vopt_media_volume_group=CONF.powervm.vopt_media_volume_group,
            vopt_media_rep_size=CONF.powervm.vopt_media_rep_size)

    @staticmethod
    def _sanitize_network_info(network_info):
        """Will sanitize the network info for the config drive.

        Newer versions of cloud-init look at the vif type information in
        the network info and utilize it to determine what to do.  There are
        a limited number of vif types, and it seems to be built on the idea
        that the neutron vif type is the cloud init vif type (which is not
        quite right).

        This sanitizes the network info that gets passed into the config
        drive to work properly with cloud-inits.
        """
        network_info = copy.deepcopy(network_info)
        for vif in network_info:
            if vif.get('type') is not 'ovs':
                vif['type'] = 'vif'
        return network_info

    def _create_cfg_dr_iso(self, instance, injected_files, network_info,
                           admin_pass=None):
        """Creates an ISO file that contains the injected files.

        Used for config drive.

        :param instance: The VM instance from OpenStack.
        :param injected_files: A list of file paths that will be injected into
                               the ISO.
        :param network_info: The network_info from the nova spawn method.
        :param admin_pass: Optional password to inject for the VM.
        :return iso_path: The path to the ISO
        :return file_name: The file name for the ISO
        """
        LOG.info("Creating config drive.", instance=instance)
        extra_md = {}
        if admin_pass is not None:
            extra_md['admin_pass'] = admin_pass

        # Sanitize the vifs for the network config
        network_info = self._sanitize_network_info(network_info)

        inst_md = instance_metadata.InstanceMetadata(instance,
                                                     content=injected_files,
                                                     extra_md=extra_md,
                                                     network_info=network_info)

        # fix instance uuid to match uuid assigned to VM
        inst_md.uuid = vm.get_pvm_uuid(instance).lower()

        # Make sure the path exists.
        im_path = CONF.powervm.image_meta_local_path
        if not os.path.exists(im_path):
            os.mkdir(im_path)

        file_name = pvm_util.sanitize_file_name_for_api(
            instance.name, prefix=CFG_DRV_PREFIX, suffix=CFG_DRV_SUFFIX,
            max_len=pvm_const.MaxLen.VOPT_NAME)
        iso_path = os.path.join(im_path, file_name)
        with configdrive.ConfigDriveBuilder(instance_md=inst_md) as cdb:
            LOG.info("Config drive ISO building to path %(iso_path)s.",
                     {'iso_path': iso_path}, instance=instance)
            # In case, if there's an OSError related failure while
            # creating config drive, retry make drive operation.

            def _retry_on_oserror(exc):
                return isinstance(exc, OSError)

            @retrying.retry(retry_on_exception=_retry_on_oserror,
                            stop_max_attempt_number=2)
            def _make_cfg_drive(iso_path):
                cdb.make_drive(iso_path)
            try:
                _make_cfg_drive(iso_path)
                return iso_path, file_name
            except OSError:
                with excutils.save_and_reraise_exception(logger=LOG):
                    # If we get here, that means there's an exception during
                    # second attempt, log the same and fail the deploy
                    # operation.
                    LOG.exception("Config drive ISO could not be built.",
                                  instance=instance)

    def create_cfg_drv_vopt(self, instance, injected_files, network_info,
                            lpar_uuid, admin_pass=None, mgmt_cna=None,
                            stg_ftsk=None):
        """Creates the config drive virtual optical and attach to VM.

        :param instance: The VM instance from OpenStack.
        :param injected_files: A list of file paths that will be injected into
                               the ISO.
        :param network_info: The network_info from the nova spawn method.
        :param lpar_uuid: The UUID of the client LPAR
        :param admin_pass: (Optional) password to inject for the VM.
        :param mgmt_cna: (Optional) The management (RMC) CNA wrapper.
        :param stg_ftsk: (Optional) If provided, the tasks to create and attach
                         the Media to the VM will be deferred on to the
                         FeedTask passed in.  The execute can be done all in
                         one method (batched together).  If None (the default),
                         the media will be created and attached immediately.
        """
        # If there is a management client network adapter, then we should
        # convert that to a VIF and add it to the network info
        if mgmt_cna is not None and CONF.powervm.use_rmc_ipv6_scheme:
            network_info = copy.deepcopy(network_info)
            network_info.append(self._mgmt_cna_to_vif(mgmt_cna))

        iso_path, file_name = self._create_cfg_dr_iso(instance, injected_files,
                                                      network_info, admin_pass)

        # Upload the media
        file_size = os.path.getsize(iso_path)
        vopt, f_uuid = self._upload_vopt(iso_path, file_name, file_size)

        # Delete the media
        os.remove(iso_path)

        # Run the attach of the virtual optical
        self._attach_vopt(instance, lpar_uuid, vopt, stg_ftsk)

    def _attach_vopt(self, instance, lpar_uuid, vopt, stg_ftsk=None):
        """Will attach the vopt to the VIOS.

        If the stg_ftsk is provided, adds the mapping to the stg_ftsk, but
        won't attach until the stg_ftsk is independently executed.

        :param instance: The VM instance from OpenStack.
        :param lpar_uuid: The UUID of the client LPAR
        :param vopt: The virtual optical device to add.
        :param stg_ftsk: (Optional) If provided, the tasks to create the
                         storage mappings to connect the Media to the VM will
                         be deferred on to the FeedTask passed in.  The execute
                         can be done all in one method (batched together).  If
                         None (the default), the media will be attached
                         immediately.
        """
        # If no transaction manager, build locally so that we can run
        # immediately
        if stg_ftsk is None:
            wtsk = pvm_tx.WrapperTask('media_attach', pvm_vios.VIOS.getter(
                self.adapter, entry_uuid=self.vios_uuid,
                xag=[pvm_const.XAG.VIO_SMAP]))
        else:
            wtsk = stg_ftsk.wrapper_tasks[self.vios_uuid]

        # Define the function to build and add the mapping
        def add_func(vios_w):
            LOG.info("Adding config drive mapping to Virtual I/O Server "
                     "%(vios)s", {'vios': vios_w.name}, instance=instance)
            mapping = tsk_map.build_vscsi_mapping(None, vios_w,
                                                  lpar_uuid, vopt)
            return tsk_map.add_map(vios_w, mapping)

        wtsk.add_functor_subtask(add_func)

        # If built locally, then execute
        if stg_ftsk is None:
            wtsk.execute()

    def _mgmt_cna_to_vif(self, cna):
        """Converts the mgmt CNA to VIF format for network injection."""
        # See IEFT RFC 4291 appendix A for information on this algorithm
        mac = vm.norm_mac(cna.mac)
        ipv6_link_local = self._mac_to_link_local(mac)

        subnet = network_model.Subnet(
            version=6, cidr=_LLA_SUBNET,
            ips=[network_model.FixedIP(address=ipv6_link_local)])
        network = network_model.Network(id='mgmt', subnets=[subnet],
                                        injected='yes')
        return network_model.VIF(id='mgmt_vif', address=mac,
                                 network=network)

    @staticmethod
    def _mac_to_link_local(mac):
        # Convert the address to IPv6.  The first step is to separate out the
        # mac address
        splits = mac.split(':')

        # Insert into the middle the key ff:fe
        splits.insert(3, 'ff')
        splits.insert(4, 'fe')

        # Do the bit flip on the first octet.
        splits[0] = "%.2x" % (int(splits[0], 16) ^ 0b00000010)

        # Convert to the IPv6 link local format.  The prefix is fe80::.  Join
        # the hexes together at every other digit.
        ll = ['fe80:']
        ll.extend([splits[x] + splits[x + 1]
                   for x in range(0, len(splits), 2)])
        return ':'.join(ll)

    def _upload_vopt(self, iso_path, file_name, file_size):
        with open(iso_path, 'rb') as d_stream:
            return tsk_stg.upload_vopt(self.adapter, self.vios_uuid, d_stream,
                                       file_name, file_size)

    def dlt_vopt(self, lpar_uuid, stg_ftsk=None, remove_mappings=True):
        """Deletes the virtual optical and scsi mappings for a VM.

        :param lpar_uuid: The pypowervm UUID of the LPAR to remove.
        :param stg_ftsk: (Optional) A FeedTask. If provided, the actions to
                         modify the storage will be added as batched functions
                         onto the FeedTask.  If not provided (the default) the
                         operation to delete the vOpt will execute immediately.
        :param remove_mappings: (Optional, Default: True) If set to true, will
                                remove the SCSI mappings as part of the
                                operation.  If false, will leave the mapping
                                but detach the storage from it.  If the VM is
                                running, it may be necessary to do the latter
                                as some operating systems will not allow the
                                removal.
        """
        # If no transaction manager, build locally so that we can run
        # immediately
        if stg_ftsk is None:
            built_stg_ftsk = True
            vio_w = pvm_vios.VIOS.get(self.adapter, root_id=self.vios_uuid,
                                      xag=[pvm_const.XAG.VIO_SMAP])
            stg_ftsk = pvm_tx.FeedTask('media_detach', [vio_w])
        else:
            built_stg_ftsk = False

        # Run the remove maps method.
        self.add_dlt_vopt_tasks(lpar_uuid, stg_ftsk,
                                remove_mappings=remove_mappings)

        # If built locally, then execute
        if built_stg_ftsk:
            stg_ftsk.execute()

    def add_dlt_vopt_tasks(self, lpar_uuid, stg_ftsk, remove_mappings=True):
        """Deletes the virtual optical and (optionally) scsi mappings for a VM.

        :param lpar_uuid: The pypowervm UUID of the LPAR whose vopt is to be
                          removed.
        :param stg_ftsk: A FeedTask handling storage I/O.  The task to remove
                         the mappings and media from the VM will be deferred on
                         to the FeedTask passed in. The execute can be done all
                         in one method (batched together).  No updates are
                         actually made here; they are simply added to the
                         FeedTask.
        :param remove_mappings: (Optional, Default: True) If set to true, will
                                remove the SCSI mappings as part of the
                                operation.  If false, will leave the mapping
                                but detach the storage from it.  If the VM is
                                running, it may be necessary to do the latter
                                as some operating systems will not allow the
                                removal.
        """
        # The function to find the VOpt
        match_func = tsk_map.gen_match_func(pvm_stg.VOptMedia)

        def rm_vopt_mapping(vios_w):
            return tsk_map.remove_maps(vios_w, lpar_uuid,
                                       match_func=match_func)

        def detach_vopt_from_map(vios_w):
            return tsk_map.detach_storage(vios_w, lpar_uuid,
                                          match_func=match_func)

        # Add a function to remove the map or detach the vopt
        stg_ftsk.wrapper_tasks[self.vios_uuid].add_functor_subtask(
            rm_vopt_mapping if remove_mappings else detach_vopt_from_map)

        # Find the vOpt device (before the remove is done) so that it can be
        # removed.
        partition_id = vm.get_vm_id(self.adapter, lpar_uuid)
        media_mappings = tsk_map.find_maps(
            stg_ftsk.get_wrapper(self.vios_uuid).scsi_mappings,
            client_lpar_id=partition_id, match_func=match_func)
        media_elems = [x.backing_storage for x in media_mappings]

        def rm_vopt():
            LOG.info("Removing virtual optical for VM with UUID %s.",
                     lpar_uuid)
            vg_wrap = pvm_stg.VG.get(self.adapter, uuid=self.vg_uuid,
                                     parent_type=pvm_vios.VIOS,
                                     parent_uuid=self.vios_uuid)
            tsk_stg.rm_vg_storage(vg_wrap, vopts=media_elems)

        # Don't add this task if there is no media to delete (eg. config drive)
        if media_elems:
            stg_ftsk.add_post_execute(task.FunctorTask(rm_vopt))
