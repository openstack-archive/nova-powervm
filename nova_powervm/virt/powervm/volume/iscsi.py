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
import collections
import copy
from oslo_concurrency import lockutils
from oslo_log import log as logging

from nova import exception as nova_exc
from nova_powervm import conf as cfg
from nova_powervm.virt.powervm import exception as p_exc
from nova_powervm.virt.powervm import vm
from nova_powervm.virt.powervm.volume import driver as v_driver
from nova_powervm.virt.powervm.volume import volume
from pypowervm import const as pvm_const
from pypowervm import exceptions as pvm_exc
from pypowervm.tasks import hdisk
from pypowervm.tasks import partition as pvm_partition
from pypowervm.utils import transaction as tx
from pypowervm.wrappers import virtual_io_server as pvm_vios

from taskflow import task

import six


LOG = logging.getLogger(__name__)
CONF = cfg.CONF
DEVNAME_KEY = 'target_devname'
_ISCSI_INITIATORS = collections.OrderedDict()


def get_iscsi_initiators(adapter, vios_ids=None):
    """Gets the VIOS iSCSI initiators.

     For the first time invocation of this method after process start up,
     it populates initiators data for VIOSes (if specified, otherwise it
     gets active VIOSes from the host) and stores in memory for futher
     lookup.

    :param adapter: The pypowervm adapter
    :param vios_ids: List of VIOS ids to get the initiators. If not
                     specified, a list of active VIOSes for the
                     host is fetched (but only for the first time)
                     through the pypowervm adapter.
    :return: A dict of the form
             {<vios_id>: <list of initiators>}
    """

    global _ISCSI_INITIATORS

    def discover_initiator(vios_id):

        # Get the VIOS id lock for initiator lookup
        @lockutils.synchronized('inititator-lookup-' + vios_id)
        def _discover_initiator():
            if (vios_id in _ISCSI_INITIATORS and
                    _ISCSI_INITIATORS[vios_id]):
                return
            else:
                try:
                    initiator = hdisk.discover_iscsi_initiator(
                        adapter, vios_id)
                    _ISCSI_INITIATORS[vios_id] = initiator
                except (pvm_exc.ISCSIDiscoveryFailed,
                        pvm_exc.JobRequestFailed) as e:
                    # TODO(chhagarw): handle differently based on
                    # error codes
                    LOG.error(e)

        _discover_initiator()

    if vios_ids is None and not _ISCSI_INITIATORS:
        vios_list = pvm_partition.get_active_vioses(adapter)
        vios_ids = [vios.uuid for vios in vios_list]

    for vios_id in vios_ids or []:
        discover_initiator(vios_id)

    LOG.debug("iSCSI initiator info: %s" % _ISCSI_INITIATORS)
    return _ISCSI_INITIATORS


class IscsiVolumeAdapter(volume.VscsiVolumeAdapter,
                         v_driver.PowerVMVolumeAdapter):
    """The iSCSI implementation of the Volume Adapter.

    This driver will connect a volume to a VM. First using iSCSI to connect the
    volume to the I/O Host (NovaLink partition). Then using the PowerVM vSCSI
    technology to host it to the VM itself.
    """
    def __init__(self, adapter, host_uuid, instance, connection_info,
                 stg_ftsk=None):
        super(IscsiVolumeAdapter, self).__init__(
            adapter, host_uuid, instance, connection_info, stg_ftsk=stg_ftsk)
        if connection_info['driver_volume_type'] == 'iser':
            self.iface_name = 'iser'
        else:
            self.iface_name = CONF.powervm.iscsi_iface

    @classmethod
    def vol_type(cls):
        """The type of volume supported by this type."""
        return 'iscsi'

    @classmethod
    def min_xags(cls):
        """List of pypowervm XAGs needed to support this adapter."""
        return [pvm_const.XAG.VIO_SMAP]

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

        # See the connect_volume for why this is a direct call instead of
        # using the tx_mgr.feed
        vios_wraps = pvm_vios.VIOS.get(self.adapter,
                                       xag=[pvm_const.XAG.VIO_STOR])

        volume_key = 'vscsi-' + self.volume_id
        for vios_w in vios_wraps:
            if vios_w.uuid not in self.vios_uuids:
                continue
            # Discover the volume on all VIOS's to trigger the
            # device configuration, which in turn will discover the
            # LUN associated with the volume. This needs to be
            # attempted on all VIOS's to determine the VIOS's that
            # will be servicing IO for this volume.
            udid = self._discover_volume_on_vios(vios_w)[1]
            if udid:
                LOG.debug("Discovered volume udid %(udid)s on vios %(name)s",
                          dict(udid=udid, name=vios_w.name))
                mig_data[volume_key] = udid

        if volume_key not in mig_data:
            LOG.debug("Failed to discover the volume")
            ex_args = dict(volume_id=self.volume_id,
                           instance_name=self.instance.name)
            raise p_exc.VolumePreMigrationFailed(**ex_args)

    def post_live_migration_at_destination(self, mig_data):
        """This method will update the connection info with the volume udid."""

        volume_key = 'vscsi-' + self.volume_id
        if volume_key in mig_data:
            self._set_udid(mig_data[volume_key])

    def post_live_migration_at_source(self, migrate_data):
        """Performs post live migration for the volume on the source host.

        This method can be used to handle any steps that need to taken on
        the source host after the VM is on the destination.

        :param migrate_data: volume migration data
        """
        # Get the udid of the volume to remove the hdisk for.  We can't
        # use the connection information because LPM 'refreshes' it, which
        # wipes out our data, so we use the data from the destination host
        # to avoid having to discover the hdisk to get the udid.
        udid = migrate_data.get('vscsi-' + self.volume_id)
        self._cleanup_volume(udid)

    def is_volume_on_vios(self, vios_w):
        """Returns whether or not the volume is on a VIOS.

        :param vios_w: The Virtual I/O Server wrapper.
        :return: True if the volume driver's volume is on the VIOS.  False
                 otherwise.
        :return: The udid of the device.
        """
        if vios_w.uuid not in self.vios_uuids:
            return False, None
        device_name, udid = self._discover_volume_on_vios(vios_w)
        return (device_name and udid) is not None, udid

    def _is_multipath(self):
        return self.connection_info["connector"].get("multipath", False)

    def _get_iscsi_conn_props(self, vios_w, auth=False):
        """Returns the required iSCSI connection properties."""
        props = dict()
        try:
            data = self.connection_info['data']
            # For multipath target properties should exist
            if all([key in data for key in ('target_portals',
                                            'target_iqns',
                                            'target_luns')]):
                props['target_portals'] = data['target_portals']
                props['target_iqns'] = data['target_iqns']
                props['target_luns'] = data['target_luns']

            if auth and 'discovery_auth_method' in data:
                for s in ('method', 'username', 'password'):
                    k = 'discovery_auth_' + s
                    props[k] = data[k]

            props['target_portal'] = data['target_portal']
            props['target_iqn'] = data['target_iqn']
            props['target_lun'] = data['target_lun']

            # if auth_method is set look for username, password
            if auth and 'auth_method' in data:
                props['auth_method'] = data['auth_method']
                props['auth_username'] = data['auth_username']
                props['auth_password'] = data['auth_password']

            return props

        except (KeyError, ValueError):
            # Missing information in the connection info
            LOG.warning('Failed to retrieve iSCSI connection properties '
                        'for vios %(vios)s, connection_info=%(cinfo)s',
                        dict(vios=vios_w.uuid, cinfo=self.connection_info))
        return None

    def _discover_vol(self, vios_w, props):
        portal = props.get("target_portals", props.get("target_portal"))
        iqn = props.get("target_iqns", props.get("target_iqn"))
        lun = props.get("target_luns", props.get("target_lun"))
        auth = props.get("auth_method")
        user = props.get("auth_username")
        password = props.get("auth_password")
        discovery_auth = props.get("discovery_auth_method")
        discovery_username = props.get("discovery_auth_username")
        discovery_password = props.get("discovery_auth_password")
        try:
            return hdisk.discover_iscsi(
                self.adapter, portal, user, password, iqn, vios_w.uuid,
                lunid=lun, iface_name=self.iface_name, auth=auth,
                discovery_auth=discovery_auth,
                discovery_username=discovery_username,
                discovery_password=discovery_password,
                multipath=self._is_multipath())
        except (pvm_exc.ISCSIDiscoveryFailed, pvm_exc.JobRequestFailed) as e:
            msg_args = {'vios': vios_w.uuid, 'err': six.text_type(e)}
            LOG.warning("iSCSI discovery on VIOS %(vios)s failed with "
                        "error: %(err)s", msg_args)
            return None, None

    def _discover_volume_on_vios(self, vios_w):
        """Discovers an hdisk on a single vios for the volume.

        :param vios_w: VIOS wrapper to process
        :returns: Device name or None
        :returns: LUN or None
        """
        device_name = udid = None
        conn_props = self._get_iscsi_conn_props(vios_w, auth=True)
        if conn_props is None:
            return None, None

        # Check if multipath, we can directly pass the IQN list to
        # to the low level driver for volume discovery, else iterate
        # over the IQN and get the list for discovery.
        if self._is_multipath():
            device_name, udid = self._discover_vol(vios_w, conn_props)
        else:
            for props in self._iterate_all_targets(conn_props):
                device_name, udid = self._discover_vol(vios_w, props)
        return device_name, udid

    def _connect_volume_to_vio(self, vios_w, slot_mgr):
        """Attempts to connect a volume to a given VIO.

        :param vios_w: The Virtual I/O Server wrapper to connect to.
        :param slot_mgr: A NovaSlotManager.  Used to delete the client slots
                         used when a volume is detached from the VM

        :return: True if the volume was connected.  False if the volume was
                 not (could be the Virtual I/O Server does not have
                 connectivity to the hdisk).
        """
        # check if the vios uuid exist in the expected vios list
        if vios_w.uuid not in self.vios_uuids:
            LOG.debug("Skipping connect volume %(vol)s from "
                      "inactive vios uuid %(uuid)s.",
                      dict(vol=self.volume_id, uuid=vios_w.uuid))
            return False

        device_name, udid = self._discover_volume_on_vios(vios_w)
        if device_name is not None and udid is not None:
            slot, lua = slot_mgr.build_map.get_vscsi_slot(vios_w, device_name)
            volume_id = self.connection_info["data"]["volume_id"]
            # Found a hdisk on this Virtual I/O Server.  Add the action to
            # map it to the VM when the stg_ftsk is executed.
            with lockutils.lock(hash(self)):
                self._add_append_mapping(
                    vios_w.uuid, device_name, lpar_slot_num=slot, lua=lua,
                    udid=udid, tag=volume_id)

            # Save the udid  for the disk in the connection info.  It is
            # used for the detach.
            self._set_udid(udid)

            LOG.debug('Device attached: %s', device_name,
                      instance=self.instance)

            # Valid attachment
            return True

        return False

    def extend_volume(self):
        """Rescan virtual disk so client VM can see extended size."""
        udid = self._get_udid()
        if udid is None:
            raise nova_exc.InvalidBDM()
        self._extend_volume(udid)

    def _disconnect_volume(self, slot_mgr):
        """Disconnect the volume.

        This is the actual method to implement within the subclass.  Some
        transaction maintenance is done by the parent class.

        :param slot_mgr: A NovaSlotManager.  Used to delete the client slots
                         used when a volume is detached from the VM
        """

        def discon_vol_for_vio(vios_w):
            """Removes the volume from a specific Virtual I/O Server.

            :param vios_w: The VIOS wrapper.
            :return: True if a remove action was done against this VIOS.  False
                     otherwise.
            """
            # Check if the vios uuid exist in the list
            if vios_w.uuid not in self.vios_uuids:
                LOG.debug("Skipping disconnect of volume %(vol)s from "
                          "inactive vios uuid %(uuid)s.",
                          dict(vol=self.volume_id, uuid=vios_w.uuid))
                return False

            LOG.debug("Disconnect volume %(vol)s from vios uuid %(uuid)s",
                      dict(vol=self.volume_id, uuid=vios_w.uuid),
                      instance=self.instance)

            device_name = None
            try:
                udid = self._get_udid()
                if udid:
                    # Get the device name using UniqueDeviceID Identifier.
                    device_name = vios_w.hdisk_from_uuid(udid)

                if not udid or not device_name:
                    # If we have no device name, at this point
                    # we should not continue.  Subsequent scrub code on
                    # future deploys will clean this up.
                    LOG.warning(
                        "Disconnect Volume: The backing hdisk for volume "
                        "%(volume_id)s on Virtual I/O Server %(vios)s is "
                        "not in a valid state.  No disconnect "
                        "actions to be taken as volume is not healthy.",
                        {'volume_id': self.volume_id, 'vios': vios_w.name},
                        instance=self.instance)
                    return False

            except Exception:
                LOG.exception(
                    "Disconnect Volume: Failed to find device on Virtual I/O "
                    "Server %(vios_name)s for volume %(volume_id)s.",
                    {'vios_name': vios_w.name, 'volume_id': self.volume_id},
                    instance=self.instance)
                return False

            # We have found the device name
            LOG.info("Disconnect Volume: Discovered the device %(hdisk)s "
                     "on Virtual I/O Server %(vios_name)s for volume "
                     "%(volume_id)s.",
                     {'volume_id': self.volume_id,
                      'vios_name': vios_w.name, 'hdisk': device_name},
                     instance=self.instance)

            # Add the action to remove the mapping when the stg_ftsk is run.
            partition_id = vm.get_vm_id(self.adapter, self.vm_uuid)

            with lockutils.lock(hash(self)):
                self._add_remove_mapping(partition_id, vios_w.uuid,
                                         device_name, slot_mgr)
                conn_data = self._get_iscsi_conn_props(vios_w)
                if not conn_data:
                    return False
                iqn = conn_data.get("target_iqns", conn_data.get("target_iqn"))
                portal = conn_data.get("target_portals",
                                       conn_data.get("target_portal"))
                lun = conn_data.get("target_luns",
                                    conn_data.get("target_lun"))

                def remove():
                    try:
                        hdisk.remove_iscsi(
                            self.adapter, iqn, vios_w.uuid, lun=lun,
                            iface_name=self.iface_name, portal=portal,
                            multipath=self._is_multipath())
                    except (pvm_exc.ISCSIRemoveFailed,
                            pvm_exc.JobRequestFailed) as e:
                        LOG.warning(e)

                self.stg_ftsk.add_post_execute(task.FunctorTask(
                    remove, name='remove_%s_from_vios_%s' % (device_name,
                                                             vios_w.uuid)))

            # Found a valid element to remove
            return True

        try:
            # See logic in _connect_volume for why this new FeedTask is here.
            discon_ftsk = tx.FeedTask(
                'discon_volume_from_vio', pvm_vios.VIOS.getter(
                    self.adapter, xag=[pvm_const.XAG.VIO_STOR]))
            # Find hdisks to disconnect
            discon_ftsk.add_functor_subtask(
                discon_vol_for_vio, provides='vio_modified', flag_update=False)
            ret = discon_ftsk.execute()

            # Warn if no hdisks disconnected.
            if not any([result['vio_modified']
                        for result in ret['wrapper_task_rets'].values()]):
                LOG.warning(
                    "Disconnect Volume: Failed to disconnect the volume "
                    "%(volume_id)s on ANY of the Virtual I/O Servers.",
                    {'volume_id': self.volume_id}, instance=self.instance)

        except Exception as e:
            LOG.exception('PowerVM error detaching volume from virtual '
                          'machine.', instance=self.instance)
            ex_args = {'volume_id': self.volume_id, 'reason': six.text_type(e),
                       'instance_name': self.instance.name}
            raise p_exc.VolumeDetachFailed(**ex_args)

    # Taken from os_brick.initiator.connectors.base_iscsi.py
    def _iterate_all_targets(self, connection_properties):
        for portal, iqn, lun in self._get_all_targets(connection_properties):
            props = copy.deepcopy(connection_properties)
            props['target_portal'] = portal
            props['target_iqn'] = iqn
            props['target_lun'] = lun
            for key in ('target_portals', 'target_iqns', 'target_luns'):
                props.pop(key, None)
            yield props

    def _get_all_targets(self, connection_properties):
        if all([key in connection_properties for key in ('target_portals',
                                                         'target_iqns',
                                                         'target_luns')]):
            return zip(connection_properties['target_portals'],
                       connection_properties['target_iqns'],
                       connection_properties['target_luns'])

        return [(connection_properties['target_portal'],
                 connection_properties['target_iqn'],
                 connection_properties.get('target_lun', 0))]
