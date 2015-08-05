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

from nova.i18n import _, _LI, _LW, _LE

from oslo_config import cfg
from oslo_log import log as logging

from nova_powervm.virt.powervm import vios
from nova_powervm.virt.powervm import vm
from nova_powervm.virt.powervm.volume import driver as v_driver

import pypowervm.exceptions as pexc
from pypowervm.tasks import hdisk
from pypowervm.tasks import scsi_mapper as tsk_map
from pypowervm.wrappers import storage as pvm_stor
from pypowervm.wrappers import virtual_io_server as pvm_vios

import six

CONF = cfg.CONF
LOG = logging.getLogger(__name__)

_XAGS = [pvm_vios.VIOS.xags.STORAGE]


def _build_udid_key(vios_uuid, volume_id):
    """This method will build the udid dictionary key.

    :param vios_uuid: The UUID of the vios for the pypowervm adapter.
    :param volume_id: The lun volume id
    :return: The udid dictionary key
    """
    return vios_uuid + volume_id


class VscsiVolumeAdapter(v_driver.FibreChannelVolumeAdapter):
    """The vSCSI implementation of the Volume Adapter.

    vSCSI is the internal mechanism to link a given hdisk on the Virtual
    I/O Server to a Virtual Machine.  This volume driver will take the
    information from the driver and link it to a given virtual machine.
    """

    def __init__(self, adapter, host_uuid, instance, connection_info):
        """Initializes the vSCSI Volume Adapter.

        :param adapter: The pypowervm adapter.
        :param host_uuid: The pypowervm UUID of the host.
        :param instance: The nova instance that the volume should connect to.
        :param connection_info: Comes from the BDM.
        """
        super(VscsiVolumeAdapter, self).__init__(adapter, host_uuid, instance,
                                                 connection_info)
        self._pfc_wwpns = None
        self._vioses_modified = []

    def connect_volume(self):
        """Connects the volume."""

        # Get the initiators
        volume_id = self.connection_info['data']['volume_id']
        device_name = None
        self._vioses_modified = []

        # Get VIOS feed
        vios_feed = vios.get_active_vioses(self.adapter, self.host_uuid,
                                           xag=_XAGS)

        # Iterate through host vios list to find valid hdisks and map to VM.
        for vio_wrap in vios_feed:
            # Get the initiatior WWPNs, targets and Lun for the given VIOS.
            vio_wwpns, t_wwpns, lun = self._get_hdisk_itls(vio_wrap)

            # Build the ITL map and discover the hdisks on the Virtual I/O
            # Server (if any).
            itls = hdisk.build_itls(vio_wwpns, t_wwpns, lun)
            if len(itls) == 0:
                LOG.debug('No ITLs for VIOS %(vios)s for volume %(volume_id)s.'
                          % {'vios': vio_wrap.name, 'volume_id': volume_id})
                continue

            status, device_name, udid = hdisk.discover_hdisk(
                self.adapter, vio_wrap.uuid, itls)
            if device_name is not None and status in [
                    hdisk.LUAStatus.DEVICE_AVAILABLE,
                    hdisk.LUAStatus.FOUND_ITL_ERR]:
                LOG.info(_LI('Discovered %(hdisk)s on vios %(vios)s for '
                         'volume %(volume_id)s. Status code: %(status)s.'),
                         {'hdisk': device_name, 'vios': vio_wrap.name,
                          'volume_id': volume_id, 'status': str(status)})

                # Found a hdisk on this Virtual I/O Server.  Add a vSCSI
                # mapping to the Virtual Machine so that it can use the hdisk.
                self._add_mapping(vio_wrap.uuid, device_name)

                # Save the UDID for the disk in the connection info.  It is
                # used for the detach.
                self._set_udid(vio_wrap.uuid, volume_id, udid)
                LOG.info(_LI('Device attached: %s'), device_name)
                self._vioses_modified.append(vio_wrap.uuid)
            elif status == hdisk.LUAStatus.DEVICE_IN_USE:
                LOG.warn(_LW('Discovered device %(dev)s for volume %(volume)s '
                             'on %(vios)s is in use. Error code: %(status)s.'),
                         {'dev': device_name, 'volume': volume_id,
                          'vios': vio_wrap.name, 'status': str(status)})

        # A valid hdisk was not found so log and exit
        if len(self._vioses_modified) == 0:
            msg = (_('Failed to discover valid hdisk on any Virtual I/O '
                     'Server for volume %(volume_id)s.') %
                   {'volume_id': volume_id})
            LOG.error(msg)
            if device_name is None:
                device_name = 'None'
            ex_args = {'backing_dev': device_name, 'reason': msg,
                       'instance_name': self.instance.name}
            raise pexc.VolumeAttachFailed(**ex_args)

    def disconnect_volume(self):
        """Disconnect the volume."""

        volume_id = self.connection_info['data']['volume_id']
        device_name = None
        volume_udid = None
        self._vioses_modified = []
        try:
            # Get VIOS feed
            vios_feed = vios.get_active_vioses(self.adapter, self.host_uuid,
                                               xag=_XAGS)

            # Iterate through host vios list to find hdisks to disconnect.
            for vio_wrap in vios_feed:
                LOG.debug("vios uuid %s", vio_wrap.uuid)
                try:
                    volume_udid = self._get_udid(vio_wrap.uuid, volume_id)
                    device_name = vio_wrap.hdisk_from_uuid(volume_udid)

                    if not device_name:
                        LOG.warn(_LW(u"Disconnect Volume: No mapped device "
                                     "found on vios %(vios)s for volume "
                                     "%(volume_id)s. volume_uid: "
                                     "%(volume_uid)s "),
                                 {'volume_uid': volume_udid,
                                  'volume_id': volume_id,
                                  'vios': vio_wrap.name})
                        continue

                except Exception as e:
                    LOG.warn(_LW(u"Disconnect Volume: Failed to find disk "
                                 "on vios %(vios_name)s for volume "
                                 "%(volume_id)s. volume_uid: %(volume_uid)s."
                                 "Error: %(error)s"),
                             {'error': e, 'volume_uid': volume_udid,
                              'volume_id': volume_id,
                              'vios_name': vio_wrap.name})
                    continue

                # We have found the device name
                LOG.info(_LI(u"Disconnect Volume: Discovered the device "
                             "%(hdisk)s on vios %(vios_name)s for volume "
                             "%(volume_id)s. volume_uid: %(volume_uid)s."),
                         {'volume_uid': volume_udid, 'volume_id': volume_id,
                          'vios_name': vio_wrap.name, 'hdisk': device_name})
                partition_id = vm.get_vm_id(self.adapter, self.vm_uuid)
                tsk_map.remove_pv_mapping(self.adapter, vio_wrap.uuid,
                                          partition_id, device_name)
                self._vioses_modified.append(vio_wrap.uuid)

                try:
                    # Attempt to remove the hDisk
                    hdisk.remove_hdisk(self.adapter, CONF.host, device_name,
                                       vio_wrap.uuid)
                except Exception as e:
                    # If there is a failure, log it, but don't stop the process
                    LOG.warn(_LW("There was an error removing the hdisk "
                             "%(disk)s from the Virtual I/O Server."),
                             {'disk': device_name})
                    LOG.warn(e)

            if len(self._vioses_modified) == 0:
                LOG.warn(_LW("Disconnect Volume: Failed to disconnect the "
                             "disk %(hdisk)s on ANY of the vioses for volume "
                             "%(volume_id)s."),
                         {'hdisk': device_name, 'volume_id': volume_id})

        except Exception as e:
            LOG.error(_LE('Cannot detach volumes from virtual machine: %s'),
                      self.vm_uuid)
            LOG.exception(_LE(u'Error: %s'), e)
            ex_args = {'backing_dev': device_name,
                       'instance_name': self.instance.name,
                       'reason': six.text_type(e)}
            raise pexc.VolumeDetachFailed(**ex_args)

    def wwpns(self):
        """Builds the WWPNs of the adapters that will connect the ports.

        :return: The list of WWPNs that need to be included in the zone set.
        """
        if self._pfc_wwpns is None:
            self._pfc_wwpns = vios.get_physical_wwpns(self.adapter,
                                                      self.host_uuid)
        return self._pfc_wwpns

    def host_name(self):
        """Derives the host name that should be used for the storage device.

        :return: The host name.
        """
        return CONF.host

    def _add_mapping(self, vios_uuid, device_name):
        """This method builds the vscsi map and adds the mapping to
        the given VIOS.

        :param vios_uuid: The UUID of the vios for the pypowervm adapter.
        :param device_name: The The hdisk device name
        """
        pv = pvm_stor.PV.bld(self.adapter, device_name)
        tsk_map.add_vscsi_mapping(self.host_uuid, vios_uuid, self.vm_uuid, pv)

    def _set_udid(self, vios_uuid, volume_id, udid):
        """This method will set the hdisk udid in the connection_info.

        :param vios_uuid: The UUID of the vios for the pypowervm adapter.
        :param volume_id: The lun volume id
        :param udid: The hdisk target_udid to be stored in system_metadata
        """
        udid_key = _build_udid_key(vios_uuid, volume_id)
        self.connection_info['data'][udid_key] = udid

    def _get_hdisk_itls(self, vios_w):
        """Returns the mapped ITLs for the hdisk for the given VIOS.

        A PowerVM system may have multiple Virtual I/O Servers to virtualize
        the I/O to the virtual machines. Each Virtual I/O server may have their
        own set of  initiator WWPNs, target WWPNs and Lun on which hdisk is
        mapped.It will determine and return the ITLs for the given VIOS.

        :param vios_w: A virtual I/O Server wrapper.
        :return: List of the i_wwpns that are part of the vios_w,
        :return: List of the t_wwpns that are part of the vios_w,
        :return: Target lun id of the hdisk for the vios_w.
        """
        it_map = self.connection_info['data']['initiator_target_map']
        i_wwpns = it_map.keys()

        active_wwpns = vios_w.get_active_pfc_wwpns()
        vio_wwpns = [x for x in i_wwpns if x in active_wwpns]

        t_wwpns = []
        for it_key in vio_wwpns:
            t_wwpns.extend(it_map[it_key])
        lun = self.connection_info['data']['target_lun']

        return vio_wwpns, t_wwpns, lun

    def _get_udid(self, vios_uuid, volume_id):
        """This method will return the hdisk udid stored in connection_info.

        :param vios_uuid: The UUID of the vios for the pypowervm adapter.
        :param volume_id: The lun volume id
        :return: The target_udid associated with the hdisk
        """
        try:
            udid_key = _build_udid_key(vios_uuid, volume_id)
            return self.connection_info['data'][udid_key]
        except (KeyError, ValueError):
            LOG.warn(_LW(u'Failed to retrieve device_id key from BDM for '
                         'volume id %s'), volume_id)
            return None
