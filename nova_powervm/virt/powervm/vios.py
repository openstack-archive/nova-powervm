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

from oslo.config import cfg
import six

from nova.i18n import _LE
from nova.openstack.common import log as logging
from pypowervm import exceptions as pvm_exc
from pypowervm.wrappers import constants as pvm_consts
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
        resp = adapter.read(pvm_consts.VIOS,
                            suffix_type='search',
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
        resp = adapter.read(pvm_consts.VIOS, root_id=vios_uuid)
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


def add_vscsi_mapping(adapter, vios_uuid, vios_name, scsi_map):
    # Get the VIOS Entry
    vios_entry, etag = get_vios_entry(adapter, vios_uuid, vios_name)
    # Wrap the entry
    vios_wrap = pvm_vios.VirtualIOServer(vios_entry)
    # Pull the current mappings
    cur_mappings = vios_wrap.get_scsi_mappings()
    # Add the new mapping to the end
    cur_mappings.append(pvm_vios.VirtualSCSIMapping(scsi_map))
    vios_wrap.set_scsi_mappings(cur_mappings)
    # Write it back to the VIOS
    adapter.update(vios_wrap._entry.element, etag,
                   pvm_consts.VIOS, vios_uuid, xag=None)
