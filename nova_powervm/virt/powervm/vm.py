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

from oslo_config import cfg
from oslo_log import log as logging

from nova.compute import power_state
from nova import exception
from nova.i18n import _LI, _LE
from nova.virt import hardware
from pypowervm import exceptions as pvm_exc
from pypowervm.tasks import cna
from pypowervm.tasks import power
from pypowervm.tasks import vterm
from pypowervm.utils import lpar_builder as lpar_bldr
from pypowervm.wrappers import logical_partition as pvm_lpar
from pypowervm.wrappers import managed_system as pvm_ms
from pypowervm.wrappers import network as pvm_net

import six

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

PVM_UNCAPPED = 'powervm:uncapped'
PVM_DED_SHAR_MODE = 'powervm:dedicated_sharing_mode'
DED_SHARING_MODES_MAP = {
    'share_idle_procs': pvm_lpar.DedicatedSharingModesEnum.SHARE_IDLE_PROCS,
    'keep_idle_procs': pvm_lpar.DedicatedSharingModesEnum.KEEP_IDLE_PROCS,
    'share_idle_procs_active':
        pvm_lpar.DedicatedSharingModesEnum.SHARE_IDLE_PROCS_ACTIVE,
    'share_idle_procs_always':
        pvm_lpar.DedicatedSharingModesEnum.SHARE_IDLE_PROCS_ALWAYS
}
# Flavor extra_specs for memory and processor properties
POWERVM_ATTRS = ['powervm:min_mem',
                 'powervm:max_mem',
                 'powervm:min_vcpu',
                 'powervm:max_vcpu',
                 'powervm:proc_units',
                 'powervm:min_proc_units',
                 'powervm:max_proc_units',
                 'powervm:dedicated_proc',
                 'powervm:uncapped',
                 'powervm:dedicated_sharing_mode',
                 'powervm:processor_compatibility',
                 'powervm:srr_capability',
                 'powervm:shared_weight',
                 'powervm:availability_priority']

# Map of PowerVM extra specs to the lpar builder attributes.
# '' is used for attributes that are not implemented yet.
# None means there is no direct attribute mapping and must
# be handled individually
ATTRS_MAP = {
    'powervm:min_mem': 'min_mem',
    'powervm:max_mem': 'max_mem',
    'powervm:min_vcpu': 'min_vcpu',
    'powervm:max_vcpu': 'max_vcpu',
    'powervm:proc_units': 'proc_units',
    'powervm:min_proc_units': 'min_proc_units',
    'powervm:max_proc_units': 'max_proc_units',
    'powervm:dedicated_proc': 'dedicated_proc',
    'powervm:uncapped': None,
    'powervm:dedicated_sharing_mode': None,
    'powervm:processor_compatibility': '',
    'powervm:srr_capability': '',
    'powervm:shared_weight': 'uncapped_weight',
    'powervm:availability_priority': ''
}


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
            resp = self._adapter.read(
                pvm_lpar.LPAR.schema_type, root_id=self._uuid,
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
        resp = adapter.read(pvm_ms.System.schema_type, root_id=host_uuid,
                            child_type=pvm_lpar.LPAR.schema_type)
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
            name = pvm_lpar.LPAR.wrap(entry).name
            lpar_list.append(name)

    return lpar_list


def get_instance_wrapper(adapter, instance, host_uuid):
    """Get the LPAR wrapper for a given Nova instance.

    :param adapter: The adapter for the pypowervm API
    :param instance: The nova instance.
    :param host_uuid: (TEMPORARY) The host UUID
    :returns: The pypowervm logical_partition wrapper.
    """
    pvm_inst_uuid = get_pvm_uuid(instance)
    resp = adapter.read(pvm_ms.System.schema_type, root_id=host_uuid,
                        child_type=pvm_lpar.LPAR.schema_type,
                        child_id=pvm_inst_uuid)
    return pvm_lpar.LPAR.wrap(resp)


def _build_attrs(instance, flavor):
    """Builds LPAR attributes that are used by the LPAR builder.

    This method translates instance and flavor values to those
    that can be used by the LPAR builder.

    :param instance: the VM instance
    :param flavor: the instance flavor
    :returns: a dict that can be used by the LPAR builder
    """
    attrs = {}

    attrs[lpar_bldr.NAME] = instance.name
    attrs[lpar_bldr.MEM] = flavor.memory_mb
    attrs[lpar_bldr.VCPU] = flavor.vcpus

    # Loop through the extra specs and process powervm keys
    for key in flavor.extra_specs.keys():
        if not key.startswith('powervm:'):
            # Skip since it's not ours
            continue

        # Check if this is a valid attribute
        if key not in POWERVM_ATTRS:
            exc = exception.InvalidAttribute(attr=key)
            LOG.exception(exc)
            raise exc

        # Look for the mapping to the lpar builder
        bldr_key = ATTRS_MAP.get(key)
        # Check for no direct mapping, handle them individually
        if bldr_key is None:
            # Map uncapped to sharing mode
            if key == PVM_UNCAPPED:
                attrs[lpar_bldr.SHARING_MODE] = (
                    pvm_lpar.SharingModesEnum.UNCAPPED
                    if flavor.extra_specs[key].lower() == 'true' else
                    pvm_lpar.SharingModesEnum.CAPPED)
            elif key == PVM_DED_SHAR_MODE:
                # Dedicated sharing modes...map directly
                mode = DED_SHARING_MODES_MAP.get(flavor.extra_specs[key])
                if mode is not None:
                    attrs[lpar_bldr.SHARING_MODE] = mode
                else:
                    attr = key + '=' + flavor.extra_specs[key]
                    exc = exception.InvalidAttribute(attr=attr)
                    LOG.exception(exc)
                    raise exc
            # TODO(IBM): Handle other attributes
            else:
                # There was no mapping or we didn't handle it.
                exc = exception.InvalidAttribute(attr=key)
                LOG.exception(exc)
                raise exc

        else:
            # We found a mapping
            attrs[bldr_key] = flavor.extra_specs[key]
    return attrs


def get_vm_id(adapter, lpar_uuid):
    """Returns the client LPAR ID for a given UUID.

    :param adapter: The pypowervm adapter.
    :param lpar_uuid: The UUID for the LPAR.
    :returns: The system id (an integer value).
    """
    return adapter.read(pvm_lpar.LPAR.schema_type, root_id=lpar_uuid,
                        suffix_type='quick', suffix_parm='PartitionID').body


def _crt_lpar_builder(host_wrapper, instance, flavor):
    """Create an LPAR builder loaded with the instance and flavor attributes

    :param host_wrapper: The host wrapper
    :param instance: The nova instance.
    :param flavor: The nova flavor.
    :returns: The LPAR builder.
    """

    attrs = _build_attrs(instance, flavor)

    stdz = lpar_bldr.DefaultStandardize(
        attrs, host_wrapper, proc_units_factor=CONF.proc_units_factor)

    return lpar_bldr.LPARBuilder(attrs, stdz)


def crt_lpar(adapter, host_wrapper, instance, flavor):
    """Create an LPAR based on the host based on the instance

    :param adapter: The adapter for the pypowervm API
    :param host_wrapper: The host wrapper
    :param instance: The nova instance.
    :param flavor: The nova flavor.
    :returns: The LPAR response from the API.
    """

    lpar = _crt_lpar_builder(host_wrapper, instance, flavor).build()

    return adapter.create(
        lpar.element, pvm_ms.System.schema_type, root_id=host_wrapper.uuid,
        child_type=pvm_lpar.LPAR.schema_type)


def update(adapter, host_wrapper, instance, flavor, entry=None):
    """Update an LPAR based on the host based on the instance

    :param adapter: The adapter for the pypowervm API
    :param host_wrapper: The host wrapper
    :param instance: The nova instance.
    :param flavor: The nova flavor.
    :param entry: The instance pvm entry, if available, otherwise it will
        be fetched.
    """

    if not entry:
        entry = get_instance_wrapper(adapter, instance, host_wrapper.uuid)

    _crt_lpar_builder(host_wrapper, instance, flavor).rebuild(entry)

    # Write out the new specs
    entry.update(adapter)


def dlt_lpar(adapter, lpar_uuid):
    """Delete an LPAR

    :param adapter: The adapter for the pypowervm API
    :param lpar_uuid: The lpar to delete
    """
    # Attempt to delete the VM. If delete fails because of vterm
    # we will close the vterm and try the delete again
    try:
        LOG.info(_LI('Deleting virtual machine. LPARID: %s') % lpar_uuid)
        resp = adapter.delete(pvm_lpar.LPAR.schema_type, root_id=lpar_uuid)
        LOG.info(_LI('Virtual machine delete status: %s') % resp.status)
        return resp
    except pvm_exc.Error as e:
        emsg = six.text_type(e)
        if 'HSCL151B' in emsg:
            # If this is a vterm error attempt to close vterm
            try:
                LOG.info(_LI('Closing virtual terminal'))
                vterm.close_vterm(adapter, lpar_uuid)
                # Try to delete the vm again
                resp = adapter.delete(pvm_lpar.LPAR.schema_type,
                                      root_id=lpar_uuid)
                LOG.info(_LI('Virtual machine delete status: %s')
                         % resp.status)
                return resp
            except pvm_exc.Error as e:
                # Fall through and raise exception
                pass
        # Attempting to close vterm did not help so raise exception
        LOG.error(_LE('Virtual machine delete failed: LPARID=%s')
                  % lpar_uuid)
        raise e


def power_on(adapter, instance, host_uuid, entry=None):
    if entry is None:
        entry = get_instance_wrapper(adapter, instance, host_uuid)

    # Get the current state and see if we can start the VM
    if entry.state in POWERVM_STARTABLE_STATE:
        # Now start the lpar
        power.power_on(adapter, entry, host_uuid)
        return True

    return False


def power_off(adapter, instance, host_uuid, entry=None, add_parms=None):
    if entry is None:
        entry = get_instance_wrapper(adapter, instance, host_uuid)

    # Get the current state and see if we can stop the VM
    if entry.state in POWERVM_STOPABLE_STATE:
        # Now stop the lpar
        power.power_off(adapter, entry, host_uuid, add_parms=add_parms)
        return True

    return False


def get_pvm_uuid(instance):
    """Get the corresponding PowerVM VM uuid of an instance uuid

    Maps a OpenStack instance uuid to a PowerVM uuid.  For now, this
    just uses a cache or looks up the value from PowerVM.  When we're
    able to set a uuid in PowerVM, then it will be a simple conversion
    between formats.  (Stay tuned.)

    :param instance: nova.objects.instance.Instance
    :returns: pvm_uuid.
    """

    cache = UUIDCache.get_cache()
    return cache.lookup(instance.name)


def get_cnas(adapter, instance, host_uuid):
    """Returns the current CNAs on the instance.

    The Client Network Adapters are the Ethernet adapters for a VM.
    :param adapter: The pypowervm adapter.
    :param instance: The nova instance.
    :param host_uuid: The host system UUID.
    :returns The CNA wrappers that represent the ClientNetworkAdapters on the
             VM.
    """
    cna_resp = adapter.read(pvm_lpar.LPAR.schema_type,
                            root_id=get_pvm_uuid(instance),
                            child_type=pvm_net.CNA.schema_type)
    return pvm_net.CNA.wrap(cna_resp)


def crt_vif(adapter, instance, host_uuid, vif):
    """Will create a Client Network Adapter on the system.

    :param adapter: The pypowervm adapter API interface.
    :param instance: The nova instance to create the VIF against.
    :param host_uuid: The host system UUID.
    :param vif: The nova VIF that describes the ethernet interface.
    """
    lpar_uuid = get_pvm_uuid(instance)
    # CNA's require a VLAN.  If the network doesn't provide, default to 1
    vlan = vif['network']['meta'].get('vlan', 1)
    cna.crt_cna(adapter, host_uuid, lpar_uuid, vlan, mac_addr=vif['address'])


class UUIDCache(object):
    """Cache of instance names to PVM UUID value

    Keeps track of mappings between the instance names and the PowerVM
    UUID values.

    :param adapter: python-powervm adapter.
    """
    _single = None

    def __new__(cls, *args, **kwds):
        if not isinstance(cls._single, cls):
            # Create it
            cls._single = object.__new__(cls, *args, **kwds)
        return cls._single

    @classmethod
    def get_cache(cls):
        return cls._single

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
                resp = self._adapter.read(pvm_lpar.LPAR.schema_type,
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
