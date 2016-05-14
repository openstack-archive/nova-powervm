# Copyright 2014, 2016 IBM Corp.
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

from nova import block_device
from nova.compute import task_states
from nova.compute import utils as compute_utils
from nova.compute import vm_states
from nova.console import type as console_type
from nova import context as ctx
from nova import exception
from nova import image
from nova import objects
from nova.objects import flavor as flavor_obj
from nova import utils as n_utils
from nova.virt import configdrive
from nova.virt import driver
from nova.virt import event
from oslo_log import log as logging
from oslo_utils import importutils
import re
import six
from taskflow import engines as tf_eng
from taskflow.patterns import linear_flow as tf_lf
import time

from pypowervm import adapter as pvm_apt
from pypowervm import const as pvm_const
from pypowervm import exceptions as pvm_exc
from pypowervm.helpers import log_helper as log_hlp
from pypowervm.helpers import vios_busy as vio_hlp
from pypowervm.tasks import memory as pvm_mem
from pypowervm.tasks import partition as pvm_par
from pypowervm.tasks import power as pvm_pwr
from pypowervm.tasks import vterm as pvm_vterm
from pypowervm import util as pvm_util
from pypowervm.wrappers import base_partition as pvm_bp
from pypowervm.wrappers import managed_system as pvm_ms

from nova_powervm import conf as cfg
from nova_powervm.virt.powervm.disk import driver as disk_dvr
from nova_powervm.virt.powervm import host as pvm_host
from nova_powervm.virt.powervm.i18n import _
from nova_powervm.virt.powervm.i18n import _LE
from nova_powervm.virt.powervm.i18n import _LI
from nova_powervm.virt.powervm.i18n import _LW
from nova_powervm.virt.powervm import image as img
from nova_powervm.virt.powervm import live_migration as lpm
from nova_powervm.virt.powervm.nvram import manager as nvram_manager
from nova_powervm.virt.powervm import slot
from nova_powervm.virt.powervm.tasks import image as tf_img
from nova_powervm.virt.powervm.tasks import network as tf_net
from nova_powervm.virt.powervm.tasks import slot as tf_slot
from nova_powervm.virt.powervm.tasks import storage as tf_stg
from nova_powervm.virt.powervm.tasks import vm as tf_vm
from nova_powervm.virt.powervm import vios
from nova_powervm.virt.powervm import vm
from nova_powervm.virt.powervm import volume as vol_attach

LOG = logging.getLogger(__name__)
CONF = cfg.CONF

# Defines, for all cinder volume types, which volume driver to use.  Currently
# only supports Fibre Channel, which has multiple options for connections.
# The connection strategy is defined above.
VOLUME_DRIVER_MAPPINGS = {
    'fibre_channel': vol_attach.FC_STRATEGY_MAPPING[
        CONF.powervm.fc_attach_strategy]
}

DISK_ADPT_NS = 'nova_powervm.virt.powervm.disk'
DISK_ADPT_MAPPINGS = {
    'localdisk': 'localdisk.LocalStorage',
    'ssp': 'ssp.SSPDiskAdapter'
}
# NVRAM store APIs for the NVRAM manager to use
NVRAM_NS = 'nova_powervm.virt.powervm.nvram.'
NVRAM_APIS = {
    'swift': 'swift.SwiftNvramStore',
}

KEEP_NVRAM_STATES = {vm_states.SHELVED, }
FETCH_NVRAM_STATES = {vm_states.SHELVED, vm_states.SHELVED_OFFLOADED}


class PowerVMDriver(driver.ComputeDriver):

    """PowerVM Implementation of Compute Driver."""

    capabilities = {
        "has_imagecache": False,
        "supports_recreate": True,
        "supports_migrate_to_same_host": False
    }

    def __init__(self, virtapi):
        super(PowerVMDriver, self).__init__(virtapi)

    def init_host(self, host):
        """Initialize anything that is necessary for the driver to function.

        Includes catching up with currently running VM's on the given host.
        """

        # Live migrations
        self.live_migrations = {}
        # Set the nvram mgr to None so events are not handled until it's setup
        self.nvram_mgr = None
        self.store_api = None
        # Get an adapter
        self._get_adapter()
        # First need to resolve the managed host UUID
        self._get_host_uuid()
        # Get the management partition's UUID
        self.mp_uuid = pvm_par.get_this_partition(self.adapter).uuid
        LOG.debug("Driver found compute partition UUID of: %s" % self.mp_uuid)

        # Make sure the Virtual I/O Server(s) are available.
        vios.validate_vios_ready(self.adapter, self.host_uuid)

        # Initialize the disk adapter.  Sets self.disk_dvr
        self._get_disk_adapter()
        self.image_api = image.API()

        self._setup_rebuild_store()

        # Init Host CPU Statistics
        self.host_cpu_stats = pvm_host.HostCPUStats(self.adapter,
                                                    self.host_uuid)

        # Cache for instance overhead.
        # Key: max_mem (int MB)
        # Value: overhead (int MB)
        self._inst_overhead_cache = {}

        LOG.info(_LI("The compute driver has been initialized."))

    def cleanup_host(self, host):
        """Clean up anything that is necessary for the driver gracefully stop.

        Includes ending remote sessions. This is optional.
        """
        # Stop listening for events
        try:
            self.session.get_event_listener().shutdown()
        except Exception:
            pass

        LOG.info(_LI("The compute driver has been shutdown."))

    def _get_adapter(self):
        # Build the adapter.  May need to attempt the connection multiple times
        # in case the REST server is starting.
        self.session = pvm_apt.Session(conn_tries=300)
        self.adapter = pvm_apt.Adapter(
            self.session, helpers=[log_hlp.log_helper,
                                   vio_hlp.vios_busy_retry_helper])
        # Register the event handler
        eh = NovaEventHandler(self)
        self.session.get_event_listener().subscribe(eh)

    def _get_disk_adapter(self):
        conn_info = {'adapter': self.adapter, 'host_uuid': self.host_uuid,
                     'mp_uuid': self.mp_uuid}

        self.disk_dvr = importutils.import_object_ns(
            DISK_ADPT_NS, DISK_ADPT_MAPPINGS[CONF.powervm.disk_driver.lower()],
            conn_info)

    def _setup_rebuild_store(self):
        """Setup the store for remote restart objects."""
        store = CONF.powervm.nvram_store.lower()
        if store != 'none':
            self.store_api = importutils.import_object(
                NVRAM_NS + NVRAM_APIS[store])
            # Events will be handled once the nvram_mgr is set.
            self.nvram_mgr = nvram_manager.NvramManager(
                self.store_api, self.adapter, self.host_uuid)
            # Do host startup for NVRAM for existing VMs on the host
            n_utils.spawn(self._nvram_host_startup)

    def _nvram_host_startup(self):
        """NVRAM Startup.

        When the compute node starts up, it's not known if any NVRAM events
        were missed when the compute process was not running. During startup
        put each LPAR on the queue to be updated, just incase.
        """
        for lpar_w in vm.get_lpars(self.adapter):
            # Find the instance for the LPAR.
            inst = vm.get_instance(ctx.get_admin_context(), lpar_w.uuid)
            if inst is not None and inst.host == CONF.host:
                self.nvram_mgr.store(inst)
            time.sleep(0)

    def _get_host_uuid(self):
        """Get the System wrapper and its UUID for the (single) host."""
        syswraps = pvm_ms.System.wrap(
            self.adapter.read(pvm_ms.System.schema_type))
        if len(syswraps) != 1:
            raise Exception(
                _("Expected exactly one host; found %d"), len(syswraps))
        self.host_wrapper = syswraps[0]
        self.host_uuid = self.host_wrapper.uuid
        LOG.info(_LI("Host UUID is:%s"), self.host_uuid)

    @staticmethod
    def _log_operation(op, instance):
        """Log entry point of driver operations."""
        LOG.info(_LI('Operation: %(op)s. Virtual machine display name: '
                     '%(display_name)s, name: %(name)s, UUID: %(uuid)s'),
                 {'op': op, 'display_name': instance.display_name,
                  'name': instance.name, 'uuid': instance.uuid})

    def get_info(self, instance):
        """Get the current status of an instance, by name (not ID!)

        Returns a dict containing:

        :state:           the running state, one of the power_state codes
        :max_mem:         (int) the maximum memory in KBytes allowed
        :mem:             (int) the memory in KBytes used by the domain
        :num_cpu:         (int) the number of virtual CPUs for the domain
        :cpu_time:        (int) the CPU time used in nanoseconds
        """
        info = vm.InstanceInfo(self.adapter, instance.name,
                               vm.get_pvm_uuid(instance))
        return info

    def instance_exists(self, instance):
        """Checks existence of an instance on the host.

        :param instance: The instance to lookup

        Returns True if an instance with the supplied ID exists on
        the host, False otherwise.
        """
        return vm.instance_exists(self.adapter, instance, self.host_uuid)

    def estimate_instance_overhead(self, instance_info):
        """Estimate the virtualization overhead required to build an instance.

        Defaults to zero, Per-instance overhead calculations are desired.

        :param instance_info: Instance/flavor to calculate overhead for.
         It can be Instance or Flavor object or a simple dict. The dict is
         expected to contain:
         { 'memory_mb': <int>, 'extra_specs': {'powervm:max_mem': <int> }}
         Values not found will default to zero.
        :return: Dict of estimated overhead values {'memory_mb': overhead}
        """
        # Check if input passed is an object instance then extract Flavor
        if isinstance(instance_info, objects.Instance):
            instance_info = instance_info.get_flavor()
        # If the instance info passed is dict then create Flavor object.
        elif isinstance(instance_info, dict):
            instance_info = objects.Flavor(**instance_info)

        max_mem = 0
        overhead = 0
        try:
            cur_mem = instance_info.memory_mb
            if hasattr(instance_info, 'extra_specs'):
                if 'powervm:max_mem' in instance_info.extra_specs.keys():
                    mem = instance_info.extra_specs.get('powervm:max_mem',
                                                        max_mem)
                    max_mem = int(mem)

            max_mem = max(cur_mem, max_mem)
            if max_mem in self._inst_overhead_cache:
                overhead = self._inst_overhead_cache[max_mem]
            else:
                overhead, avail = pvm_mem.calculate_memory_overhead_on_host(
                    self.adapter, self.host_uuid, {'max_mem': max_mem})
                self._inst_overhead_cache[max_mem] = overhead

        except Exception as e:
            LOG.exception(e)
        finally:
            return {'memory_mb': overhead}

    def list_instances(self):
        """Return the names of all the instances known to the virt host.

        :return: VM Names as a list.
        """
        lpar_list = vm.get_lpar_names(self.adapter)
        return lpar_list

    def get_host_cpu_stats(self):
        """Return the current CPU state of the host."""
        return self.host_cpu_stats.get_host_cpu_stats()

    def instance_on_disk(self, instance):
        """Checks access of instance files on the host.

        :param instance: nova.objects.instance.Instance to lookup

        Returns True if files of an instance with the supplied ID accessible on
        the host, False otherwise.

        .. note::
            Used in rebuild for HA implementation and required for validation
            of access to instance shared disk files
        """

        # If the instance is booted from volume then we shouldn't
        # really care if instance "disks" are on shared storage.
        context = ctx.get_admin_context()
        block_device_info = self._get_block_device_info(context, instance)
        if self._is_booted_from_volume(block_device_info):
            LOG.debug('Instance booted from volume.', instance=instance)
            return True

        # If configured for shared storage, see if we can find the disks
        if self.disk_dvr.capabilities['shared_storage']:
            LOG.debug('Looking for instance disks on shared storage.',
                      instance=instance)
            # Try to get a reference to the disk
            try:
                if self.disk_dvr.get_disk_ref(instance,
                                              disk_dvr.DiskType.BOOT):
                    LOG.debug('Disks found on shared storage.',
                              instance=instance)
                    return True
            except Exception as e:
                LOG.exception(e)

        LOG.debug('Instance disks not found on this host.', instance=instance)
        return False

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None,
              flavor=None):
        """Create a new instance/VM/domain on the virtualization platform.

        Once this successfully completes, the instance should be
        running (power_state.RUNNING).

        If this fails, any partial instance should be completely
        cleaned up, and the virtualization platform should be in the state
        that it was before this call began.

        Spawn can be called while deploying an instance for the first time or
        it can be called to recreate an instance that was shelved or during
        evacuation.  We have to be careful to handle all these cases.  During
        evacuation, when on shared storage, the image_meta will be empty.

        :param context: security context
        :param instance: Instance object as returned by DB layer.
                         This function should use the data there to guide
                         the creation of the new instance.
        :param nova.objects.ImageMeta image_meta:
            The metadata of the image of the instance.
        :param injected_files: User files to inject into instance.
        :param admin_password: Administrator password to set in instance.
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param block_device_info: Information about block devices to be
                                  attached to the instance.
        :param flavor: The flavor for the instance to be spawned.
        """
        self._log_operation('spawn', instance)
        if not flavor:
            admin_ctx = ctx.get_admin_context(read_deleted='yes')
            flavor = (
                flavor_obj.Flavor.get_by_id(admin_ctx,
                                            instance.instance_type_id))

        # Extract the block devices.
        bdms = self._extract_bdm(block_device_info)

        # Define the flow
        flow_spawn = tf_lf.Flow("spawn")

        # Determine if this is a VM recreate
        recreate = (instance.task_state == task_states.REBUILD_SPAWNING and
                    'id' not in image_meta)

        # Create the transaction manager (FeedTask) for Storage I/O.
        xag = self._get_inst_xag(instance, bdms, recreate=recreate)
        stg_ftsk = vios.build_tx_feed_task(self.adapter, self.host_uuid,
                                           xag=xag)

        # Build the PowerVM Slot lookup map.  Only the recreate action needs
        # the volume driver iterator (to look up volumes and their client
        # mappings).
        vol_drv_iter = (self._vol_drv_iter(context, instance, bdms=bdms,
                                           stg_ftsk=stg_ftsk)
                        if recreate else None)
        slot_mgr = slot.build_slot_mgr(
            instance, self.store_api, adapter=self.adapter,
            vol_drv_iter=vol_drv_iter)

        # Create the LPAR, check if NVRAM restore is needed.
        nvram_mgr = (self.nvram_mgr if self.nvram_mgr and
                     (recreate or instance.vm_state in FETCH_NVRAM_STATES)
                     else None)
        flow_spawn.add(tf_vm.Create(self.adapter, self.host_wrapper, instance,
                                    flavor, stg_ftsk, nvram_mgr=nvram_mgr))

        # Create a flow for the IO
        flow_spawn.add(tf_net.PlugVifs(
            self.virtapi, self.adapter, instance, network_info,
            self.host_uuid, slot_mgr))
        flow_spawn.add(tf_net.PlugMgmtVif(
            self.adapter, instance, self.host_uuid, slot_mgr))

        # Only add the image disk if this is from Glance.
        if not self._is_booted_from_volume(block_device_info):

            # If a recreate, just hookup the existing disk on shared storage.
            if recreate:
                flow_spawn.add(tf_stg.FindDisk(
                    self.disk_dvr, context, instance, disk_dvr.DiskType.BOOT))
            else:
                # Creates the boot image.
                flow_spawn.add(tf_stg.CreateDiskForImg(
                    self.disk_dvr, context, instance, image_meta,
                    disk_size=flavor.root_gb))
            # Connects up the disk to the LPAR
            flow_spawn.add(tf_stg.ConnectDisk(self.disk_dvr, context, instance,
                                              stg_ftsk=stg_ftsk))

        # Determine if there are volumes to connect.  If so, add a connection
        # for each type.
        self._add_volume_connection_tasks(
            context, instance, bdms, flow_spawn, stg_ftsk, slot_mgr)

        # If the config drive is needed, add those steps.  Should be done
        # after all the other I/O.
        if configdrive.required_by(instance) and not recreate:
            flow_spawn.add(tf_stg.CreateAndConnectCfgDrive(
                self.adapter, self.host_uuid, instance, injected_files,
                network_info, admin_password, stg_ftsk=stg_ftsk))

        # Add the transaction manager flow to the end of the 'I/O
        # connection' tasks.  This will run all the connections in parallel.
        flow_spawn.add(stg_ftsk)

        # Update load source of IBMi VM
        distro = instance.system_metadata.get('image_os_distro', '')
        if distro.lower() == img.OSDistro.OS400:
            boot_type = self._get_boot_connectivity_type(
                context, bdms, block_device_info)
            flow_spawn.add(tf_vm.UpdateIBMiSettings(
                self.adapter, instance, self.host_uuid, boot_type))

        # Save the slot map information
        flow_spawn.add(tf_slot.SaveSlotStore(instance, slot_mgr))

        # Last step is to power on the system.
        flow_spawn.add(tf_vm.PowerOn(self.adapter, self.host_uuid, instance))

        # Run the flow.
        tf_eng.run(flow_spawn)

    def _add_volume_connection_tasks(self, context, instance, bdms,
                                     flow, stg_ftsk, slot_mgr):
        """Determine if there are volumes to connect to this instance.

        If there are volumes to connect to this instance add a task to the
        flow for each volume.

        :param context: security context.
        :param instance: Instance object as returned by DB layer.
        :param bdms: block device mappings.
        :param flow: the flow to add the tasks to.
        :param stg_ftsk: the storage task flow.
        :param slot_mgr: A NovaSlotManager.  Used to store/retrieve the client
                         slots used when a volume is attached to a VM.
        """
        for bdm, vol_drv in self._vol_drv_iter(context, instance, bdms=bdms,
                                               stg_ftsk=stg_ftsk):
            # First connect the volume.  This will update the
            # connection_info.
            flow.add(tf_stg.ConnectVolume(vol_drv, slot_mgr))

            # Save the BDM so that the updated connection info is
            # persisted.
            flow.add(tf_stg.SaveBDM(bdm, instance))

    def _add_volume_disconnection_tasks(self, context, instance, bdms,
                                        flow, stg_ftsk, slot_mgr):
        """Determine if there are volumes to disconnect from this instance.

        If there are volumes to disconnect from this instance add a task to the
        flow for each volume.

        :param context: security context.
        :param instance: Instance object as returned by DB layer.
        :param bdms: block device mappings.
        :param flow: the flow to add the tasks to.
        :param stg_ftsk: the storage task flow.
        :param slot_mgr: A NovaSlotManager.  Used to store/retrieve the client
                         slots used when a volume is detached from a VM.
        """
        # TODO(thorst) Do we need to do something on the disconnect for slots?
        for bdm, vol_drv in self._vol_drv_iter(context, instance, bdms=bdms,
                                               stg_ftsk=stg_ftsk):
            flow.add(tf_stg.DisconnectVolume(vol_drv, slot_mgr))

    def _get_block_device_info(self, context, instance):
        """Retrieves the instance's block_device_info."""

        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
            context, instance.uuid)
        return driver.get_block_device_info(instance, bdms)

    def _is_booted_from_volume(self, block_device_info):
        """Determine whether the root device is listed in block_device_info.

        If it is, this can be considered a 'boot from Cinder Volume'.

        :param block_device_info: The block device info from the compute
                                  manager.
        :return: True if the root device is in block_device_info and False if
                 it is not.
        """
        if block_device_info is None:
            return False

        root_bdm = block_device.get_root_bdm(
            driver.block_device_info_get_mapping(block_device_info))
        return (root_bdm is not None)

    @property
    def need_legacy_block_device_info(self):
        return False

    def _destroy(self, context, instance, block_device_info=None,
                 network_info=None, destroy_disks=True, shutdown=True):

        """Internal destroy method used by multiple operations.

        :param context: security context
        :param instance: Instance object as returned by DB layer.
        :param block_device_info: Information about block devices that should
                                  be detached from the instance.
                                  This can be None when destroying the original
                                  VM during confirm resize/migration.  In that
                                  case, the storage mappings have already been
                                  removed from the original VM, so no work to
                                  do.
        :param network_info: The network information associated with the
                             instance
        :param destroy_disks: Indicates if disks should be destroyed
        :param shutdown: Indicate whether to shutdown the VM first
        """

        def _setup_flow_and_run():
            # Extract the block devices.
            bdms = self._extract_bdm(block_device_info)

            # Define the flow
            flow = tf_lf.Flow("destroy")

            if shutdown:
                # Power Off the LPAR. If its disks are about to be deleted,
                # VSP hard shutdown it.
                flow.add(tf_vm.PowerOff(self.adapter, self.host_uuid,
                                        pvm_inst_uuid, instance,
                                        force_immediate=destroy_disks))

            # Create the transaction manager (FeedTask) for Storage I/O.
            xag = self._get_inst_xag(instance, bdms)
            stg_ftsk = vios.build_tx_feed_task(self.adapter, self.host_uuid,
                                               xag=xag)

            # Build the PowerVM Slot lookup map.
            slot_mgr = slot.build_slot_mgr(instance, self.store_api)

            # Call the unplug VIFs task.  While CNAs get removed from the LPAR
            # directly on the destroy, this clears up the I/O Host side.
            flow.add(tf_vm.Get(self.adapter, self.host_uuid, instance))
            flow.add(tf_net.UnplugVifs(self.adapter, instance, network_info,
                                       self.host_uuid, slot_mgr))

            # Add the disconnect/deletion of the vOpt to the transaction
            # manager.
            flow.add(tf_stg.DeleteVOpt(self.adapter, self.host_uuid, instance,
                                       pvm_inst_uuid, stg_ftsk=stg_ftsk))

            # Determine if there are volumes to disconnect.  If so, remove each
            # volume (within the transaction manager)
            self._add_volume_disconnection_tasks(
                context, instance, bdms, flow, stg_ftsk, slot_mgr)

            # Only detach the disk adapters if this is not a boot from volume
            # since volumes are handled above.  This is only for disks.
            destroy_disk_task = None
            if not self._is_booted_from_volume(block_device_info):
                # Detach the disk storage adapters (when the stg_ftsk runs)
                flow.add(tf_stg.DetachDisk(
                    self.disk_dvr, context, instance, stg_ftsk))

                # Delete the storage disks
                if destroy_disks:
                    destroy_disk_task = tf_stg.DeleteDisk(
                        self.disk_dvr, context, instance)

            # Add the transaction manager flow to the end of the 'storage
            # connection' tasks.  This will run all the disconnection ops
            # in parallel
            flow.add(stg_ftsk)

            # The disks shouldn't be destroyed until the unmappings are done.
            if destroy_disk_task:
                flow.add(destroy_disk_task)

            # Last step is to delete the LPAR from the system and delete
            # the NVRAM from the store.
            # Note: If moving to a Graph Flow, will need to change to depend on
            # the prior step.
            flow.add(tf_vm.Delete(self.adapter, pvm_inst_uuid, instance))

            if (destroy_disks and
                    instance.vm_state not in KEEP_NVRAM_STATES and
                    instance.host in [None, CONF.host]):
                # If the disks are being destroyed and not one of the
                # operations that we should keep the NVRAM around for, then
                # it's probably safe to delete the NVRAM from the store.
                flow.add(tf_vm.DeleteNvram(self.nvram_mgr, instance))
                flow.add(tf_slot.DeleteSlotStore(instance, slot_mgr))

            # Build the engine & run!
            tf_eng.run(flow)

        try:
            pvm_inst_uuid = vm.get_pvm_uuid(instance)
            _setup_flow_and_run()
        except exception.InstanceNotFound:
            LOG.warning(_LW('VM was not found during destroy operation.'),
                        instance=instance)
            return
        except pvm_exc.HttpError as e:
            # See if we were operating on the LPAR that we're deleting
            # and it wasn't found
            resp = e.response
            exp = '/ManagedSystem/.*/LogicalPartition/.*-.*-.*-.*-.*'
            if resp.status == 404 and re.search(exp, resp.reqpath):
                # It's the LPAR, so just return.
                LOG.warning(_LW('VM was not found during destroy operation.'),
                            instance=instance)
                return
            else:
                # Convert to a Nova exception
                raise exception.InstanceTerminationFailure(
                    reason=six.text_type(e))
        except Exception as e:
                LOG.exception(e)
                # Convert to a Nova exception
                raise exception.InstanceTerminationFailure(
                    reason=six.text_type(e))

    def destroy(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True, migrate_data=None):
        """Destroy (shutdown and delete) the specified instance.

        If the instance is not found (for example if networking failed), this
        function should still succeed.  It's probably a good idea to log a
        warning in that case.

        :param context: security context
        :param instance: Instance object as returned by DB layer.
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param block_device_info: Information about block devices that should
                                  be detached from the instance.
        :param destroy_disks: Indicates if disks should be destroyed
        :param migrate_data: a LiveMigrateData object
        """
        if instance.task_state == task_states.RESIZE_REVERTING:
            LOG.info(_LI('Destroy called for migrated/resized instance.'),
                     instance=instance)
            # This destroy is part of resize or migrate.  It's called to
            # revert the resize/migration on the destination host.

            # Get the VM and see if we've renamed it to the resize name,
            # if not delete as usual because then we know it's not the
            # original VM.
            pvm_inst_uuid = vm.get_pvm_uuid(instance)
            vm_name = vm.get_vm_qp(self.adapter, pvm_inst_uuid,
                                   qprop='PartitionName', log_errors=False)
            if vm_name == self._gen_resize_name(instance, same_host=True):
                # Since it matches it must have been a resize, don't delete it!
                LOG.info(_LI('Ignoring destroy call during resize revert.'),
                         instance=instance)
                return

        # Run the destroy
        self._log_operation('destroy', instance)
        self._destroy(
            context, instance, block_device_info=block_device_info,
            network_info=network_info, destroy_disks=destroy_disks,
            shutdown=True)

    def attach_volume(self, context, connection_info, instance, mountpoint,
                      disk_bus=None, device_type=None, encryption=None):
        """Attach the volume to the instance at mountpoint using info."""
        self._log_operation('attach_volume', instance)

        # Define the flow
        flow = tf_lf.Flow("attach_volume")

        # Get the LPAR Wrapper
        flow.add(tf_vm.Get(self.adapter, self.host_uuid, instance))

        # Determine if there are volumes to connect.  If so, add a connection
        # for each type.
        slot_mgr = slot.build_slot_mgr(instance, self.store_api)
        vol_drv = self._get_inst_vol_adpt(context, instance,
                                          conn_info=connection_info)
        flow.add(tf_stg.ConnectVolume(vol_drv, slot_mgr))

        # Save the new slot info
        flow.add(tf_slot.SaveSlotStore(instance, slot_mgr))

        # Build the engine & run!
        engine = tf_eng.load(flow)
        engine.run()

        # The volume connector may have updated the system metadata.  Save
        # the instance to persist the data.  Spawn/destroy auto saves instance,
        # but the attach does not.  Detach does not need this save - as the
        # detach flows do not (currently) modify system metadata.  May need
        # to revise in the future as volume connectors evolve.
        instance.save()

    def detach_volume(self, connection_info, instance, mountpoint,
                      encryption=None):
        """Detach the volume attached to the instance."""
        self._log_operation('detach_volume', instance)

        # Get a volume adapter for this volume
        vol_drv = self._get_inst_vol_adpt(ctx.get_admin_context(), instance,
                                          conn_info=connection_info)

        # Before attempting to detach a volume, ensure the instance exists
        # If a live migration fails, the compute manager will call detach
        # for each volume attached to the instance, against the destination
        # host.  If the migration failed, then the VM is probably not on
        # the destination host.
        if not vm.instance_exists(self.adapter, instance, self.host_uuid):
            LOG.info(_LI('During volume detach, the instance was not found'
                         ' on this host.'), instance=instance)

            # Check if there is live migration cleanup to do on this volume.
            mig = self.live_migrations.get(instance.uuid, None)
            if mig is not None and isinstance(mig, lpm.LiveMigrationDest):
                mig.cleanup_volume(vol_drv)
            return

        # Define the flow
        flow = tf_lf.Flow("detach_volume")

        # Add a task to detach the volume
        slot_mgr = slot.build_slot_mgr(instance, self.store_api)
        flow.add(tf_stg.DisconnectVolume(vol_drv, slot_mgr))

        # Save the new slot info
        flow.add(tf_slot.SaveSlotStore(instance, slot_mgr))

        # Build the engine & run!
        engine = tf_eng.load(flow)
        engine.run()

    def snapshot(self, context, instance, image_id, update_task_state):
        """Snapshots the specified instance.

        :param context: security context
        :param instance: Instance object as returned by DB layer.
        :param image_id: Reference to a pre-created image that will
                         hold the snapshot.
        :param update_task_state: Callable to update the state of the snapshot
                                  task with one of the IMAGE_* consts from
                                  nova.compute.task_states.  Call spec
                                  (inferred from compute driver source):
            update_task_state(task_state, expected_task_state=None)
                param task_state: The nova.compute.task_states.IMAGE_* state to
                                  set.
                param expected_state: The nova.compute.task_state.IMAGE_* state
                                      which should be in place before this
                                      update.  The driver will raise if this
                                      doesn't match.
        """
        self._log_operation('snapshot', instance)

        # Define the flow
        flow = tf_lf.Flow("snapshot")

        # Notify that we're starting the process
        flow.add(tf_img.UpdateTaskState(update_task_state,
                                        task_states.IMAGE_PENDING_UPLOAD))

        # Connect the instance's boot disk to the management partition, and
        # scan the scsi bus and bring the device into the management partition.
        flow.add(tf_stg.InstanceDiskToMgmt(self.disk_dvr, instance))

        # Notify that the upload is in progress
        flow.add(tf_img.UpdateTaskState(
            update_task_state, task_states.IMAGE_UPLOADING,
            expected_state=task_states.IMAGE_PENDING_UPLOAD))

        # Stream the disk to glance
        flow.add(tf_img.StreamToGlance(context, self.image_api, image_id,
                                       instance))

        # Disconnect the boot disk from the management partition and delete the
        # device
        flow.add(tf_stg.RemoveInstanceDiskFromMgmt(self.disk_dvr, instance))

        # Build the engine & run
        tf_eng.load(flow).run()

    def rescue(self, context, instance, network_info, image_meta,
               rescue_password):
        """Rescue the specified instance.

        :param nova.context.RequestContext context:
            The context for the rescue.
        :param nova.objects.instance.Instance instance:
            The instance being rescued.
        :param nova.network.model.NetworkInfo network_info:
            Necessary network information for the resume.
        :param nova.objects.ImageMeta image_meta:
            The metadata of the image of the instance.
        :param rescue_password: new root password to set for rescue.
        """
        self._log_operation('rescue', instance)

        pvm_inst_uuid = vm.get_pvm_uuid(instance)
        # Define the flow
        flow = tf_lf.Flow("rescue")

        # Get the LPAR Wrapper
        flow.add(tf_vm.Get(self.adapter, self.host_uuid, instance))

        # Power Off the LPAR
        flow.add(tf_vm.PowerOff(self.adapter, self.host_uuid,
                                pvm_inst_uuid, instance))

        # Creates the boot image.
        flow.add(tf_stg.CreateDiskForImg(
            self.disk_dvr, context, instance, image_meta,
            image_type=disk_dvr.DiskType.RESCUE))

        # Connects up the disk to the LPAR
        flow.add(tf_stg.ConnectDisk(self.disk_dvr, context, instance))

        # Last step is to power on the system.
        flow.add(tf_vm.PowerOn(
            self.adapter, self.host_uuid, instance,
            pwr_opts={pvm_pwr.BootMode.KEY: pvm_pwr.BootMode.SMS}))

        # Build the engine & run!
        engine = tf_eng.load(flow)
        engine.run()

    def unrescue(self, instance, network_info):
        """Unrescue the specified instance.

        :param instance: nova.objects.instance.Instance
        """
        self._log_operation('unrescue', instance)

        pvm_inst_uuid = vm.get_pvm_uuid(instance)
        context = ctx.get_admin_context()

        # Define the flow
        flow = tf_lf.Flow("unrescue")

        # Get the LPAR Wrapper
        flow.add(tf_vm.Get(self.adapter, self.host_uuid, instance))

        # Power Off the LPAR
        flow.add(tf_vm.PowerOff(self.adapter, self.host_uuid,
                                pvm_inst_uuid, instance))

        # Detach the disk adapter for the rescue image
        flow.add(tf_stg.DetachDisk(self.disk_dvr, context, instance,
                                   disk_type=[disk_dvr.DiskType.RESCUE]))

        # Delete the storage disk for the rescue image
        flow.add(tf_stg.DeleteDisk(self.disk_dvr, context, instance))

        # Last step is to power on the system.
        flow.add(tf_vm.PowerOn(self.adapter, self.host_uuid, instance))

        # Build the engine & run!
        engine = tf_eng.load(flow)
        engine.run()

    def power_off(self, instance, timeout=0, retry_interval=0):
        """Power off the specified instance.

        :param instance: nova.objects.instance.Instance
        :param timeout: time to wait for GuestOS to shutdown
        :param retry_interval: How often to signal guest while
                               waiting for it to shutdown
        """
        self._log_operation('power_off', instance)
        vm.power_off(self.adapter, instance, self.host_uuid)

    def power_on(self, context, instance, network_info,
                 block_device_info=None):
        """Power on the specified instance.

        :param instance: nova.objects.instance.Instance
        """
        self._log_operation('power_on', instance)
        vm.power_on(self.adapter, instance, self.host_uuid)

    def reboot(self, context, instance, network_info, reboot_type,
               block_device_info=None, bad_volumes_callback=None):
        """Reboot the specified instance.

        After this is called successfully, the instance's state
        goes back to power_state.RUNNING. The virtualization
        platform should ensure that the reboot action has completed
        successfully even in cases in which the underlying domain/vm
        is paused or halted/stopped.

        :param instance: nova.objects.instance.Instance
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param reboot_type: Either a HARD or SOFT reboot
        :param block_device_info: Info pertaining to attached volumes
        :param bad_volumes_callback: Function to handle any bad volumes
            encountered
        """
        self._log_operation(reboot_type + ' reboot', instance)
        force_immediate = reboot_type == 'HARD'
        entry = vm.get_instance_wrapper(self.adapter, instance, self.host_uuid)
        if entry.state != pvm_bp.LPARState.NOT_ACTIVATED:
            pvm_pwr.power_off(entry, self.host_uuid, restart=True,
                              force_immediate=force_immediate)
        else:
            # pypowervm does NOT throw an exception if "already down".
            # Any other exception from pypowervm is a legitimate failure;
            # let it raise up.
            # If we get here, pypowervm thinks the instance is down.
            pvm_pwr.power_on(entry, self.host_uuid)

        # Again, pypowervm exceptions are sufficient to indicate real failure.
        # Otherwise, pypowervm thinks the instance is up.
        return True

    def get_available_resource(self, nodename):
        """Retrieve resource information.

        This method is called when nova-compute launches, and
        as part of a periodic task

        :param nodename:
            node which the caller want to get resources from
            a driver that manages only one node can safely ignore this
        :return: Dictionary describing resources
        """

        resp = self.adapter.read(pvm_ms.System.schema_type,
                                 root_id=self.host_uuid)
        if resp:
            self.host_wrapper = pvm_ms.System.wrap(resp.entry)
        # Get host information
        data = pvm_host.build_host_resource_from_ms(self.host_wrapper)

        # Add the disk information
        data["local_gb"] = self.disk_dvr.capacity
        data["local_gb_used"] = self.disk_dvr.capacity_used

        return data

    def get_host_uptime(self):
        """Returns the result of calling "uptime" on the target host."""
        # trivial implementation from libvirt/driver.py for consistency
        out, err = n_utils.execute('env', 'LANG=C', 'uptime')
        return out

    def attach_interface(self, instance, image_meta, vif):
        """Attach an interface to the instance."""
        self.plug_vifs(instance, [vif])

    def detach_interface(self, instance, vif):
        """Detach an interface from the instance."""
        self.unplug_vifs(instance, [vif])

    def plug_vifs(self, instance, network_info):
        """Plug VIFs into networks."""
        self._log_operation('plug_vifs', instance)

        # Define the flow
        flow = tf_lf.Flow("plug_vifs")

        # Get the LPAR Wrapper
        flow.add(tf_vm.Get(self.adapter, self.host_uuid, instance))

        # Run the attach
        slot_mgr = slot.build_slot_mgr(instance, self.store_api)
        flow.add(tf_net.PlugVifs(self.virtapi, self.adapter, instance,
                                 network_info, self.host_uuid, slot_mgr))

        # Save the new slot info
        flow.add(tf_slot.SaveSlotStore(instance, slot_mgr))

        # Build the engine & run!
        engine = tf_eng.load(flow)
        try:
            engine.run()
        except exception.InstanceNotFound:
            raise exception.VirtualInterfacePlugException(
                _("Plug vif failed because instance %s was not found.")
                % instance.name)
        except Exception as e:
            LOG.exception(e)
            raise exception.VirtualInterfacePlugException(
                _("Plug vif failed because of an unexpected error."))

    def unplug_vifs(self, instance, network_info):
        """Unplug VIFs from networks."""
        self._log_operation('unplug_vifs', instance)

        # Define the flow
        flow = tf_lf.Flow("unplug_vifs")

        # Get the LPAR Wrapper
        flow.add(tf_vm.Get(self.adapter, self.host_uuid, instance))

        # Run the detach
        slot_mgr = slot.build_slot_mgr(instance, self.store_api)
        flow.add(tf_net.UnplugVifs(self.adapter, instance, network_info,
                                   self.host_uuid, slot_mgr))

        # Save the new slot info
        flow.add(tf_slot.SaveSlotStore(instance, slot_mgr))

        # Build the engine & run!
        engine = tf_eng.load(flow)
        try:
            engine.run()
        except exception.InstanceNotFound as ei:
            LOG.exception(ei)
            LOG.warning(_LW('VM was not found during unplug operation '
                            'as it is already possibly deleted'),
                        instance=instance)
        except Exception as e:
            LOG.exception(e)
            raise exception.InterfaceDetachFailed(instance_uuid=instance.uuid)

    def get_available_nodes(self, refresh=False):
        """Returns nodenames of all nodes managed by the compute service.

        This method is for multi compute-nodes support. If a driver supports
        multi compute-nodes, this method returns a list of nodenames managed
        by the service. Otherwise, this method should return
        [hypervisor_hostname].
        """

        return [self.host_wrapper.mtms.mtms_str]

    def legacy_nwinfo(self):
        """Indicate if the driver requires the legacy network_info format."""
        return False

    def get_host_ip_addr(self):
        """Retrieves the IP address of the Host."""
        # This code was pulled from the libvirt driver.
        ips = compute_utils.get_machine_ips()
        if CONF.my_ip not in ips:
            LOG.warning(_LW('my_ip address (%(my_ip)s) was not found on '
                            'any of the interfaces: %(ifaces)s'),
                        {'my_ip': CONF.my_ip, 'ifaces': ", ".join(ips)})
        return CONF.my_ip

    def get_volume_connector(self, instance):
        """Get connector information for the instance for attaching to volumes.

        Connector information is a dictionary representing the ip of the
        machine that will be making the connection, the name of the iscsi
        initiator and the hostname of the machine as follows::

            {
                'ip': ip,
                'initiator': initiator,
                'host': hostname
            }

        """
        # The host ID
        connector = {'host': CONF.host}

        # The WWPNs in case of FC connection.
        vol_drv = self._get_inst_vol_adpt(ctx.get_admin_context(),
                                          instance)

        # The WWPNs in case of FC connection.
        if vol_drv is not None:
            # Override the host name.
            # TODO(IBM) See if there is a way to support a FC host name that
            # is independent of overall host name.
            connector['host'] = vol_drv.host_name()

            # Set the WWPNs
            wwpn_list = vol_drv.wwpns()
            if wwpn_list is not None:
                connector["wwpns"] = wwpn_list
        return connector

    def migrate_disk_and_power_off(self, context, instance, dest,
                                   flavor, network_info,
                                   block_device_info=None,
                                   timeout=0, retry_interval=0):

        disk_info = {}

        if flavor and flavor.root_gb < instance.root_gb:
            raise exception.InstanceFaultRollback(
                exception.ResizeError(reason=_('Cannot reduce disk size.')))

        same_host = dest == self.get_host_ip_addr()
        if same_host:
            self._log_operation('resize', instance)
        else:
            self._log_operation('migration', instance)

            # Can't migrate the disks if they are not on shared storage
            if not self._is_booted_from_volume(block_device_info):

                if not self.disk_dvr.capabilities['shared_storage']:
                    raise exception.InstanceFaultRollback(
                        exception.ResizeError(
                            reason=_('Cannot migrate local disks.')))

                # Get disk info from disk driver.
                disk_info = dict(disk_info, **self.disk_dvr.get_info())

        pvm_inst_uuid = vm.get_pvm_uuid(instance)

        # Define the migrate flow
        flow = tf_lf.Flow("migrate_vm")

        # Power off the VM
        flow.add(tf_vm.PowerOff(self.adapter, self.host_uuid,
                                pvm_inst_uuid, instance))

        if not same_host:
            # If VM is moving to a new host make sure the NVRAM is at the very
            # latest.
            flow.add(tf_vm.StoreNvram(self.nvram_mgr, instance,
                     immediate=True))
        if flavor.root_gb > instance.root_gb:
            # Resize the root disk
            flow.add(tf_stg.ExtendDisk(self.disk_dvr, context, instance,
                                       dict(type='boot'), flavor.root_gb))

        # Disconnect any volumes that are attached.  They are reattached
        # on the new VM (or existing VM if this is just a resize.)
        # Extract the block devices.
        bdms = self._extract_bdm(block_device_info)
        if bdms:
            # Create the transaction manager (FeedTask) for Storage I/O.
            xag = self._get_inst_xag(instance, bdms)
            stg_ftsk = vios.build_tx_feed_task(self.adapter, self.host_uuid,
                                               xag=xag)

            # Get the slot map.  This is so we build the client
            # adapters in the same slots.
            slot_mgr = slot.build_slot_mgr(
                instance, self.store_api, adapter=self.adapter,
                vol_drv_iter=self._vol_drv_iter(
                    context, instance, bdms=bdms, stg_ftsk=stg_ftsk))

            # Determine if there are volumes to disconnect.  If so, remove each
            # volume (within the transaction manager)
            self._add_volume_disconnection_tasks(context, instance, bdms, flow,
                                                 stg_ftsk, slot_mgr)

            # Add the transaction manager flow to the end of the 'storage
            # disconnection' tasks.  This will run all the disconnections in
            # parallel
            flow.add(stg_ftsk)

        # We rename the VM to help identify if this is a resize and so it's
        # easy to see the VM is being migrated from pvmctl.  We use the resize
        # name so we don't destroy it on a revert when it's on the same host.
        new_name = self._gen_resize_name(instance, same_host=same_host)
        flow.add(tf_vm.Rename(self.adapter, self.host_uuid, instance,
                              new_name))
        try:
            tf_eng.run(flow)
        except Exception as e:
            raise exception.InstanceFaultRollback(e)

        return disk_info

    @staticmethod
    def _gen_resize_name(instance, same_host=False):
        """Generate a temporary name for the source VM being resized/migrated.

        :param instance: nova.objects.instance.Instance being migrated/resized.
        :param same_host: Boolean indicating whether this resize is being
                          performed for the sake of a resize (True) or a
                          migration (False).
        :return: A new name which can be assigned to the source VM.
        """
        prefix = 'resize_' if same_host else 'migrate_'
        return pvm_util.sanitize_partition_name_for_api(prefix + instance.name)

    def finish_migration(self, context, migration, instance, disk_info,
                         network_info, image_meta, resize_instance,
                         block_device_info=None, power_on=True):
        """Completes a resize or cold migration.

        :param context: the context for the migration/resize
        :param migration: the migrate/resize information
        :param instance: nova.objects.instance.Instance being migrated/resized
        :param disk_info: the newly transferred disk information
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param nova.objects.ImageMeta image_meta:
            The metadata of the image of the instance.
        :param resize_instance: True if the instance disks are being resized,
                                False otherwise
        :param block_device_info: instance volume block device info
        :param power_on: True if the instance should be powered on, False
                         otherwise
        """

        # See if this was to the same host
        same_host = migration.source_compute == migration.dest_compute

        if same_host:
            self._log_operation('finish resize', instance)
        else:
            self._log_operation('finish migration', instance)

        # Ensure the disk drivers are compatible.
        if (not same_host and
                not self._is_booted_from_volume(block_device_info)):
            # Can't migrate the disks if they are not on shared storage
            if not self.disk_dvr.capabilities['shared_storage']:
                raise exception.InstanceFaultRollback(
                    exception.ResizeError(
                        reason=_('Cannot migrate local disks.')))
            # Call the disk driver to evaluate the disk info
            reason = self.disk_dvr.validate(disk_info)
            if reason:
                raise exception.InstanceFaultRollback(
                    exception.ResizeError(reason=reason))

        # Extract the block devices.
        bdms = self._extract_bdm(block_device_info)

        # Define the flow
        flow = tf_lf.Flow("finish_migration")

        # If attaching disks or volumes
        if bdms or not same_host:
            # Create the transaction manager (FeedTask) for Storage I/O.
            xag = self._get_inst_xag(instance, bdms)
            stg_ftsk = vios.build_tx_feed_task(self.adapter, self.host_uuid,
                                               xag=xag)
            # We need the slot manager
            # a) If migrating to a different host: to restore the proper slots;
            # b) If adding/removing block devices, to register the slots.
            slot_mgr = slot.build_slot_mgr(
                instance, self.store_api, adapter=self.adapter,
                vol_drv_iter=self._vol_drv_iter(
                    context, instance, bdms=bdms, stg_ftsk=stg_ftsk))
        else:
            stg_ftsk = None

        if same_host:
            # This is just a resize.
            new_name = self._gen_resize_name(instance, same_host=True)
            flow.add(tf_vm.Resize(self.adapter, self.host_wrapper, instance,
                                  instance.flavor, name=new_name))
        else:
            # This is a migration over to another host.  We have a lot of work.
            # Create the LPAR
            flow.add(tf_vm.Create(self.adapter, self.host_wrapper, instance,
                                  instance.flavor, stg_ftsk,
                                  nvram_mgr=self.nvram_mgr))

            # Create a flow for the network IO
            flow.add(tf_net.PlugVifs(self.virtapi, self.adapter, instance,
                                     network_info, self.host_uuid, slot_mgr))
            flow.add(tf_net.PlugMgmtVif(
                self.adapter, instance, self.host_uuid, slot_mgr))

            # Need to attach the boot disk, if present.
            if not self._is_booted_from_volume(block_device_info):
                flow.add(tf_stg.FindDisk(self.disk_dvr, context, instance,
                                         disk_dvr.DiskType.BOOT))

                # Connects up the disk to the LPAR
                # TODO(manas) Connect the disk flow into the slot lookup map
                flow.add(tf_stg.ConnectDisk(self.disk_dvr, context, instance,
                                            stg_ftsk=stg_ftsk))

        if bdms:
            # Determine if there are volumes to connect.  If so, add a
            # connection for each type.
            self._add_volume_connection_tasks(
                context, instance, bdms, flow, stg_ftsk, slot_mgr)

        if stg_ftsk:
            # Add the transaction manager flow to the end of the 'storage
            # connection' tasks to run all the connections in parallel
            flow.add(stg_ftsk)

        if power_on:
            # Get the lpar wrapper (required by power-on), then power-on
            flow.add(tf_vm.Get(self.adapter, self.host_uuid, instance))
            flow.add(tf_vm.PowerOn(self.adapter, self.host_uuid, instance))

        try:
            tf_eng.run(flow)
        except Exception as e:
            raise exception.InstanceFaultRollback(e)

    def confirm_migration(self, migration, instance, network_info):
        """Confirms a resize, destroying the source VM.

        :param migration: the migrate/resize information
        :param instance: nova.objects.instance.Instance
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        """
        # See if this was to the same host
        same_host = migration.source_compute == migration.dest_compute
        if same_host:
            # This was a local resize, don't delete our only VM!
            self._log_operation('confirm resize', instance)
            vm.rename(self.adapter, self.host_uuid, instance, instance.name)
            return

        # Confirming the migrate means we need to delete source VM.
        self._log_operation('confirm migration', instance)

        # Destroy the old VM.
        destroy_disks = not self.disk_dvr.capabilities['shared_storage']
        context = ctx.get_admin_context()
        self._destroy(context, instance, block_device_info=None,
                      destroy_disks=destroy_disks, shutdown=False)

    def finish_revert_migration(self, context, instance, network_info,
                                block_device_info=None, power_on=True):
        """Finish reverting a resize on the source host.

        :param context: the context for the finish_revert_migration
        :param instance: nova.objects.instance.Instance being migrated/resized
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param block_device_info: instance volume block device info
        :param power_on: True if the instance should be powered on, False
                         otherwise
        """
        self._log_operation('revert resize/migration', instance)

        # This method is always run on the source host, so we just need to
        # revert the VM back to it's old sizings, if it was even changed
        # at all.  If it was a migration, then it wasn't changed but it
        # shouldn't hurt to "update" it with the prescribed flavor.  This
        # makes it easy to handle both resize and migrate.
        #
        # The flavor should be the 'old' flavor now.
        vm.power_off(self.adapter, instance, self.host_uuid)
        vm.update(self.adapter, self.host_wrapper, instance,
                  instance.flavor)

        if power_on:
            vm.power_on(self.adapter, instance, self.host_uuid)

    def ensure_filtering_rules_for_instance(self, instance, network_info):
        """Setting up filtering rules and waiting for its completion.

        To migrate an instance, filtering rules to hypervisors
        and firewalls are inevitable on destination host.
        ( Waiting only for filtering rules to hypervisor,
        since filtering rules to firewall rules can be set faster).

        Concretely, the below method must be called.
        - setup_basic_filtering (for nova-basic, etc.)
        - prepare_instance_filter(for nova-instance-instance-xxx, etc.)

        to_xml may have to be called since it defines PROJNET, PROJMASK.
        but libvirt migrates those value through migrateToURI(),
        so , no need to be called.

        Don't use thread for this method since migration should
        not be started when setting-up filtering rules operations
        are not completed.

        :param instance: nova.objects.instance.Instance object

        """
        # No op for PowerVM
        pass

    def check_can_live_migrate_destination(self, context, instance,
                                           src_compute_info, dst_compute_info,
                                           block_migration=False,
                                           disk_over_commit=False):
        """Check if it is possible to execute live migration.

        This runs checks on the destination host, and then calls
        back to the source host to check the results.

        :param context: security context
        :param instance: nova.db.sqlalchemy.models.Instance
        :param src_compute_info: Info about the sending machine
        :param dst_compute_info: Info about the receiving machine
        :param block_migration: if true, prepare for block migration
        :param disk_over_commit: if true, allow disk over commit

        :returns: a dict containing migration info (hypervisor-dependent)
        """
        LOG.info(_LI("Checking live migration capability on destination "
                     "host."), instance=instance)

        mig = lpm.LiveMigrationDest(self, instance)
        self.live_migrations[instance.uuid] = mig
        return mig.check_destination(context, src_compute_info,
                                     dst_compute_info)

    def check_can_live_migrate_destination_cleanup(self, context,
                                                   dest_check_data):
        """Do required cleanup on dest host after check_can_live_migrate calls

        :param context: security context
        :param dest_check_data: result of check_can_live_migrate_destination
        """
        LOG.info(_LI("Cleaning up from checking live migration capability "
                     "on destination."))

    def check_can_live_migrate_source(self, context, instance,
                                      dest_check_data, block_device_info=None):
        """Check if it is possible to execute live migration.

        This checks if the live migration can succeed, based on the
        results from check_can_live_migrate_destination.

        :param context: security context
        :param instance: nova.db.sqlalchemy.models.Instance
        :param dest_check_data: result of check_can_live_migrate_destination
        :param block_device_info: result of _get_instance_block_device_info
        :returns: a dict containing migration info (hypervisor-dependent)
        """
        LOG.info(_LI("Checking live migration capability on source host."),
                 instance=instance)
        mig = lpm.LiveMigrationSrc(self, instance, dest_check_data)
        self.live_migrations[instance.uuid] = mig

        # Get a volume driver for each volume
        vol_drvs = self._build_vol_drivers(context, instance,
                                           block_device_info)

        return mig.check_source(context, block_device_info, vol_drvs)

    def pre_live_migration(self, context, instance, block_device_info,
                           network_info, disk_info, migrate_data=None):
        """Prepare an instance for live migration

        :param context: security context
        :param instance: nova.objects.instance.Instance object
        :param block_device_info: instance block device information
        :param network_info: instance network information
        :param disk_info: instance disk information
        :param migrate_data: a LiveMigrateData object
        """
        LOG.info(_LI("Pre live migration processing."),
                 instance=instance)
        mig = self.live_migrations[instance.uuid]

        # Get a volume driver for each volume
        vol_drvs = self._build_vol_drivers(context, instance,
                                           block_device_info)

        # Run pre-live migration
        return mig.pre_live_migration(context, block_device_info, network_info,
                                      disk_info, migrate_data, vol_drvs)

    def live_migration(self, context, instance, dest,
                       post_method, recover_method, block_migration=False,
                       migrate_data=None):
        """Live migration of an instance to another host.

        :param context: security context
        :param instance:
            nova.db.sqlalchemy.models.Instance object
            instance object that is migrated.
        :param dest: destination host
        :param post_method:
            post operation method.
            expected nova.compute.manager._post_live_migration.
        :param recover_method:
            recovery method when any exception occurs.
            expected nova.compute.manager._rollback_live_migration.
        :param block_migration: if true, migrate VM disk.
        :param migrate_data: a LiveMigrateData object

        """
        self._log_operation('live_migration', instance)
        try:
            mig = self.live_migrations[instance.uuid]
            try:
                mig.live_migration(context, migrate_data)
            except pvm_exc.JobRequestTimedOut as timeout_ex:
                # If the migration operation exceeds configured timeout
                LOG.error(_LE("Live migration timed out. Aborting migration"),
                          instance=instance)
                mig.migration_abort()
                self._migration_exception_util(context, instance, dest,
                                               recover_method,
                                               block_migration, migrate_data,
                                               mig, ex=timeout_ex)
            except Exception as e:
                LOG.exception(e)
                self._migration_exception_util(context, instance, dest,
                                               recover_method,
                                               block_migration, migrate_data,
                                               mig, ex=e)

            LOG.debug("Calling post live migration method.", instance=instance)
            # Post method to update host in OpenStack and finish live-migration
            post_method(context, instance, dest, block_migration, migrate_data)
        finally:
            # Remove the migration record on the source side.
            del self.live_migrations[instance.uuid]

    def _migration_exception_util(self, context, instance, dest,
                                  recover_method, block_migration,
                                  migrate_data, mig, ex):
        """Migration exception utility.

        :param context: security context
        :param instance:
            nova.db.sqlalchemy.models.Instance object
            instance object that is migrated.
        :param dest: destination host
        :param recover_method:
            recovery method when any exception occurs.
            expected nova.compute.manager._rollback_live_migration.
        :param block_migration: if true, migrate VM disk.
        :param migrate_data: a LiveMigrateData object
        :param mig: live_migration object
        :param ex: exception reason

        """
        LOG.warning(_LW("Rolling back live migration."), instance=instance)
        try:
            mig.rollback_live_migration(context)
            recover_method(context, instance, dest, block_migration,
                           migrate_data)
        except Exception as e:
            LOG.exception(e)

        raise lpm.LiveMigrationFailed(name=instance.name,
                                      reason=six.text_type(ex))

    def rollback_live_migration_at_destination(self, context, instance,
                                               network_info,
                                               block_device_info,
                                               destroy_disks=True,
                                               migrate_data=None):
        """Clean up destination node after a failed live migration.

        :param context: security context
        :param instance: instance object that was being migrated
        :param network_info: instance network information
        :param block_device_info: instance block device information
        :param destroy_disks:
            if true, destroy disks at destination during cleanup
        :param migrate_data: a LiveMigrateData object

        """
        del self.live_migrations[instance.uuid]

    def check_instance_shared_storage_local(self, context, instance):
        """Check if instance files located on shared storage.

        This runs check on the destination host, and then calls
        back to the source host to check the results.

        :param context: security context
        :param instance: nova.objects.instance.Instance object
        """
        # Defer to the disk driver method.
        return self.disk_dvr.check_instance_shared_storage_local(
            context, instance)

    def check_instance_shared_storage_remote(self, context, data):
        """Check if instance files located on shared storage.

        :param context: security context
        :param data: result of check_instance_shared_storage_local
        """
        # Defer to the disk driver method.
        return self.disk_dvr.check_instance_shared_storage_remote(
            context, data)

    def check_instance_shared_storage_cleanup(self, context, data):
        """Do cleanup on host after check_instance_shared_storage calls

        :param context: security context
        :param data: result of check_instance_shared_storage_local
        """
        # Defer to the disk driver method.
        return self.disk_dvr.check_instance_shared_storage_cleanup(
            context, data)

    def post_live_migration(self, context, instance, block_device_info,
                            migrate_data=None):
        """Post operation of live migration at source host.

        :param context: security context
        :instance: instance object that was migrated
        :block_device_info: instance block device information
        :param migrate_data: a LiveMigrateData object
        """
        # Build the volume drivers
        vol_drvs = self._build_vol_drivers(context, instance,
                                           block_device_info)

        mig = self.live_migrations[instance.uuid]
        mig.post_live_migration(vol_drvs, migrate_data)

    def post_live_migration_at_source(self, context, instance, network_info):
        """Unplug VIFs from networks at source.

        :param context: security context
        :param instance: instance object reference
        :param network_info: instance network information
        """
        LOG.info(_LI("Post live migration processing on source host."),
                 instance=instance)
        mig = self.live_migrations[instance.uuid]
        mig.post_live_migration_at_source(network_info)

    def post_live_migration_at_destination(self, context, instance,
                                           network_info,
                                           block_migration=False,
                                           block_device_info=None):
        """Post operation of live migration at destination host.

        :param context: security context
        :param instance: instance object that is migrated
        :param network_info: instance network information
        :param block_migration: if true, post operation of block_migration.
        """
        LOG.info(_LI("Post live migration processing on destination host."),
                 instance=instance)
        mig = self.live_migrations[instance.uuid]
        mig.instance = instance

        # Build the volume drivers
        vol_drvs = self._build_vol_drivers(context, instance,
                                           block_device_info)

        # Run post live migration
        mig.post_live_migration_at_destination(network_info, vol_drvs)
        del self.live_migrations[instance.uuid]

    def _vol_drv_iter(self, context, instance, block_device_info=None,
                      bdms=None, stg_ftsk=None):
        """Yields a bdm and volume driver."""
        # Get a volume driver for each volume
        if not bdms:
            bdms = self._extract_bdm(block_device_info)
        for bdm in bdms or []:
            conn_info = bdm.get('connection_info')
            # if it doesn't have connection_info, it's not a volume
            if not conn_info:
                continue

            vol_drv = self._get_inst_vol_adpt(context, instance,
                                              conn_info=conn_info,
                                              stg_ftsk=stg_ftsk)
            yield bdm, vol_drv

    def _build_vol_drivers(self, context, instance, block_device_info=None,
                           bdms=None, stg_ftsk=None):
        """Builds the volume connector drivers for a block device info."""
        # Get a volume driver for each volume
        return [vol_drv for bdm, vol_drv in self._vol_drv_iter(
            context, instance, block_device_info=block_device_info, bdms=bdms,
            stg_ftsk=stg_ftsk)]

    def unfilter_instance(self, instance, network_info):
        """Stop filtering instance."""
        # No op for PowerVM
        pass

    @staticmethod
    def _extract_bdm(block_device_info):
        """Returns the block device mapping out of the block device info.

        The block device mapping is a list of instances of block device
        classes from nova.virt.block_device.  Each block device
        represents one volume connection.

        An example string representation of the a DriverVolumeBlockDevice
        from the early Liberty time frame is:
        {'guest_format': None,
        'boot_index': 0,
        'mount_device': u'/dev/sda',
        'connection_info': {u'driver_volume_type': u'fibre_channel',
                            u'serial': u'e11765ea-dd14-4aa9-a953-4fd6b4999635',
                            u'data': {u'initiator_target_map':
                                        {u'21000024ff747e59':
                                            [u'500507680220E522',
                                            u'500507680210E522'],
                                        u'21000024ff747e58':
                                            [u'500507680220E522',
                                            u'500507680210E522']},
                                        u'vendor': u'IBM',
                                        u'target_discovered':False,
                                        u'target_UID': u'600507680282...',
                                        u'qos_specs': None,
                                        u'volume_id': u'e11765ea-...',
                                        u'target_lun': u'2',
                                        u'access_mode': u'rw',
                                        u'target_wwn': u'500507680220E522'}
                            },
        'disk_bus': None,
        'device_type': u'disk',
        'delete_on_termination': True}
        """
        if block_device_info is None:
            return []
        return block_device_info.get('block_device_mapping', [])

    def get_vnc_console(self, context, instance):
        """Get connection info for a vnc console.

        :param context: security context
        :param instance: nova.objects.instance.Instance

        :return: An instance of console.type.ConsoleVNC
        """
        self._log_operation('get_vnc_console', instance)
        lpar_uuid = vm.get_pvm_uuid(instance)

        # Build the connection to the VNC.
        host = CONF.vnc.vncserver_proxyclient_address
        port = pvm_vterm.open_remotable_vnc_vterm(
            self.adapter, lpar_uuid, host, vnc_path=lpar_uuid)

        # Note that the VNC viewer will wrap the internal_access_path with
        # the HTTP content.
        return console_type.ConsoleVNC(host=host, port=port,
                                       internal_access_path=lpar_uuid)

    def _get_inst_xag(self, instance, bdms, recreate=False):
        """Returns the extended attributes required for a given instance.

        This is used in coordination with the FeedTask.  It identifies ahead
        of time what each request requires for its general operations.

        :param instance: Nova instance for which the volume adapter is needed.
        :param bdms: The BDMs for the operation.
        :param recreate: (Optional, Default: False) If set to true, will return
                         all of the storage XAGs so that a full scrub can be
                         done (since specific slots are needed).
        :return: List of extended attributes required for the operation.
        """
        if recreate:
            return {pvm_const.XAG.VIO_FMAP, pvm_const.XAG.VIO_SMAP,
                    pvm_const.XAG.VIO_STOR}
        # All operations for deploy/destroy require scsi by default.  This is
        # either vopt, local/SSP disks, etc...
        xags = {pvm_const.XAG.VIO_SMAP}
        if not bdms:
            LOG.debug('Instance XAGs for VM %(inst)s is %(xags)s.',
                      {'inst': instance.name,
                       'xags': ','.join(xags)})
            return list(xags)

        # If we have any volumes, add the volumes required mapping XAGs.
        adp_type = vol_attach.FC_STRATEGY_MAPPING[
            CONF.powervm.fc_attach_strategy.lower()]
        vol_cls = importutils.import_class(adp_type)
        xags.update(set(vol_cls.min_xags()))
        LOG.debug('Instance XAGs for VM %(inst)s is %(xags)s.',
                  {'inst': instance.name,
                   'xags': ','.join(xags)})
        return list(xags)

    def _get_inst_vol_adpt(self, context, instance, conn_info=None,
                           stg_ftsk=None):
        """Returns the appropriate volume driver based on connection type.

        Checks the connection info for connection-type and return the
        connector, if no connection info is provided returns the default
        connector.
        :param context: security context
        :param instance: Nova instance for which the volume adapter is needed.
        :param conn_info: BDM connection information of the instance to
                          get the volume adapter type (vSCSI/NPIV) requested.
        :param stg_ftsk: (Optional) The FeedTask that can be used to defer the
                         mapping actions against the Virtual I/O Server for. If
                         not provided, then the connect/disconnect actions will
                         be immediate.
        :return: Returns the volume adapter, if conn_info is not passed then
                 returns the volume adapter based on the CONF
                 fc_attach_strategy property (npiv/vscsi). Otherwise returns
                 the adapter based on the connection-type of
                 connection_info.
        """
        adp_type = vol_attach.FC_STRATEGY_MAPPING[
            CONF.powervm.fc_attach_strategy]
        vol_cls = importutils.import_class(adp_type)
        if conn_info:
            LOG.debug('Volume Adapter returned for connection_info=%s' %
                      conn_info)
        LOG.debug('Volume Adapter class %(cls)s for instance %(inst)s' %
                  {'cls': vol_cls.__name__, 'inst': instance.name})
        return vol_cls(self.adapter, self.host_uuid,
                       instance, conn_info, stg_ftsk=stg_ftsk)

    def _get_boot_connectivity_type(self, context, bdms, block_device_info):
        """Get connectivity information for the instance.

        :param context: security context
        :param bdms: The BDMs for the operation. If boot volume of
                     the instance is ssp lu or local disk, the bdms is None.
        :param block_device_info: Instance volume block device info.
        :return: Returns the boot connectivity type,
                 If boot volume is a npiv volume, returns 'npiv'
                 Otherwise, return 'vscsi'.
        """
        # Set default boot_conn_type as 'vscsi'
        boot_conn_type = 'vscsi'
        if self._is_booted_from_volume(block_device_info) and bdms is not None:
            for bdm in bdms:
                if bdm.get('boot_index') == 0:
                    conn_info = bdm.get('connection_info')
                    connectivity_type = conn_info['data']['connection-type']
                    boot_conn_type = ('vscsi' if connectivity_type ==
                                      'pv_vscsi' else connectivity_type)
                    return boot_conn_type
        else:
            return boot_conn_type


class NovaEventHandler(pvm_apt.RawEventHandler):
    """Used to receive and handle events from PowerVM."""
    inst_actions_handled = {'PartitionState', 'NVRAM'}

    def __init__(self, driver):
        self._driver = driver

    def _handle_event(self, uri, etype, details, eid):
        """Handle an individual event.

        :param uri: PowerVM event uri
        :param etype: PowerVM event type
        :param details: PowerVM event details
        :param eid: PowerVM event id
        """

        # See if this uri ends with a PowerVM UUID.
        if not pvm_util.is_instance_path(uri):
            return

        pvm_uuid = pvm_util.get_req_path_uuid(
            uri, preserve_case=True)
        # If a vm event and one we handle, call the inst handler.
        if (uri.endswith('LogicalPartition/' + pvm_uuid) and
                (self.inst_actions_handled & set(details))):
            inst = vm.get_instance(ctx.get_admin_context(),
                                   pvm_uuid)
            if inst:
                LOG.debug('Handle action "%(action)s" event for instance: '
                          '%(inst)s' %
                          dict(action=details, inst=inst.name))
                self._handle_inst_event(
                    inst, pvm_uuid, uri, etype, details, eid)

    def _handle_inst_event(self, inst, pvm_uuid, uri, etype, details, eid):
        """Handle an instance event.

        This method will check if an instance event signals a change in the
        state of the instance as known to OpenStack and if so, trigger an
        event upward.

        :param inst: the instance object.
        :param pvm_uuid: the PowerVM uuid of the vm
        :param uri: PowerVM event uri
        :param etype: PowerVM event type
        :param details: PowerVM event details
        :param eid: PowerVM event id
        """
        # If the state of the vm changed see if it should be handled
        if 'PartitionState' in details:
            # Get the current state
            pvm_state = vm.get_vm_qp(self._driver.adapter, pvm_uuid,
                                     'PartitionState')
            # See if it's really a change of state from what OpenStack knows
            transition = vm.translate_event(pvm_state, inst.power_state)
            if transition is not None:
                LOG.debug('New state for instance: %s', pvm_state,
                          instance=inst)
                # Now create an event and sent it.
                lce = event.LifecycleEvent(inst.uuid, transition)
                LOG.info(_LI('Sending life cycle event for instance state '
                             'change to: %s'), pvm_state, instance=inst)
                self._driver.emit_event(lce)

        # If the NVRAM has changed for this instance and a store is configured.
        if 'NVRAM' in details and self._driver.nvram_mgr is not None:
            # Schedule the NVRAM for the instance to be stored.
            self._driver.nvram_mgr.store(inst)

    def process(self, events):
        """Process the event that comes back from PowerVM.

        Example of event data:
            <EventType kb="ROR" kxe="false">NEW_CLIENT</EventType>
            <EventID kxe="false" kb="ROR">1452692619554</EventID>
            <EventData kxe="false" kb="ROR"/>
            <EventDetail kb="ROR" kxe="false"/>

            <EventType kb="ROR" kxe="false">MODIFY_URI</EventType>
            <EventID kxe="false" kb="ROR">1452692619557</EventID>
            <EventData kxe="false" kb="ROR">http://localhost:12080/rest/api/
                uom/ManagedSystem/c889bf0d-9996-33ac-84c5-d16727083a77
            </EventData>
            <EventDetail kb="ROR" kxe="false">Other</EventDetail>

            <EventType kb="ROR" kxe="false">MODIFY_URI</EventType>
            <EventID kxe="false" kb="ROR">1452692619566</EventID>
            <EventData kxe="false" kb="ROR">http://localhost:12080/rest/api/
                uom/ManagedSystem/c889bf0d-9996-33ac-84c5-d16727083a77/
                LogicalPartition/794654F5-B6E9-4A51-BEC2-A73E41EAA938
            </EventData>
            <EventDetail kb="ROR" kxe="false">RMCState,PartitionState,Other
            </EventDetail>

        :param events: A sequence of event dicts that has come back from the
                       system.

                       Format:
                       [
                            {
                               'EventType': <type>,
                               'EventID': <id>,
                               'EventData': <data>,
                               'EventDetail': <detail>
                            },
                       ]
        """
        for pvm_event in events:
            try:
                # Pull all the pieces of the event.
                uri = pvm_event['EventData']
                etype = pvm_event['EventType']
                details = pvm_event['EventDetail']
                details = details.split(',') if details else []
                eid = pvm_event['EventID']

                if etype not in ['NEW_CLIENT']:
                    LOG.debug('PowerVM Event-Action: %s URI: %s Details %s' %
                              (etype, uri, details))
                    self._handle_event(uri, etype, details, eid)
            except Exception as e:
                LOG.exception(e)
                LOG.warning(_LW('Unable to parse event URI: %s from PowerVM.'),
                            uri)
