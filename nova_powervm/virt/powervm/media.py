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

import abc
from nova.api.metadata import base as instance_metadata
from nova.i18n import _LE
from nova.i18n import _LI
from nova.openstack.common import log as logging
from nova.virt import configdrive
import os

from oslo.config import cfg

from pypowervm.jobs import upload_lv
from pypowervm.wrappers import constants as pvmc
from pypowervm.wrappers import virtual_io_server as vios_w
from pypowervm.wrappers import volume_group as vg

import six

LOG = logging.getLogger(__name__)

CONF = cfg.CONF


@six.add_metaclass(abc.ABCMeta)
class AbstractMediaException(Exception):
    def __init__(self, **kwargs):
        msg = self.msg_fmt % kwargs
        super(AbstractMediaException, self).__init__(msg)


class NoMediaRepoVolumeGroupFound(AbstractMediaException):
    msg_fmt = _LE('Unable to locate the volume group %(vol_grp)s to store the '
                  'virtual optical media within.  Since it is not rootvg, the '
                  'volume group must be pre-created on the VIOS.')


class ConfigDrivePowerVM(object):

    def __init__(self, adapter, host_uuid, vios_uuid):
        """Creates the config drive manager for PowerVM.

        :param adapter: The pypowervm adapter to communicate with the system.
        :param host_uuid: The UUID of the host system.
        :param vios_uuid: The VIOS UUID that contains the media repo.
        """
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.vios_uuid = vios_uuid
        self.vg_uuid = self._validate_vopt_vg()

    def _create_cfg_dr_iso(self, instance, injected_files, network_info,
                           admin_pass=None):
        """Creates an ISO file that contains the injected files.  Used for
        config drive.

        :param instance: The VM instance from OpenStack.
        :param injected_files: A list of file paths that will be injected into
                               the ISO.
        :param network_info: The network_info from the nova spawn method.
        :param admin_password: Optional password to inject for the VM.
        :return iso_path: The path to the ISO
        :return file_name: The file name for the ISO
        """
        LOG.info(_LI("Creating config drive for instance: %s") % instance.name)
        extra_md = {}
        if admin_pass is not None:
            extra_md['admin_pass'] = admin_pass

        inst_md = instance_metadata.InstanceMetadata(instance,
                                                     content=injected_files,
                                                     extra_md=extra_md,
                                                     network_info=network_info)

        # Make sure the path exists.
        if not os.path.exists(CONF.image_meta_local_path):
            os.mkdir(CONF.image_meta_local_path)

        file_name = '%s_config.iso' % instance.name.replace('-', '_')
        iso_path = os.path.join(CONF.image_meta_local_path, file_name)
        with configdrive.ConfigDriveBuilder(instance_md=inst_md) as cdb:
            LOG.info(_LI("Config drive ISO being built for instance %(inst)s "
                         "building to path %(iso_path)s.") %
                     {'inst': instance.name, 'iso_path': iso_path})
            cdb.make_drive(iso_path)
            return iso_path, file_name

    def create_cfg_drv_vopt(self, instance, injected_files, network_info,
                            lpar_uuid, admin_pass=None):
        """Creates the config drive virtual optical.  Does not attach to VM.

        :param instance: The VM instance from OpenStack.
        :param injected_files: A list of file paths that will be injected into
                               the ISO.
        :param network_info: The network_info from the nova spawn method.
        :param lpar_uuid: The UUID of the client LPAR
        :param admin_pass: Optional password to inject for the VM.
        """
        iso_path, file_name = self._create_cfg_dr_iso(instance, injected_files,
                                                      network_info, admin_pass)

        # Upload the media
        file_size = os.path.getsize(iso_path)
        self._upload_lv(iso_path, file_name, file_size)

        # Delete the media
        os.remove(iso_path)

        # Now that it is uploaded, create the vSCSI mappings that link this to
        # the VM.  Don't run the upload as these are batched in a single call
        # to the VIOS later.
        elem = vios_w.crt_scsi_map_to_vopt(self.adapter, self.host_uuid,
                                           lpar_uuid, file_name)
        return vios_w.VirtualSCSIMapping(elem)

    def _upload_lv(self, iso_path, file_name, file_size):
        with open(iso_path, 'rb') as d_stream:
            upload_lv.upload_vopt(self.adapter, self.vios_uuid, d_stream,
                                  file_name, file_size)

    def _validate_vopt_vg(self):
        """Will ensure that the virtual optical media repository exists.

        This method will be expensive the first time it is run.  Should be
        quick on subsequent restarts.  Should be called on startup.

        :return vg_uuid: The Volume Group UUID holding the media repo.
        """
        resp = self.adapter.read(pvmc.VIOS, self.vios_uuid, pvmc.VOL_GROUP)
        found_vg = None
        for vg_entry in resp.feed.entries:
            vol_grp = vg.VolumeGroup(vg_entry)
            if vol_grp.name == CONF.vopt_media_volume_group:
                found_vg = vol_grp
                break

        if found_vg is None:
            if CONF.vopt_media_volume_group == 'rootvg':
                # If left at the default of rootvg, we should create it.
                # TODO(IBM) Need to implement.  Need implementation in
                # pypowervm api.
                raise NoMediaRepoVolumeGroupFound(
                    vol_grp=CONF.vopt_media_volume_group)
            else:
                raise NoMediaRepoVolumeGroupFound(
                    vol_grp=CONF.vopt_media_volume_group)

        # Ensure that there is a virtual optical media repository within it.
        vmedia_repos = found_vg.get_vmedia_repos()
        if len(vmedia_repos) == 0:
            vopt_repo = vg.crt_vmedia_repo('vopt',
                                           str(CONF.vopt_media_rep_size))
            vmedia_repos = [vg.VirtualMediaRepository(vopt_repo)]
            # TODO(IBM) This fails because its appending at the end...
            found_vg.set_vmedia_repos(vmedia_repos)
            self.adapter.update(found_vg._entry.element, resp.headers['etag'],
                                pvmc.VIOS, self.vios_uuid, pvmc.VOL_GROUP,
                                found_vg.uuid)

        return found_vg.uuid
