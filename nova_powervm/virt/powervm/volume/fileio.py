# Copyright 2017 IBM Corp.
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
import six

from nova_powervm import conf as cfg
from nova_powervm.virt.powervm.i18n import _LI
from nova_powervm.virt.powervm.volume import driver as v_driver
from oslo_log import log as logging
from pypowervm import const as pvm_const
from pypowervm.tasks import partition
from pypowervm.tasks import scsi_mapper as tsk_map
from pypowervm.wrappers import storage as pvm_stg

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


@six.add_metaclass(abc.ABCMeta)
class FileIOVolumeAdapter(v_driver.PowerVMVolumeAdapter):
    """Base class for connecting file based Cinder Volumes to PowerVM VMs."""

    @classmethod
    def min_xags(cls):
        return [pvm_const.XAG.VIO_SMAP]

    @abc.abstractmethod
    def _get_path(self):
        """Return the path to the file to connect."""
        pass

    def _connect_volume(self, slot_mgr):
        # Get the hosting UUID
        nl_vios_wrap = partition.get_mgmt_partition(self.adapter)
        vios_uuid = nl_vios_wrap.uuid

        # Get the File Path
        fio = pvm_stg.FileIO.bld(self.adapter, self._get_path())

        def add_func(vios_w):
            # If the vios doesn't match, just return
            if vios_w.uuid != vios_uuid:
                return None

            LOG.info(_LI("Adding logical volume disk connection between VM "
                         "%(vm)s and VIOS %(vios)s."),
                     {'vm': self.instance.name, 'vios': vios_w.name},
                     instance=self.instance)
            mapping = tsk_map.build_vscsi_mapping(
                self.host_uuid, vios_w, self.vm_uuid, fio)
            return tsk_map.add_map(vios_w, mapping)

        self.stg_ftsk.add_functor_subtask(add_func)

    def _disconnect_volume(self, slot_mgr):
        # Get the hosting UUID
        nl_vios_wrap = partition.get_mgmt_partition(self.adapter)
        vios_uuid = nl_vios_wrap.uuid

        # Build the match function
        match_func = tsk_map.gen_match_func(pvm_stg.FileIO,
                                            names=[self._get_path()])

        # Make sure the remove function will run within the transaction manager
        def rm_func(vios_w):
            # If the vios doesn't match, just return
            if vios_w.uuid != vios_uuid:
                return None

            LOG.info(_LI("Disconnecting instance %(inst)s from storage "
                         "disks."), {'inst': self.instance.name},
                     instance=self.instance)
            return tsk_map.remove_maps(vios_w, self.vm_uuid,
                                       match_func=match_func)

        self.stg_ftsk.add_functor_subtask(rm_func)

        # Find the disk directly.
        mappings = tsk_map.find_maps(nl_vios_wrap.scsi_mappings,
                                     client_lpar_id=self.vm_uuid,
                                     match_func=match_func)

        return [x.backing_storage for x in mappings]
