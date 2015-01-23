# Copyright 2014, 2015 IBM Corp.
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

from oslo.config import cfg

from nova.compute import power_state
from nova import exception
from nova.openstack.common import log as logging
from nova.virt import hardware
from pypowervm import exceptions as pvm_exc
from pypowervm.jobs import power
from pypowervm.wrappers import constants as pvm_consts
from pypowervm.wrappers import logical_partition as pvm_lpar

LOG = logging.getLogger(__name__)
CONF = cfg.CONF

POWERVM_TO_NOVA_STATE = {
    "migrating running": power_state.RUNNING,
    "running": power_state.RUNNING,
    "starting": power_state.RUNNING,

    "migrating not active": power_state.SHUTDOWN,
    "not activated": power_state.SHUTDOWN,

    "hardware discovery": power_state.NOSTATE,
    "not available": power_state.NOSTATE,
    # map open firmware state to active since it can be shut down
    "open firmware": power_state.RUNNING,
    "resuming": power_state.NOSTATE,
    "shutting down": power_state.NOSTATE,
    "suspending": power_state.NOSTATE,
    "unknown": power_state.NOSTATE,

    "suspended": power_state.SUSPENDED,

    "error": power_state.CRASHED
}

POWERVM_STARTABLE_STATE = ("not activated")
POWERVM_STOPABLE_STATE = ("running", "starting", "open firmware")


def _translate_vm_state(pvm_state):
    """Find the current state of the lpar and convert it to
    the appropriate nova.compute.power_state

    :returns: The appropriate integer state value from power_state
    """

    if pvm_state is None:
        return power_state.NOSTATE

    try:
        nova_state = POWERVM_TO_NOVA_STATE[pvm_state.lower()]
    except KeyError:
        nova_state = power_state.NOSTATE

    return nova_state


class InstanceInfo(hardware.InstanceInfo):
    """Instance Information

    This object tries to lazy load the attributes since the compute
    manager retrieves it a lot just to check the status and doesn't need
    all the attributes.

    :param adapter: pypowervm adapter
    :param name: instance name
    :param uuid: powervm uuid
    """
    def __init__(self, adapter, name, uuid):
        self._adapter = adapter
        self._name = name
        self._uuid = uuid
        self._state = None
        self._mem_kb = None
        self._max_mem_kb = None
        self._num_cpu = None
        # Non-lazy loaded properties
        self.cpu_time_ns = 0
        # Todo(IBM) Should this be the instance UUID vs PowerVM?
        self.id = uuid

    def _get_property(self, q_prop):
        try:
            resp = self._adapter.read(pvm_consts.LPAR, root_id=self._uuid,
                                      suffix_type='quick', suffix_parm=q_prop)
        except pvm_exc.Error as e:
            if e.response.status == 404:
                raise exception.InstanceNotFound(instance_id=self._name)
            else:
                LOG.exception(e)
                raise

        return resp.body.lstrip(' "').rstrip(' "')

    @property
    def state(self):
        # return the state if we previously loaded it
        if self._state is not None:
            return self._state

        # otherwise, fetch the value now
        pvm_state = self._get_property('PartitionState')
        self._state = _translate_vm_state(pvm_state)
        return self._state

    @property
    def mem_kb(self):
        # return the memory if we previously loaded it
        if self._mem_kb is not None:
            return self._mem_kb

        # otherwise, fetch the value now
        pvm_mem_kb = self._get_property('CurrentMemory')
        self._mem_kb = pvm_mem_kb
        return self._mem_kb

    @property
    def max_mem_kb(self):
        # return the max memory if we previously loaded it
        if self._max_mem_kb is not None:
            return self._max_mem_kb

        # TODO(IBM) max isn't a quick property.  We need the wrapper
        return self.mem_kb()

    @property
    def num_cpu(self):
        # return the number of cpus if we previously loaded it
        if self._num_cpu is not None:
            return self._num_cpu

        # otherwise, fetch the value now
        pvm_num_cpu = self._get_property('AllocatedVirtualProcessors')
        self._num_cpu = pvm_num_cpu
        return self._num_cpu

    def __eq__(self, other):
        return (self.__class__ == other.__class__ and
                self._uuid == other._uuid)


def get_lpar_feed(adapter, host_uuid):
    """Get a feed of the LPARs."""

    feed = None
    try:
        resp = adapter.read(pvm_consts.MGT_SYS,
                            root_id=host_uuid,
                            child_type=pvm_consts.LPAR)
        feed = resp.feed
    except pvm_exc.Error as e:
        LOG.exception(e)
    return feed


def get_lpar_list(adapter, host_uuid):
    """Get a list of the LPAR names."""
    lpar_list = []
    feed = get_lpar_feed(adapter, host_uuid)
    if feed is not None:
        for entry in feed.entries:
            name = pvm_lpar.LogicalPartition(entry).name
            lpar_list.append(name)

    return lpar_list


def get_instance_wrapper(adapter, instance, pvm_uuids, host_uuid):
    """Get the LogicalPartition wrapper for a given Nova instance.

    :param adapter: The adapter for the pypowervm API
    :param instance: The nova instance.
    :param pvm_uuids: The PowerVM UUID map (from the UUIDCache)
    :param host_uuid: (TEMPORARY) The host UUID
    :returns: The pypowervm logical_partition wrapper.
    """
    pvm_inst_uuid = pvm_uuids.lookup(instance.name)
    resp = adapter.read(pvm_consts.MGT_SYS, root_id=host_uuid,
                        child_type=pvm_consts.LPAR, child_id=pvm_inst_uuid)
    return pvm_lpar.LogicalPartition.load_from_response(resp)


def calc_proc_units(vcpu):
    return (vcpu * CONF.proc_units_factor)


def _format_lpar_resources(flavor):
    mem = str(flavor.memory_mb)
    vcpus = str(flavor.vcpus)
    proc_units = '%.2f' % calc_proc_units(flavor.vcpus)
    proc_weight = CONF.uncapped_proc_weight

    return mem, vcpus, proc_units, proc_weight


def crt_lpar(adapter, host_uuid, instance, flavor):
    """Create an LPAR based on the host based on the instance

    :param adapter: The adapter for the pypowervm API
    :param host_uuid: (TEMPORARY) The host UUID
    :param instance: The nova instance.
    :param flavor: The nova flavor.
    :returns: The LPAR response from the API.
    """

    mem, vcpus, proc_units, proc_weight = _format_lpar_resources(flavor)

    sprocs = pvm_lpar.crt_shared_procs(proc_units, vcpus,
                                       uncapped_weight=proc_weight)
    lpar_elem = pvm_lpar.crt_lpar(instance.name,
                                  pvm_lpar.LPAR_TYPE_AIXLINUX,
                                  sprocs,
                                  mem,
                                  min_mem=mem,
                                  max_mem=mem,
                                  max_io_slots='64')

    return adapter.create(lpar_elem, pvm_consts.MGT_SYS,
                          root_id=host_uuid, child_type=pvm_lpar.LPAR)


def update(adapter, pvm_uuids, host_uuid, instance, flavor):
    """Update an LPAR based on the host based on the instance

    :param adapter: The adapter for the pypowervm API
    :param pvm_uuids: The PowerVM UUID map (from the UUIDCache)
    :param host_uuid: (TEMPORARY) The host UUID
    :param instance: The nova instance.
    :param flavor: The nova flavor.
    """

    mem, vcpus, proc_units, proc_weight = _format_lpar_resources(flavor)

    entry = get_instance_wrapper(adapter, instance,
                                 pvm_uuids, host_uuid)
    uuid = entry.uuid

    # Set the memory fields
    entry.desired_mem = mem
    entry.max_mem = mem
    entry.min_mem = mem
    # VCPU
    entry.desired_vcpus = vcpus
    entry.max_vcpus = vcpus
    entry.min_vcpus = vcpus
    # Proc Units
    entry.desired_proc_units = proc_units
    entry.max_proc_units = proc_units
    entry.min_proc_units = proc_units
    # Proc weight
    entry.uncapped_weight = str(proc_weight)
    # Write out the new specs
    adapter.update(entry._element, entry.etag, pvm_consts.MGT_SYS,
                   root_id=host_uuid, child_type=pvm_lpar.LPAR, child_id=uuid)


def dlt_lpar(adapter, lpar_uuid):
    """Delete an LPAR

    :param adapter: The adapter for the pypowervm API
    :param lpar_uuid: The lpar to delete
    """
    resp = adapter.delete(pvm_consts.LPAR, root_id=lpar_uuid)
    return resp


def power_on(adapter, instance, pvm_uuids, host_uuid, entry=None):
    if entry is None:
        entry = get_instance_wrapper(adapter, instance, pvm_uuids, host_uuid)

    # Get the current state and see if we can start the VM
    if entry.state in POWERVM_STARTABLE_STATE:
        # Now start the lpar
        power.power_on(adapter, entry, host_uuid)


def power_off(adapter, instance, pvm_uuids, host_uuid, entry=None):
    if entry is None:
        entry = get_instance_wrapper(adapter, instance, pvm_uuids, host_uuid)

    # Get the current state and see if we can stop the VM
    if entry.state in POWERVM_STOPABLE_STATE:
        # Now stop the lpar
        power.power_off(adapter, entry, host_uuid)


class UUIDCache(object):
    """Cache of instance names to PVM UUID value

    Keeps track of mappings between the instance names and the PowerVM
    UUID values.

    :param adapter: python-powervm adapter.
    """
    def __init__(self, adapter):
        self._adapter = adapter
        self._cache = {}

    def lookup(self, name, fetch=True):
        # Lookup the instance name, if we don't find it fetch it, if specified
        uuid = self._cache.get(name, None)
        if uuid is None and fetch:
            # Try to look it up
            searchstring = "(PartitionName=='%s')" % name
            try:
                resp = self._adapter.read(pvm_consts.LPAR,
                                          suffix_type='search',
                                          suffix_parm=searchstring)
            except pvm_exc.Error as e:
                if e.response.status == 404:
                    raise exception.InstanceNotFound(instance_id=name)
                else:
                    LOG.exception(e)
                    raise
            except Exception as e:
                LOG.exception(e)
                raise

            # Process the response
            if len(resp.feed.entries) == 0:
                raise exception.InstanceNotFound(instance_id=name)

            self.load_from_feed(resp.feed)
            uuid = self._cache.get(name, None)
        return uuid

    def add(self, name, uuid):
        # Add the name mapping to the cache
        self._cache[name] = uuid.upper()

    def remove(self, name):
        # Remove the name mapping, if it exists
        try:
            del self._cache[name]
        except KeyError:
            pass

    def load_from_feed(self, feed):
        # Loop through getting the name and uuid, and add them to the cache
        for entry in feed.entries:
            uuid = entry.properties['id']
            name = entry.element.findtext('./PartitionName')
            self.add(name, uuid)
