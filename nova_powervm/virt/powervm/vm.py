# Copyright 2014, 2018 IBM Corp.
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

from oslo_concurrency import lockutils
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import uuidutils
import re
import six

from nova.compute import power_state
from nova.compute import task_states
from nova import exception
from nova import objects
from nova.virt import event
from nova.virt import hardware
from pypowervm import exceptions as pvm_exc
from pypowervm.helpers import log_helper as pvm_log
from pypowervm.tasks import ibmi
from pypowervm.tasks import power
from pypowervm.tasks import power_opts as popts
from pypowervm.tasks import vterm
from pypowervm import util as pvm_util
from pypowervm.utils import lpar_builder as lpar_bldr
from pypowervm.utils import transaction as pvm_trans
from pypowervm.utils import uuid as pvm_uuid
from pypowervm.utils import validation as vldn
from pypowervm.wrappers import base_partition as pvm_bp
from pypowervm.wrappers import iocard as pvm_card
from pypowervm.wrappers import logical_partition as pvm_lpar
from pypowervm.wrappers import network as pvm_net
from pypowervm.wrappers import shared_proc_pool as pvm_spp

from nova_powervm import conf as cfg
from nova_powervm.virt.powervm import exception as nvex
from nova_powervm.virt.powervm.i18n import _


LOG = logging.getLogger(__name__)
CONF = cfg.CONF

POWERVM_TO_NOVA_STATE = {
    pvm_bp.LPARState.MIGRATING_RUNNING: power_state.RUNNING,
    pvm_bp.LPARState.RUNNING: power_state.RUNNING,
    pvm_bp.LPARState.STARTING: power_state.RUNNING,
    # map open firmware state to active since it can be shut down
    pvm_bp.LPARState.OPEN_FIRMWARE: power_state.RUNNING,
    # It is running until it is off.
    pvm_bp.LPARState.SHUTTING_DOWN: power_state.RUNNING,
    # It is running until the suspend completes
    pvm_bp.LPARState.SUSPENDING: power_state.RUNNING,

    pvm_bp.LPARState.MIGRATING_NOT_ACTIVE: power_state.SHUTDOWN,
    pvm_bp.LPARState.NOT_ACTIVATED: power_state.SHUTDOWN,

    pvm_bp.LPARState.UNKNOWN: power_state.NOSTATE,
    pvm_bp.LPARState.HARDWARE_DISCOVERY: power_state.NOSTATE,
    pvm_bp.LPARState.NOT_AVAILBLE: power_state.NOSTATE,

    # While resuming, we should be considered suspended still.  Only once
    # resumed will we be active (which is represented by the RUNNING state)
    pvm_bp.LPARState.RESUMING: power_state.SUSPENDED,
    pvm_bp.LPARState.SUSPENDED: power_state.SUSPENDED,

    pvm_bp.LPARState.ERROR: power_state.CRASHED
}

# Groupings of PowerVM events used when considering if a state transition
# has taken place.
RUNNING_EVENTS = [
    pvm_bp.LPARState.MIGRATING_RUNNING,
    pvm_bp.LPARState.RUNNING,
    pvm_bp.LPARState.STARTING,
    pvm_bp.LPARState.OPEN_FIRMWARE,
]
STOPPED_EVENTS = [
    pvm_bp.LPARState.NOT_ACTIVATED,
    pvm_bp.LPARState.ERROR,
    pvm_bp.LPARState.UNKNOWN,
]
SUSPENDED_EVENTS = [
    pvm_bp.LPARState.SUSPENDING,
]
RESUMING_EVENTS = [
    pvm_bp.LPARState.RESUMING,
]

POWERVM_STARTABLE_STATE = (pvm_bp.LPARState.NOT_ACTIVATED, )
POWERVM_STOPABLE_STATE = (
    pvm_bp.LPARState.RUNNING, pvm_bp.LPARState.STARTING,
    pvm_bp.LPARState.OPEN_FIRMWARE, pvm_bp.LPARState.SHUTTING_DOWN,
    pvm_bp.LPARState.ERROR, pvm_bp.LPARState.RESUMING,
    pvm_bp.LPARState.SUSPENDING)


def translate_event(pvm_state, pwr_state):
    """Translate the PowerVM state and see if it has changed.

    Compare the state from PowerVM to the state from OpenStack and see if
    a life cycle event should be sent to up to OpenStack.

    :param pvm_state: VM state from PowerVM
    :param pwr_state: Instance power state from OpenStack
    :returns: life cycle event to send.
    """
    trans = None
    if pvm_state in RUNNING_EVENTS and pwr_state != power_state.RUNNING:
        trans = event.EVENT_LIFECYCLE_STARTED
    elif pvm_state in STOPPED_EVENTS and pwr_state != power_state.SHUTDOWN:
        trans = event.EVENT_LIFECYCLE_STOPPED
    elif (pvm_state in SUSPENDED_EVENTS and
          pwr_state != power_state.SUSPENDED):
        trans = event.EVENT_LIFECYCLE_SUSPENDED
    elif pvm_state in RESUMING_EVENTS and pwr_state != power_state.RUNNING:
        trans = event.EVENT_LIFECYCLE_RESUMED

    LOG.debug('Translated {PowerVM state %s; power state %s} to %s',
              pvm_state, pwr_state, trans)
    return trans


def _translate_vm_state(pvm_state):
    """Find the current state of the lpar.

    State is converted to the appropriate nova.compute.power_state

    :return: The appropriate integer state value from power_state
    """

    if pvm_state is None:
        return power_state.NOSTATE

    try:
        nova_state = POWERVM_TO_NOVA_STATE[pvm_state.lower()]
    except KeyError:
        nova_state = power_state.NOSTATE

    return nova_state


class VMBuilder(object):
    """Converts a Nova Instance/Flavor into a pypowervm LPARBuilder."""

    _PVM_PROC_COMPAT = 'powervm:processor_compatibility'
    _PVM_UNCAPPED = 'powervm:uncapped'
    _PVM_DED_SHAR_MODE = 'powervm:dedicated_sharing_mode'
    _PVM_SHAR_PROC_POOL = 'powervm:shared_proc_pool_name'
    _PVM_SRR_CAPABILITY = 'powervm:srr_capability'
    _PVM_PPT_RATIO = 'powervm:ppt_ratio'
    _PVM_ENFORCE_AFFINITY_CHECK = 'powervm:enforce_affinity_check'

    # Map of PowerVM extra specs to the lpar builder attributes.
    # '' is used for attributes that are not implemented yet.
    # None means there is no direct attribute mapping and must
    # be handled individually
    _ATTRS_MAP = {
        'powervm:min_mem': lpar_bldr.MIN_MEM,
        'powervm:max_mem': lpar_bldr.MAX_MEM,
        'powervm:min_vcpu': lpar_bldr.MIN_VCPU,
        'powervm:max_vcpu': lpar_bldr.MAX_VCPU,
        'powervm:proc_units': lpar_bldr.PROC_UNITS,
        'powervm:min_proc_units': lpar_bldr.MIN_PROC_U,
        'powervm:max_proc_units': lpar_bldr.MAX_PROC_U,
        'powervm:dedicated_proc': lpar_bldr.DED_PROCS,
        'powervm:shared_weight': lpar_bldr.UNCAPPED_WEIGHT,
        'powervm:availability_priority': lpar_bldr.AVAIL_PRIORITY,
        'powervm:enable_lpar_metric': lpar_bldr.ENABLE_LPAR_METRIC,
        _PVM_PPT_RATIO: lpar_bldr.PPT_RATIO,
        _PVM_ENFORCE_AFFINITY_CHECK: lpar_bldr.ENFORCE_AFFINITY_CHECK,
        _PVM_UNCAPPED: None,
        _PVM_DED_SHAR_MODE: None,
        _PVM_PROC_COMPAT: None,
        _PVM_SHAR_PROC_POOL: None,
        _PVM_SRR_CAPABILITY: None,
    }

    _DED_SHARING_MODES_MAP = {
        'share_idle_procs': pvm_bp.DedicatedSharingMode.SHARE_IDLE_PROCS,
        'keep_idle_procs': pvm_bp.DedicatedSharingMode.KEEP_IDLE_PROCS,
        'share_idle_procs_active':
            pvm_bp.DedicatedSharingMode.SHARE_IDLE_PROCS_ACTIVE,
        'share_idle_procs_always':
            pvm_bp.DedicatedSharingMode.SHARE_IDLE_PROCS_ALWAYS
    }

    def __init__(self, host_w, adapter, slot_mgr=None, cur_lpar_w=None):
        """Initialize the converter.

        :param host_w: The host system wrapper.
        :param adapter: The pypowervm.adapter.Adapter for the PowerVM REST API.
        :param slot_mgr: NovaSlotManager for setting/saving the maximum number
                         of virtual slots on the VM.
        :param cur_lpar_w: The LPAR wrapper of the instance. Passing in this
                           parameter signifies a resize operation.
        """
        self.adapter = adapter
        self.host_w = host_w
        kwargs = dict(uncapped_weight=CONF.powervm.uncapped_proc_weight,
                      proc_units_factor=CONF.powervm.proc_units_factor)
        if cur_lpar_w:
            # Maintain the existing attributes in DefaultStandardize
            kwargs['max_slots'] = cur_lpar_w.io_config.max_virtual_slots
            kwargs['spp'] = cur_lpar_w.proc_config.shared_proc_cfg.pool_id
            kwargs['avail_priority'] = cur_lpar_w.avail_priority
            kwargs['srr'] = cur_lpar_w.srr_enabled
            kwargs['proc_compat'] = cur_lpar_w.proc_compat_mode
            kwargs['enable_lpar_metric'] = (
                cur_lpar_w.allow_perf_data_collection)
        if slot_mgr is not None:
            # This will already default if not set
            max_vslots = slot_mgr.build_map.get_max_vslots()
            if max_vslots > kwargs.get('max_slots', 0):
                kwargs['max_slots'] = max_vslots
        self.stdz = lpar_bldr.DefaultStandardize(self.host_w, **kwargs)

    def lpar_builder(self, instance):
        """Returns the pypowervm LPARBuilder for a given Nova flavor.

        :param instance: the VM instance
        """
        attrs = self._format_flavor(instance)
        self._add_IBMi_attrs(instance, attrs)
        return lpar_bldr.LPARBuilder(self.adapter, attrs, self.stdz)

    def _add_IBMi_attrs(self, instance, attrs):
        distro = instance.system_metadata.get('image_os_distro', '')
        if distro.lower() == 'ibmi':
            attrs[lpar_bldr.ENV] = pvm_bp.LPARType.OS400
            # Add other attributes in the future

    def _format_flavor(self, instance):
        """Returns the pypowervm format of the flavor.

        :param instance: the VM instance
        :return: a dict that can be used by the LPAR builder
        """
        # The attrs are what is sent to pypowervm to convert the lpar.
        attrs = {}

        attrs[lpar_bldr.NAME] = pvm_util.sanitize_partition_name_for_api(
            instance.name)
        # The uuid is only actually set on a create of an LPAR
        attrs[lpar_bldr.UUID] = pvm_uuid.convert_uuid_to_pvm(instance.uuid)
        attrs[lpar_bldr.MEM] = instance.flavor.memory_mb
        attrs[lpar_bldr.VCPU] = instance.flavor.vcpus
        # Set the srr capability to True by default
        attrs[lpar_bldr.SRR_CAPABLE] = True

        # Loop through the extra specs and process powervm keys
        for key in instance.flavor.extra_specs.keys():
            # If it is not a valid key, then can skip.
            if not self._is_pvm_valid_key(key):
                continue

            # Look for the mapping to the lpar builder
            bldr_key = self._ATTRS_MAP.get(key)

            # Check for no direct mapping, if the value is none, need to
            # derive the complex type
            if bldr_key is None:
                self._build_complex_type(key, attrs, instance.flavor)
            elif bldr_key == lpar_bldr.ENABLE_LPAR_METRIC:
                lpar_metric = self._flavor_bool(
                    instance.flavor.extra_specs[key], key)
                attrs[bldr_key] = lpar_metric
            elif bldr_key == lpar_bldr.PPT_RATIO:
                if (instance.task_state == task_states.REBUILD_SPAWNING and not
                   self.host_w.get_capability(
                       'physical_page_table_ratio_capable')):
                    # We still want to be able to rebuild from hosts that
                    # support setting the PPT ratio to hosts that don't support
                    # setting the PPT ratio.
                    LOG.info("Ignoring PPT ratio on rebuild to PPT ratio "
                             "unsupported host.", instance=instance)
                else:
                    attrs[bldr_key] = instance.flavor.extra_specs[key]
            elif bldr_key == lpar_bldr.ENFORCE_AFFINITY_CHECK:
                if (instance.task_state == task_states.REBUILD_SPAWNING and not
                   self.host_w.get_capability('affinity_check_capable')):
                    # We still want to be able to rebuild from hosts that
                    # support affinity score check to hosts that don't support
                    # affinity score check.
                    LOG.info("Skipping affinity check attribute processing "
                             "on rebuild to host which does not support "
                             "affinity checks.", instance=instance)
                else:
                    attrs[bldr_key] = self._flavor_bool(
                        instance.flavor.extra_specs[key], key)
            else:
                # We found a direct mapping
                attrs[bldr_key] = instance.flavor.extra_specs[key]

        return attrs

    def _is_pvm_valid_key(self, key):
        """Will return if this is a valid PowerVM key.

        :param key: The powervm key.
        :return: True if valid key.  False if non-powervm key and should be
                 skipped.  Raises an InvalidAttribute exception if is an
                 unknown PowerVM key.
        """
        # If not a powervm key, then it is not 'pvm_valid'
        if not key.startswith('powervm:'):
            return False

        # Check if this is a valid attribute
        if key not in self._ATTRS_MAP.keys():
            exc = exception.InvalidAttribute(attr=key)
            raise exc

        return True

    def _build_complex_type(self, key, attrs, flavor):
        """If a key does not directly map, this method derives the right value.

        Some types are complex, in that the flavor may have one key that maps
        to several different attributes in the lpar builder.  This method
        handles the complex types.

        :param key: The flavor's key.
        :param attrs: The attribute map to put the value into.
        :param flavor: The Nova instance flavor.
        :return: The value to put in for the key.
        """
        # Map uncapped to sharing mode
        if key == self._PVM_UNCAPPED:
            is_uncapped = self._flavor_bool(flavor.extra_specs[key], key)
            shar_mode = (pvm_bp.SharingMode.UNCAPPED if is_uncapped
                         else pvm_bp.SharingMode.CAPPED)
            attrs[lpar_bldr.SHARING_MODE] = shar_mode
        elif key == self._PVM_DED_SHAR_MODE:
            # Dedicated sharing modes...map directly
            mode = self._DED_SHARING_MODES_MAP.get(
                flavor.extra_specs[key])
            if mode is not None:
                attrs[lpar_bldr.SHARING_MODE] = mode
            else:
                attr = key + '=' + flavor.extra_specs[key]
                exc = exception.InvalidAttribute(attr=attr)
                LOG.error(exc)
                raise exc
        elif key == self._PVM_SHAR_PROC_POOL:
            pool_name = flavor.extra_specs[key]
            attrs[lpar_bldr.SPP] = self._spp_pool_id(pool_name)
        elif key == self._PVM_PROC_COMPAT:
            # Handle variants of the supported values
            attrs[lpar_bldr.PROC_COMPAT] = re.sub(
                r'\+', '_Plus', flavor.extra_specs[key])
        elif key == self._PVM_SRR_CAPABILITY:
            srr_cap = self._flavor_bool(flavor.extra_specs[key], key)
            attrs[lpar_bldr.SRR_CAPABLE] = srr_cap
        else:
            # There was no mapping or we didn't handle it.
            exc = exception.InvalidAttribute(attr=key)
            LOG.error(exc)
            raise exc

    def _spp_pool_id(self, pool_name):
        """Returns the shared proc pool id for a given pool name.

        :param pool_name: The shared proc pool name.
        :return: The internal API id for the shared proc pool.
        """
        if (pool_name is None or
                pool_name == pvm_spp.DEFAULT_POOL_DISPLAY_NAME):
            # The default pool is 0
            return 0

        # Search for the pool with this name
        pool_wraps = pvm_spp.SharedProcPool.search(
            self.adapter, name=pool_name, parent=self.host_w)

        # Check to make sure there is a pool with the name, and only one pool.
        if len(pool_wraps) > 1:
            msg = (_('Multiple Shared Processing Pools with name %(pool)s.') %
                   {'pool': pool_name})
            raise exception.ValidationError(msg)
        elif len(pool_wraps) == 0:
            msg = (_('Unable to find Shared Processing Pool %(pool)s') %
                   {'pool': pool_name})
            raise exception.ValidationError(msg)

        # Return the singular pool id.
        return pool_wraps[0].id

    def _flavor_bool(self, val, key):
        """Will validate and return the boolean for a given value.

        :param val: The value to parse into a boolean.
        :param key: The flavor key.
        :return: The boolean value for the attribute.  If is not well formed
                 will raise an ValidationError.
        """
        trues = ['true', 't', 'yes', 'y']
        falses = ['false', 'f', 'no', 'n']
        if val.lower() in trues:
            return True
        elif val.lower() in falses:
            return False
        else:
            msg = (_('Flavor attribute %(attr)s must be either True or '
                     'False.  Current value %(val)s is not allowed.') %
                   {'attr': key, 'val': val})
            raise exception.ValidationError(msg)


def get_lpars(adapter):
    """Get a list of the LPAR wrappers."""
    return pvm_lpar.LPAR.search(adapter, is_mgmt_partition=False)


def get_lpar_names(adapter):
    """Get a list of the LPAR names."""
    return [x.name for x in get_lpars(adapter)]


def get_instance_wrapper(adapter, instance, xag=None):
    """Get the LPAR wrapper for a given Nova instance.

    :param adapter: The adapter for the pypowervm API
    :param instance: The nova instance OR its instance uuid.
    :param xag: The pypowervm XAG to be used on the read request
    :return: The pypowervm logical_partition wrapper.
    """
    pvm_inst_uuid = get_pvm_uuid(instance)
    try:
        return pvm_lpar.LPAR.get(adapter, uuid=pvm_inst_uuid, xag=xag)
    except pvm_exc.HttpNotFound:
        raise exception.InstanceNotFound(instance_id=pvm_inst_uuid)


def instance_exists(adapter, instance, log_errors=False):
    """Determine if an instance exists on the host.

    :param adapter: The adapter for the pypowervm API
    :param instance: The nova instance.
    :param log_errors: Indicator whether to log REST data after an exception
    :return: boolean, whether the instance exists.
    """
    try:
        # If we're able to get the property, then it exists.
        get_vm_id(adapter, get_pvm_uuid(instance), log_errors=log_errors)
        return True
    except exception.InstanceNotFound:
        return False


def get_vm_id(adapter, lpar_uuid, log_errors=True):
    """Returns the client LPAR ID for a given UUID.

    :param adapter: The pypowervm adapter.
    :param lpar_uuid: The UUID for the LPAR.
    :param log_errors: Indicator whether to log REST data after an exception
    :return: The system id (an integer value).
    """
    return get_vm_qp(adapter, lpar_uuid, qprop='PartitionID',
                     log_errors=log_errors)


def get_vm_qp(adapter, lpar_uuid, qprop=None, log_errors=True):
    """Returns one or all quick properties of an LPAR.

    :param adapter: The pypowervm adapter.
    :param lpar_uuid: The (powervm) UUID for the LPAR.
    :param qprop: The quick property key to return.  If specified, that single
                  property value is returned.  If None/unspecified, all quick
                  properties are returned in a dictionary.
    :param log_errors: Indicator whether to log REST data after an exception
    :return: Either a single quick property value or a dictionary of all quick
             properties.
    """
    try:
        kwds = dict(root_id=lpar_uuid, suffix_type='quick', suffix_parm=qprop)
        if not log_errors:
            # Remove the log helper from the list of helpers
            helpers = adapter.helpers
            try:
                helpers.remove(pvm_log.log_helper)
            except ValueError:
                # It's not an error if we didn't find it.
                pass
            kwds['helpers'] = helpers
        resp = adapter.read(pvm_lpar.LPAR.schema_type, **kwds)
    except pvm_exc.HttpNotFound:
        # 404 error indicates the LPAR has been deleted (or moved to a
        # different host)
        raise exception.InstanceNotFound(instance_id=lpar_uuid)

    return jsonutils.loads(resp.body)


def get_vm_info(adapter, instance):
    """Get the InstanceInfo for an instance.

    :param adapter: The pypowervm.adapter.Adapter for the PowerVM REST API.
    :param instance: nova.objects.instance.Instance object
    :returns: An InstanceInfo object.
    """
    pvm_uuid = get_pvm_uuid(instance)
    pvm_state = get_vm_qp(adapter, pvm_uuid, 'PartitionState')
    nova_state = _translate_vm_state(pvm_state)
    return hardware.InstanceInfo(nova_state)


def create_lpar(adapter, host_wrapper, instance, nvram=None, slot_mgr=None):
    """Create an LPAR based on the host based on the instance

    :param adapter: The adapter for the pypowervm API
    :param host_wrapper: The host wrapper
    :param instance: The nova instance.
    :param nvram: The NVRAM to set on the LPAR.
    :param slot_mgr: NovaSlotManager to restore/save the maximum number of
                     virtual slots.  If omitted, the default is used.
    :return: The LPAR response from the API.
    """
    try:
        lpar_b = VMBuilder(
            host_wrapper, adapter, slot_mgr=slot_mgr).lpar_builder(instance)
        pending_lpar_w = lpar_b.build()
        vldn.LPARWrapperValidator(pending_lpar_w, host_wrapper).validate_all()
        if nvram is not None:
            pending_lpar_w.nvram = nvram
        lpar_w = pending_lpar_w.create(parent=host_wrapper)
        if slot_mgr is not None:
            slot_mgr.register_max_vslots(lpar_w.io_config.max_virtual_slots)
        return lpar_w
    except lpar_bldr.LPARBuilderException as e:
        # Raise the BuildAbortException since LPAR failed to build
        raise exception.BuildAbortException(instance_uuid=instance.uuid,
                                            reason=e)
    except pvm_exc.HttpError as he:
        # Raise the API exception
        LOG.exception("PowerVM HttpError creating LPAR.", instance=instance)
        raise nvex.PowerVMAPIFailed(inst_name=instance.name, reason=he)


def update(adapter, host_wrapper, instance, entry=None, name=None):
    """Update an LPAR based on the host based on the instance

    :param adapter: The adapter for the pypowervm API
    :param host_wrapper: The host wrapper
    :param instance: The nova instance.
    :param entry: The instance pvm entry, if available, otherwise it will
        be fetched.
    :param name: VM name to use for the update.  Used on resize when we want
        to rename it but not use the instance name.
    :returns: The updated LPAR wrapper.
    """

    if not entry:
        entry = get_instance_wrapper(adapter, instance)

    lpar_b = VMBuilder(host_wrapper, adapter, cur_lpar_w=entry).lpar_builder(
        instance)
    lpar_b.rebuild(entry)

    # Set the new name if the instance name is not desired.
    if name:
        entry.name = pvm_util.sanitize_partition_name_for_api(name)
    # Write out the new specs, return the updated version
    return entry.update()


def rename(adapter, instance, name, entry=None):
    """Rename a VM.

    :param adapter: The adapter for the pypowervm API
    :param instance: The nova instance.
    :param name: The new name.
    :param entry: The instance pvm entry, if available, otherwise it will
        be fetched.
    :returns: The updated LPAR wrapper.
    """
    if not entry:
        entry = get_instance_wrapper(adapter, instance)

    hyp_name = pvm_util.sanitize_partition_name_for_api(name)

    @pvm_trans.entry_transaction
    def _rename(entry):
        entry.name = hyp_name
        return entry.update()

    return _rename(entry)


def delete_lpar(adapter, instance):
    """Delete an LPAR

    :param adapter: The adapter for the pypowervm API.
    :param instance: The nova instance whose LPAR is to be deleted.
    """
    lpar_uuid = get_pvm_uuid(instance)
    # Attempt to delete the VM.
    try:
        LOG.info('Deleting LPAR', instance=instance)

        # Ensure any vterms are closed.  Will no-op otherwise.
        vterm.close_vterm(adapter, lpar_uuid)

        # Run the LPAR delete
        resp = adapter.delete(pvm_lpar.LPAR.schema_type, root_id=lpar_uuid)
        LOG.info('LPAR delete status: %d', resp.status, instance=instance)
        return resp
    except pvm_exc.HttpNotFound:
        LOG.info('LPAR not found (already deleted).', instance=instance)


def power_on(adapter, instance, opts=None):
    """Powers on a VM.

    :param adapter: A pypowervm.adapter.Adapter.
    :param instance: The nova instance to power on.
    :param opts: (Optional) Additional parameters to the pypowervm power_on
                 method.  See that method's docstring for details.
    :return: True if the instance was powered on.  False if it was not in a
             startable state.
    :raises: InstancePowerOnFailure
    """
    # Synchronize power-on and power-off ops on a given instance
    with lockutils.lock('power_%s' % instance.uuid):
        entry = get_instance_wrapper(adapter, instance)

        # Get the current state and see if we can start the VM
        if entry.state in POWERVM_STARTABLE_STATE:
            # Now start the lpar
            power.power_on(entry, None, add_parms=opts)
            return True

    return False


def power_off(adapter, instance, force_immediate=False, timeout=None):
    """Powers off a VM.

    :param adapter: A pypowervm.adapter.Adapter.
    :param instance: The nova instance to power off.
    :param force_immediate: (Optional, Default False) Should it be immediately
                            shut down.
    :param timeout: (Optional, Default None) How long to wait for the job
                    to complete.  By default, is None which indicates it should
                    use the default from pypowervm's power off method.
    :return: True if the instance was stopped.  False if it was not in a
             stoppable state.
    :raises: InstancePowerOffFailure
    """
    # Synchronize power-on and power-off ops on a given instance
    with lockutils.lock('power_%s' % instance.uuid):
        entry = get_instance_wrapper(adapter, instance)

        # Get the current state and see if we can stop the VM
        LOG.debug("Power off requested for instance in state %(state)s. Force "
                  "Immediate Flag: %(force)s.",
                  {'state': entry.state, 'force': force_immediate},
                  instance=instance)
        if entry.state in POWERVM_STOPABLE_STATE:
            # Now stop the lpar
            try:
                LOG.debug("Power off executing.", instance=instance)
                kwargs = {'timeout': timeout} if timeout else {}
                if force_immediate:
                    power.PowerOp.stop(
                        entry, opts=popts.PowerOffOpts().vsp_hard(), **kwargs)
                else:
                    power.power_off_progressive(entry, **kwargs)
            except Exception as e:
                LOG.exception("Failed to power off instance.",
                              instance=instance)
                raise exception.InstancePowerOffFailure(
                    reason=six.text_type(e))
            return True
        else:
            LOG.debug("Power off not required.", instance=instance)

    return False


def reboot(adapter, instance, hard):
    """Reboots a VM.

    :param adapter: A pypowervm.adapter.Adapter.
    :param instance: The nova instance to reboot.
    :param hard: Boolean True if hard reboot, False otherwise.
    :raises: InstanceRebootFailure
    """
    # Synchronize power-on and power-off ops on a given instance
    with lockutils.lock('power_%s' % instance.uuid):
        try:
            entry = get_instance_wrapper(adapter, instance)
            if entry.state != pvm_bp.LPARState.NOT_ACTIVATED:
                if hard:
                    power.PowerOp.stop(
                        entry, opts=popts.PowerOffOpts().vsp_hard().restart())
                else:
                    power.power_off_progressive(entry, restart=True)
            else:
                # pypowervm does NOT throw an exception if "already down".
                # Any other exception from pypowervm is a legitimate failure;
                # let it raise up.
                # If we get here, pypowervm thinks the instance is down.
                power.power_on(entry, None)
        except Exception as e:
            LOG.exception("Failed to reboot instance.", instance=instance)
            raise exception.InstanceRebootFailure(reason=six.text_type(e))


def get_pvm_uuid(instance):
    """Get the corresponding PowerVM VM uuid of an instance uuid

    Maps a OpenStack instance uuid to a PowerVM uuid.  The UUID between the
    Nova instance and PowerVM will be 1 to 1 mapped.  This method runs the
    algorithm against the instance's uuid to convert it to the PowerVM
    UUID.

    :param instance: nova.objects.instance.Instance OR the OpenStack instance
                     uuid.
    :return: pvm_uuid.
    """
    inst_uuid = instance if uuidutils.is_uuid_like(instance) else instance.uuid
    return pvm_uuid.convert_uuid_to_pvm(inst_uuid).upper()


def _uuid_set_high_bit(pvm_uuid):
    """Turns on the high bit of a uuid

    PowerVM uuids always set the byte 0, bit 0 to 0.
    So to convert it to an OpenStack uuid we may have to set the high bit.

    :param uuid: A PowerVM compliant uuid
    :returns: A standard format uuid string
    """
    return "%x%s" % (int(pvm_uuid[0], 16) | 8, pvm_uuid[1:])


def get_instance(context, pvm_uuid):
    """Get an instance, if there is one, that corresponds to the PVM UUID

    Not finding the instance can be a pretty normal case when handling events.
    Don't log exceptions for those cases.

    :param pvm_uuid: PowerVM UUID
    :return: OpenStack instance or None
    """
    uuid = pvm_uuid.lower()

    def get_inst():
        try:
            return objects.Instance.get_by_uuid(context, uuid)
        except exception.InstanceNotFound:
            return objects.Instance.get_by_uuid(context,
                                                _uuid_set_high_bit(uuid))

    try:
        return get_inst()
    except exception.InstanceNotFound:
        pass
    except Exception as e:
        LOG.debug('Instance with PowerVM UUID %s not found: %s', pvm_uuid, e)
    return None


def get_cnas(adapter, instance, **search):
    """Returns the (possibly filtered) current CNAs on the instance.

    The Client Network Adapters are the Ethernet adapters for a VM.
    :param adapter: The pypowervm adapter.
    :param instance: The nova instance.
    :param search: Keyword arguments for CNA.search.  If omitted, all CNAs are
                   returned.
    :return: The CNA wrappers that represent the ClientNetworkAdapters
    on the VM
    """
    meth = pvm_net.CNA.search if search else pvm_net.CNA.get

    return meth(adapter, parent_type=pvm_lpar.LPAR,
                parent_uuid=get_pvm_uuid(instance), **search)


def get_vnics(adapter, instance, **search):
    """Returns the (possibly filtered) current vNICs on the instance.

    :param adapter: The pypowervm adapter.
    :param instance: The nova instance.
    :param search: Keyword arguments for VNIC.search.  If omitted, all VNICs
                   are returned.
    :return: The VNIC wrappers that represent the virtual NICs on the VM.
    """
    meth = pvm_card.VNIC.search if search else pvm_card.VNIC.get

    return meth(adapter, parent_type=pvm_lpar.LPAR,
                parent_uuid=get_pvm_uuid(instance), **search)


def norm_mac(mac):
    """Normalizes a MAC address from pypowervm format to OpenStack.

    That means that the format will be converted to lower case and will
    have colons added.

    :param mac: A pypowervm mac address.  Ex. 1234567890AB
    :return: A mac that matches the standard neutron format.
             Ex. 12:34:56:78:90:ab
    """
    mac = mac.lower().replace(':', '')
    return ':'.join(mac[i:i + 2] for i in range(0, len(mac), 2))


def update_ibmi_settings(adapter, instance, boot_type):
    """Update settings of IBMi VMs on the instance.

    :param adapter: The pypowervm adapter.
    :param instance: The nova instance.
    :param boot_type: The boot connectivity type of the instance.
    """
    lpar_wrap = get_instance_wrapper(adapter, instance)
    entry = ibmi.update_ibmi_settings(adapter, lpar_wrap, boot_type)
    entry.update()
