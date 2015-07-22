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

import copy
from nova.api.metadata import base as instance_metadata
from nova.i18n import _LI, _LW
from nova.network import model as network_model
from nova.virt import configdrive
import os

from oslo_config import cfg
from oslo_log import log as logging

from pypowervm import const as pvm_const
from pypowervm.tasks import scsi_mapper as tsk_map
from pypowervm.tasks import storage as tsk_stg
from pypowervm import util as pvm_util
from pypowervm.wrappers import base_partition as pvm_bp
from pypowervm.wrappers import managed_system as pvm_ms
from pypowervm.wrappers import storage as pvm_stg
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.virt.powervm import exception as npvmex
from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)

CONF = cfg.CONF


class ConfigDrivePowerVM(object):

    _cur_vios_uuid = None
    _cur_vios_name = None
    _cur_vg_uuid = None

    def __init__(self, adapter, host_uuid):
        """Creates the config drive manager for PowerVM.

        :param adapter: The pypowervm adapter to communicate with the system.
        :param host_uuid: The UUID of the host system.
        """
        self.adapter = adapter
        self.host_uuid = host_uuid

        # The validate will use the cached static variables for the VIOS info.
        # Once validate is done, set the class variables to the updated cache.
        self._validate_vopt_vg()

        self.vios_uuid = ConfigDrivePowerVM._cur_vios_uuid
        self.vios_name = ConfigDrivePowerVM._cur_vios_name
        self.vg_uuid = ConfigDrivePowerVM._cur_vg_uuid

    def _create_cfg_dr_iso(self, instance, injected_files, network_info,
                           admin_pass=None):
        """Creates an ISO file that contains the injected files.  Used for
        config drive.

        :param instance: The VM instance from OpenStack.
        :param injected_files: A list of file paths that will be injected into
                               the ISO.
        :param network_info: The network_info from the nova spawn method.
        :param admin_pass: Optional password to inject for the VM.
        :return iso_path: The path to the ISO
        :return file_name: The file name for the ISO
        """
        LOG.info(_LI("Creating config drive for instance: %s"), instance.name)
        extra_md = {}
        if admin_pass is not None:
            extra_md['admin_pass'] = admin_pass

        inst_md = instance_metadata.InstanceMetadata(instance,
                                                     content=injected_files,
                                                     extra_md=extra_md,
                                                     network_info=network_info)

        # Make sure the path exists.
        im_path = CONF.powervm.image_meta_local_path
        if not os.path.exists(im_path):
            os.mkdir(im_path)

        file_name = pvm_util.sanitize_file_name_for_api(
            instance.name, prefix='cfg_', suffix='.iso',
            max_len=pvm_const.MaxLen.VOPT_NAME)
        iso_path = os.path.join(im_path, file_name)
        with configdrive.ConfigDriveBuilder(instance_md=inst_md) as cdb:
            LOG.info(_LI("Config drive ISO being built for instance %(inst)s "
                         "building to path %(iso_path)s."),
                     {'inst': instance.name, 'iso_path': iso_path})
            cdb.make_drive(iso_path)
            return iso_path, file_name

    def create_cfg_drv_vopt(self, instance, injected_files, network_info,
                            lpar_uuid, admin_pass=None, mgmt_cna=None):
        """Creates the config drive virtual optical and attach to VM.

        :param instance: The VM instance from OpenStack.
        :param injected_files: A list of file paths that will be injected into
                               the ISO.
        :param network_info: The network_info from the nova spawn method.
        :param lpar_uuid: The UUID of the client LPAR
        :param admin_pass: (Optional) password to inject for the VM.
        :param mgmt_cna: (Optional) The management (RMC) CNA wrapper.
        """
        # If there is a management client network adapter, then we should
        # convert that to a VIF and add it to the network info
        if mgmt_cna is not None:
            network_info = copy.deepcopy(network_info)
            network_info.append(self._mgmt_cna_to_vif(mgmt_cna))

        iso_path, file_name = self._create_cfg_dr_iso(instance, injected_files,
                                                      network_info, admin_pass)

        # Upload the media
        file_size = os.path.getsize(iso_path)
        vopt, f_uuid = self._upload_vopt(iso_path, file_name, file_size)

        # Delete the media
        os.remove(iso_path)

        # Add the mapping to the virtual machine
        tsk_map.add_vscsi_mapping(self.host_uuid, self.vios_uuid, lpar_uuid,
                                  vopt)

    def _mgmt_cna_to_vif(self, cna):
        """Converts the mgmt CNA to VIF format for network injection."""
        subnet = network_model.Subnet(version='6', dhcp_server='link_local')
        network = network_model.Network(id='mgmt', subnets=[subnet],
                                        injected='yes')
        return network_model.VIF(id='mgmt_vif', address=vm.norm_mac(cna.mac),
                                 network=network)

    def _upload_vopt(self, iso_path, file_name, file_size):
        with open(iso_path, 'rb') as d_stream:
            return tsk_stg.upload_vopt(self.adapter, self.vios_uuid, d_stream,
                                       file_name, file_size)

    def _validate_vopt_vg(self):
        """Will ensure that the virtual optical media repository exists.

        This method will connect to one of the Virtual I/O Servers on the
        system and ensure that there is a root_vg that the optical media (which
        is temporary) exists.

        If the volume group on an I/O Server goes down (perhaps due to
        maintenance), the system will rescan to determine if there is another
        I/O Server that can host the request.

        The very first invocation may be expensive.  It may also be expensive
        to call if a Virtual I/O Server unexpectantly goes down.

        If there are no Virtual I/O Servers that can support the media, then
        an exception will be thrown.
        """

        # TODO(IBM) Add thread safety here in case two calls into this are
        # done at once.

        # If our static variables were set, then we should validate that the
        # repo is still running.  Otherwise, we need to reset the variables
        # (as it could be down for maintenance).
        if ConfigDrivePowerVM._cur_vg_uuid is not None:
            vio_uuid = ConfigDrivePowerVM._cur_vios_uuid
            vg_uuid = ConfigDrivePowerVM._cur_vg_uuid
            try:
                vg_resp = self.adapter.read(pvm_vios.VIOS.schema_type,
                                            vio_uuid, pvm_stg.VG.schema_type,
                                            vg_uuid)
                if vg_resp is not None:
                    return
            except Exception:
                pass

            LOG.log(_LI("An error occurred querying the virtual optical media "
                        "repository.  Attempting to re-establish connection "
                        "with a virtual optical media repository"))

        # If we're hitting this, either it's our first time booting up, or the
        # previously used Volume Group went offline (ex. VIOS went down for
        # maintenance).
        #
        # Since it doesn't matter which VIOS we use for the media repo, we
        # should query all Virtual I/O Servers and see if an appropriate
        # media repository exists.
        vios_resp = self.adapter.read(pvm_ms.System.schema_type,
                                      root_id=self.host_uuid,
                                      child_type=pvm_vios.VIOS.schema_type)
        vio_wraps = pvm_vios.VIOS.wrap(vios_resp)

        # First loop through the VIOSes to see if any have the right VG
        found_vg = None
        found_vios = None

        for vio_wrap in vio_wraps:
            # If the RMC state is not active, skip over to ensure we don't
            # timeout
            if vio_wrap.rmc_state != pvm_bp.RMCState.ACTIVE:
                continue

            try:
                vg_resp = self.adapter.read(pvm_vios.VIOS.schema_type,
                                            root_id=vio_wrap.uuid,
                                            child_type=pvm_stg.VG.schema_type)
                vg_wraps = pvm_stg.VG.wrap(vg_resp)
                for vg_wrap in vg_wraps:
                    if vg_wrap.name == CONF.powervm.vopt_media_volume_group:
                        found_vg = vg_wrap
                        found_vios = vio_wrap
                        break
            except Exception:
                LOG.warn(_LW('Unable to read volume groups for Virtual '
                             'I/O Server %s'), vio_wrap.name)
                pass

        # If we didn't find a volume group...raise the exception.  It should
        # default to being the rootvg, which all VIOSes will have.  Otherwise,
        # this is user specified, and if it was not found is a proper
        # exception path.
        if found_vg is None:
            raise npvmex.NoMediaRepoVolumeGroupFound(
                vol_grp=CONF.powervm.vopt_media_volume_group)

        # Ensure that there is a virtual optical media repository within it.
        if len(found_vg.vmedia_repos) == 0:
            vopt_repo = pvm_stg.VMediaRepos.bld(
                self.adapter, 'vopt', str(CONF.powervm.vopt_media_rep_size))
            found_vg.vmedia_repos = [vopt_repo]
            found_vg = found_vg.update()

        # At this point, we know that we've successfully set up the volume
        # group.  Save to the static class variables.
        ConfigDrivePowerVM._cur_vg_uuid = found_vg.uuid
        ConfigDrivePowerVM._cur_vios_uuid = found_vios.uuid
        ConfigDrivePowerVM._cur_vios_name = found_vios.name

    def dlt_vopt(self, lpar_uuid):
        """Deletes the virtual optical and scsi mappings for a VM."""
        partition_id = vm.get_vm_id(self.adapter, lpar_uuid)

        # Remove the SCSI mappings to all vOpt Media.  This returns the media
        # devices that were removed from the bus.
        vios, media_elems = tsk_map.remove_vopt_mapping(
            self.adapter, self.vios_uuid, partition_id)

        # Next delete the media from the volume group.  To do so, remove the
        # media from the volume group, which triggers a delete.
        vg_rsp = self.adapter.read(pvm_vios.VIOS.schema_type,
                                   root_id=self.vios_uuid,
                                   child_type=pvm_stg.VG.schema_type,
                                   child_id=self.vg_uuid)
        tsk_stg.rm_vg_storage(pvm_stg.VG.wrap(vg_rsp), vopts=media_elems)
