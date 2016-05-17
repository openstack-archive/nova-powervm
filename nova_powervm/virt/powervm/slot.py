# Copyright 2016 IBM Corp.
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

import six

from oslo_log import log as logging

from pypowervm import const as c
from pypowervm import exceptions as pvm_exc
from pypowervm.tasks import slot_map
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.virt.powervm import exception as p_exc
from nova_powervm.virt.powervm.i18n import _LW


LOG = logging.getLogger(__name__)

_SLOT_KEY = "CLIENT_SLOT_DATA"


def build_slot_mgr(instance, store_api, adapter=None, vol_drv_iter=None):
    """Builds the NovaSlotManager

    A slot manager serves two purposes.  First is to store the 'slots' in which
    client adapters are created.  These adapters host I/O to the VMs and
    it is important to save which VM slots these adapters are created within.

    The second purpose for the slot mgr is to consume that data.  When a VM
    rebuild operation is kicked off, the client adapters must go into the
    same exact slots.  This slot mgr serves up that necessary metadata.

    :param instance: The nova instance to get the slot map for.
    :param store_api: The Swift Storage API that will save the Slot Map data.
                      This may be None, indicating that the Slot Map data
                      should not be persisted.
    :param adapter: pypowervm.adapter.Adapter for REST communication.  Required
                    if rebuilding.  May be omitted if the slot map instance
                    will only be used for saving source mappings.
    :param vol_drv_iter: Iterator over volume drivers in this driver instance.
                         May be omitted if the slot map instance will only be
                         used for saving source mappings.
    :return: The appropriate PowerVM SlotMapStore implementation.  If the NVRAM
             store is set up, a Swift-backed implementation is returned.  If
             there is no NVRAM set up, a no-op implementation is returned.
    """
    if store_api is not None:
        return SwiftSlotManager(store_api, instance=instance, adapter=adapter,
                                vol_drv_iter=vol_drv_iter)
    return NoopSlotManager(instance=instance)


class NovaSlotManager(slot_map.SlotMapStore):
    """Used to manage the slots for a PowerVM-based system.

    Slots are used by the Virtual Machine to place 'adapters' on the system.
    This slot store serves two purposes.  It will guide the spawn (or rebuild)
    operation with what slots it should use.

    Second, it serves as a storage of the client slot data (which can then
    be saved to an external location for rebuild server scenarios).

    This class extends the base pypowervm facilities for this, but adds the
    context of the 'Nova' objects.  It should be extended by the backing
    storage implementations.
    """

    def __init__(self, instance=None, adapter=None, vol_drv_iter=None):
        super(NovaSlotManager, self).__init__(
            '%s_slot_map' % instance.uuid)
        self.instance = instance
        self.adapter = adapter
        self.vol_drv_iter = vol_drv_iter if vol_drv_iter else ()
        self._build_map = None
        self._vios_wraps = []

    @property
    def build_map(self):
        """Returns a 'BuildSlotMap' from pypowervm.

        Identifies for build out of a VM what slots should be used for the
        adapters.
        """
        if self._build_map is None:
            if self.adapter and self.vol_drv_iter:
                self.init_recreate_map(self.adapter, self.vol_drv_iter)
            else:
                self._build_map = slot_map.BuildSlotMap(self)
        return self._build_map

    def init_recreate_map(self, adapter, vol_drv_iter):
        """To be used on a target system.  Builds the 'slot recreate' map.

        This is to initialize on the target system how the client slots should
        be rebuilt on the client VM.

        This should not be called unless it is a VM recreate.

        :param adapter: The pypowervm adapter.
        :param vol_drv_iter: An iterator of the volume drivers.
        """
        # Get the VIOSes first.  Be greedy, this should only be called on a
        # rebuild.  For the rebuild we need to focus on being correct first.
        # Performance is secondary.
        self._vios_wraps = pvm_vios.VIOS.get(
            adapter, xag=[c.XAG.VIO_STOR, c.XAG.VIO_FMAP, c.XAG.VIO_SMAP])

        pv_vscsi_vol_to_vio = {}
        fabric_names = []
        for bdm, vol_drv in vol_drv_iter:
            if vol_drv.vol_type() == 'vscsi':
                self._pv_vscsi_vol_to_vio(vol_drv, pv_vscsi_vol_to_vio)
            elif len(fabric_names) == 0 and vol_drv.vol_type() == 'npiv':
                fabric_names = vol_drv._fabric_names()

        # Run the full initialization now that we have the pre-requisite data
        try:
            self._build_map = slot_map.RebuildSlotMap(
                self, self._vios_wraps, pv_vscsi_vol_to_vio, fabric_names)
        except pvm_exc.InvalidHostForRebuild as e:
            raise p_exc.InvalidRebuild(error=six.text_type(e))

    def _pv_vscsi_vol_to_vio(self, vol_drv, vol_to_vio):
        """Find which physical volumes are on what VIOSes.

        Builds:  { "udid" : [ "vios_uuid", "vios_uuid"], ...}
        """
        for vios_w in self._vios_wraps:
            on_vio, udid = vol_drv.is_volume_on_vios(vios_w)
            if not on_vio:
                continue

            if udid not in vol_to_vio:
                vol_to_vio[udid] = []
            vol_to_vio[udid].append(vios_w.uuid)


class SwiftSlotManager(NovaSlotManager):
    """Used to store the slot metadata for the VM.

    When rebuilding a PowerVM virtual machine, the slots must line up in their
    original location.  This is so that the VM boots from the same location
    and the boot order (as well as other data) is preserved.

    This implementation is used to store the implementation in Swift.  It is
    only used if the operator has choosen to use Swift to store the NVRAM
    metadata.
    """

    def __init__(self, store_api, **kwargs):
        self.store_api = store_api
        super(SwiftSlotManager, self).__init__(**kwargs)

    def load(self):
        return self.store_api.fetch_slot_map(self.inst_key)

    def save(self):
        self.store_api.store_slot_map(self.inst_key, self.serialized)

    def delete(self):
        try:
            self.store_api.delete_slot_map(self.inst_key)
        except Exception:
            LOG.warning(_LW("Unable to delete the slot map from Swift backing "
                            "store with ID %(inst_key)s.  Will require "
                            "manual cleanup."), {'inst_key': self.inst_key},
                        self.instance)


class NoopSlotManager(NovaSlotManager):
    """No op Slot Map (for when Swift is not used - which is standard)."""

    pass
