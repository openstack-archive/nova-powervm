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

from oslo_config import cfg
from oslo_log import log as logging
import six

from nova.i18n import _LE
from pypowervm import exceptions as pvm_exc
from pypowervm.wrappers import logical_partition as pvm_lpar
from pypowervm.wrappers import managed_system as pvm_ms
from pypowervm.wrappers import virtual_io_server as pvm_vios

vios_opts = [
    cfg.StrOpt('vios_name',
               default='',
               help='VIOS to use for I/O operations.')
]


LOG = logging.getLogger(__name__)
CONF = cfg.CONF
CONF.register_opts(vios_opts)


@six.add_metaclass(abc.ABCMeta)
class AbstractVIOSException(Exception):
    def __init__(self, **kwds):
        msg = self.msg_fmt % kwds
        super(AbstractVIOSException, self).__init__(msg)


class VIOSNotFound(AbstractVIOSException):
    msg_fmt = _LE('Unable to locate the Virtual I/O Server \'%(vios_name)s\''
                  ' for this operation.')


def get_vios_uuid(adapter, name):
    searchstring = "(PartitionName=='%s')" % name
    try:
        resp = adapter.read(pvm_vios.VIOS.schema_type, suffix_type='search',
                            suffix_parm=searchstring)
    except pvm_exc.Error as e:
        if e.response.status == 404:
            raise VIOSNotFound(vios_name=name)
        else:
            LOG.exception(e)
            raise
    except Exception as e:
        LOG.exception(e)
        raise

    entry = resp.feed.entries[0]
    uuid = entry.properties['id']
    return uuid


def get_vios_entry(adapter, vios_uuid, vios_name):
    try:
        resp = adapter.read(pvm_vios.VIOS.schema_type, root_id=vios_uuid)
    except pvm_exc.Error as e:
        if e.response.status == 404:
            raise VIOSNotFound(vios_name=vios_name)
        else:
            LOG.exception(e)
            raise
    except Exception as e:
        LOG.exception(e)
        raise

    return resp.entry, resp.headers['etag']


def get_physical_wwpns(adapter, ms_uuid):
    """Returns the WWPNs of the FC adapters across all VIOSes on system."""
    resp = adapter.read(pvm_ms.System.schema_type, root_id=ms_uuid,
                        child_type=pvm_vios.VIOS.schema_type)
    vios_feed = pvm_vios.VIOS.wrap(resp)
    wwpn_list = []
    for vios in vios_feed:
        wwpn_list.extend(vios.get_pfc_wwpns())
    return wwpn_list


def get_vscsi_mappings(adapter, lpar_uuid, vio_wrapper, mapping_type):
    """Returns a list of VSCSIMappings that pair to the instance.

    :param adapter: The pypowervm API Adapter.
    :param lpar_uuid: The lpar UUID that identifies which system to get the
                      storage mappings for.
    :param vio_wrapper: The VIOS pypowervm wrapper for the VIOS.  Should have
                        the mappings within it.
    :param mapping_type: The type of mapping to look for.  Typically
                         VOptMedia or VDisk
    :returns: A list of vSCSI Mappings (pypowervm wrapper) from the VIOS
              that are tied to the lpar, for the mapping type.
    """
    # Quick read of the partition ID.  Identifier between LPAR and VIOS
    partition_id = adapter.read(pvm_lpar.LPAR.schema_type, root_id=lpar_uuid,
                                suffix_type='quick',
                                suffix_parm='PartitionID').body

    # The return list is the Virtual SCSI Mapping wrappers
    vscsi_maps = []

    # Find all of the maps that match up to our partition
    for scsi_map in vio_wrapper.scsi_mappings:
        # If no client, it can't match us...
        if scsi_map.client_adapter is None:
            continue

        # If the ID doesn't match up, not a valid one.
        if scsi_map.client_adapter.lpar_id != partition_id:
            continue

        # Check to see if the mapping is to a vopt
        if (scsi_map.backing_storage is None or
                not isinstance(scsi_map.backing_storage, mapping_type)):
            continue

        vscsi_maps.append(scsi_map)
    return vscsi_maps


def add_vscsi_mapping(adapter, vios_uuid, vios_name, scsi_map):
    # Get the VIOS Entry
    vios_entry, etag = get_vios_entry(adapter, vios_uuid, vios_name)
    # Wrap the entry
    vios_wrap = pvm_vios.VIOS.wrap(vios_entry)
    # Add the new mapping to the end
    vios_wrap.scsi_mappings.append(scsi_map)
    # Write it back to the VIOS
    adapter.update(vios_wrap, etag, pvm_vios.VIOS.schema_type, vios_uuid,
                   xag=None)
