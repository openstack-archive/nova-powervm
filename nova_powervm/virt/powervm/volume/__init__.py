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


# Defines the various volume connectors that can be used.
from nova import exception
from oslo_utils import importutils

from nova_powervm import conf as cfg
from nova_powervm.virt.powervm.i18n import _

CONF = cfg.CONF

FC_STRATEGY_MAPPING = {
    'npiv': CONF.powervm.fc_npiv_adapter_api,
    'vscsi': CONF.powervm.fc_vscsi_adapter_api
}

_STATIC_VOLUME_MAPPINGS = {
    'iscsi': 'nova_powervm.virt.powervm.volume.iscsi.'
             'IscsiVolumeAdapter',
    'iser': 'nova_powervm.virt.powervm.volume.iscsi.'
             'IscsiVolumeAdapter',
    'local': 'nova_powervm.virt.powervm.volume.local.'
             'LocalVolumeAdapter',
    'nfs': 'nova_powervm.virt.powervm.volume.nfs.NFSVolumeAdapter',
    'gpfs': 'nova_powervm.virt.powervm.volume.gpfs.GPFSVolumeAdapter',
    'rbd': 'nova_powervm.virt.powervm.volume.rbd.RBDVolumeAdapter',
}


def build_volume_driver(adapter, host_uuid, instance, conn_info,
                        stg_ftsk=None):
    vol_cls = get_volume_class(conn_info.get('driver_volume_type'))

    return vol_cls(adapter, host_uuid, instance, conn_info,
                   stg_ftsk=stg_ftsk)


def get_volume_class(drv_type):
    if drv_type in _STATIC_VOLUME_MAPPINGS:
        class_type = _STATIC_VOLUME_MAPPINGS[drv_type]
    elif drv_type == 'fibre_channel':
        class_type = (FC_STRATEGY_MAPPING[
            CONF.powervm.fc_attach_strategy.lower()])
    else:
        failure_reason = _("Invalid connection type of %s") % drv_type
        raise exception.InvalidVolume(reason=failure_reason)

    return importutils.import_class(class_type)


def get_hostname_for_volume(instance):
    if CONF.powervm.fc_attach_strategy.lower() == 'npiv':
        # Tie the host name to the instance, as it will be represented in
        # the backend as a full server.
        host = CONF.host if len(CONF.host) < 20 else CONF.host[:20]
        return host + '_' + instance.name
    else:
        return CONF.host


def get_wwpns_for_volume_connector(adapter, host_uuid, instance):
    # WWPNs are derived from the FC connector.  Pass in a fake connection info
    # to trick it into thinking it FC
    fake_fc_conn_info = {'driver_volume_type': 'fibre_channel'}
    fc_vol_drv = build_volume_driver(adapter, host_uuid, instance,
                                     fake_fc_conn_info)
    return fc_vol_drv.wwpns()
