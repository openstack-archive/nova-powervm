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

from nova import block_device
from nova.compute import task_states
from nova.compute import utils as compute_utils
from nova.console import type as console_type
from nova import context as ctx
from nova import exception
from nova import image
from nova.i18n import _LI, _LW, _
from nova.objects import flavor as flavor_obj
from nova import utils as n_utils
from nova.virt import configdrive
from nova.virt import driver
import re
import time

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import importutils
import six
from taskflow import engines as tf_eng
from taskflow.patterns import linear_flow as tf_lf

from pypowervm import adapter as pvm_apt
from pypowervm import exceptions as pvm_exc
from pypowervm.helpers import log_helper as log_hlp
from pypowervm.helpers import vios_busy as vio_hlp
from pypowervm.tasks import power as pvm_pwr
from pypowervm.tasks import vterm as pvm_vterm
from pypowervm.utils import retry as pvm_retry
from pypowervm.wrappers import base_partition as pvm_bp
from pypowervm.wrappers import managed_system as pvm_ms

from nova_powervm.virt.powervm.disk import driver as disk_dvr
from nova_powervm.virt.powervm import host as pvm_host
from nova_powervm.virt.powervm import live_migration as lpm
from nova_powervm.virt.powervm import mgmt
from nova_powervm.virt.powervm.tasks import image as tf_img
from nova_powervm.virt.powervm.tasks import network as tf_net
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


class PowerVMDriver(driver.ComputeDriver):

    """PowerVM Implementation of Compute Driver."""

    def __init__(self, virtapi):
        super(PowerVMDriver, self).__init__(virtapi)

    def init_host(self, host):
        """Initialize anything that is necessary for the driver to function,
        including catching up with currently running VM's on the given host.
        """

        # Live migrations
        self.live_migrations = {}
        # Get an adapter
        self._get_adapter()
        # First need to resolve the managed host UUID
        self._get_host_uuid()
        # Get the management partition
        self.mp_uuid = mgmt.get_mgmt_partition(self.adapter).uuid

        # Initialize the disk adapter.  Sets self.disk_dvr
        self._get_disk_adapter()
        self.image_api = image.API()

        # Init Host CPU Statistics
        self.host_cpu_stats = pvm_host.HostCPUStats(self.adapter,
                                                    self.host_uuid)

        LOG.info(_LI("The compute driver has been initialized."))

    def _get_adapter(self):
        self.session = pvm_apt.Session()
        self.adapter = pvm_apt.Adapter(
            self.session, helpers=[log_hlp.log_helper,
                                   vio_hlp.vios_busy_retry_helper])

    def _get_disk_adapter(self):
        conn_info = {'adapter': self.adapter, 'host_uuid': self.host_uuid,
                     'mp_uuid': self.mp_uuid}

        self.disk_dvr = importutils.import_object_ns(
            DISK_ADPT_NS, DISK_ADPT_MAPPINGS[CONF.powervm.disk_driver],
            conn_info)

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
        """Log entry point of driver operations
        """
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

    def list_instances(self):
        """Return the names of all the instances known to the virtualization
        layer, as a list.
        """
        lpar_list = vm.get_lpar_names(self.adapter)
        return lpar_list

    def get_host_cpu_stats(self):
        """Return the current CPU state of the host."""
        return self.host_cpu_stats.get_host_cpu_stats()

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None,
              flavor=None):
        """Create a new instance/VM/domain on the virtualization platform.

        Once this successfully completes, the instance should be
        running (power_state.RUNNING).

        If this fails, any partial instance should be completely
        cleaned up, and the virtualization platform should be in the state
        that it was before this call began.

        :param context: security context
        :param instance: Instance object as returned by DB layer.
                         This function should use the data there to guide
                         the creation of the new instance.
        :param image_meta: image object returned by nova.image.glance that
                           defines the image from which to boot this instance
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

        # Define the flow
        flow_spawn = tf_lf.Flow("spawn")

        # Create the LPAR
        flow_spawn.add(tf_vm.Create(self.adapter, self.host_wrapper, instance,
                                    flavor))

        # Create a flow for the IO
        flow_spawn.add(tf_net.PlugVifs(self.virtapi, self.adapter, instance,
                                       network_info, self.host_uuid))
        flow_spawn.add(tf_net.PlugMgmtVif(self.adapter, instance,
                                          self.host_uuid))

        # Create the transaction manager (FeedTask) for Storage I/O.
        tx_mgr = vios.build_tx_feed_task(self.adapter, self.host_uuid)

        # Only add the image disk if this is from Glance.
        if not self._is_booted_from_volume(block_device_info):
            # Creates the boot image.
            flow_spawn.add(tf_stg.CreateDiskForImg(
                self.disk_dvr, context, instance, image_meta,
                disk_size=flavor.root_gb))

            # Connects up the disk to the LPAR
            flow_spawn.add(tf_stg.ConnectDisk(self.disk_dvr, context, instance,
                                              tx_mgr))

        # Determine if there are volumes to connect.  If so, add a connection
        # for each type.
        bdms = self._extract_bdm(block_device_info)
        if bdms is not None:
            for bdm in bdms:
                conn_info = bdm.get('connection_info')
                vol_drv = self._get_inst_vol_adpt(
                    context, instance, conn_info=conn_info, tx_mgr=tx_mgr)

                # First connect the volume.  This will update the
                # connection_info.
                flow_spawn.add(tf_stg.ConnectVolume(vol_drv))

                # Save the BDM so that the updated connection info is
                # persisted.
                flow_spawn.add(tf_stg.SaveBDM(bdm, instance))

        # If the config drive is needed, add those steps.  Should be done
        # after all the other I/O.
        if configdrive.required_by(instance):
            flow_spawn.add(tf_stg.CreateAndConnectCfgDrive(
                self.adapter, self.host_uuid, instance, injected_files,
                network_info, admin_password))

        # Add the transaction manager flow to the end of the 'I/O
        # connection' tasks.  This will run all the connections in parallel.
        flow_spawn.add(tx_mgr)

        # Last step is to power on the system.
        flow_spawn.add(tf_vm.PowerOn(self.adapter, self.host_uuid, instance))

        # Build the engine & run!
        engine = tf_eng.load(flow_spawn, engine='parallel', max_workers=3)
        engine.run()

    def _is_booted_from_volume(self, block_device_info):
        """Determine whether the root device is listed in block_device_info.

        If it is, this can be considered a 'boot from Cinder Volume'.

        :param block_device_info: The block device info from the compute
                                  manager.
        :return: True if the root device is in block_device_info and False if
                 it is not.
        """
        root_bdm = block_device.get_root_bdm(
            driver.block_device_info_get_mapping(block_device_info))
        return (root_bdm is not None)

    @property
    def need_legacy_block_device_info(self):
        return False

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
        :param migrate_data: implementation specific params
        """

        def _run_flow():
            # Define the flow
            flow = tf_lf.Flow("destroy")

            # Power Off the LPAR
            flow.add(tf_vm.PowerOff(self.adapter, self.host_uuid,
                                    pvm_inst_uuid, instance))

            # Create the transaction manager (FeedTask) for Storage I/O.
            tx_mgr = vios.build_tx_feed_task(self.adapter, self.host_uuid)

            # Add the disconnect/deletion of the vOpt to the transaction
            # manager.
            flow.add(tf_stg.DeleteVOpt(self.adapter, self.host_uuid, instance,
                                       pvm_inst_uuid, tx_mgr=tx_mgr))

            # Determine if there are volumes to disconnect.  If so, remove each
            # volume (within the transaction manager)
            bdms = self._extract_bdm(block_device_info)
            if bdms is not None:
                for bdm in bdms:
                    conn_info = bdm.get('connection_info')
                    vol_drv = self._get_inst_vol_adpt(
                        context, instance, conn_info=conn_info, tx_mgr=tx_mgr)
                    flow.add(tf_stg.DisconnectVolume(vol_drv))

            # Only attach the disk adapters if this is not a boot from volume.
            destroy_disk_task = None
            if not self._is_booted_from_volume(block_device_info):
                # Detach the disk storage adapters (when the tx_mgr runs)
                flow.add(tf_stg.DetachDisk(
                    self.disk_dvr, context, instance, tx_mgr))

                # Delete the storage disks
                if destroy_disks:
                    destroy_disk_task = tf_stg.DeleteDisk(
                        self.disk_dvr, context, instance)

            # Add the transaction manager flow to the end of the 'storage
            # connection' tasks.  This will run all the connections in parallel
            flow.add(tx_mgr)

            # The disks shouldn't be destroyed until the unmappings are done.
            if destroy_disk_task:
                flow.add(destroy_disk_task)

            # Last step is to delete the LPAR from the system.
            # Note: If moving to a Graph Flow, will need to change to depend on
            # the prior step.
            flow.add(tf_vm.Delete(self.adapter, pvm_inst_uuid, instance))

            # Build the engine & run!
            engine = tf_eng.load(flow)
            engine.run()

        self._log_operation('destroy', instance)
        if instance.task_state == task_states.RESIZE_REVERTING:
            # This destroy is part of resize, just skip destroying
            # TODO(IBM): What to do longer term
            LOG.info(_LI('Ignoring destroy call during resize revert.'))
            return

        try:
            pvm_inst_uuid = vm.get_pvm_uuid(instance)
            _run_flow()
        except exception.InstanceNotFound:
            LOG.warn(_LW('VM was not found during destroy operation.'),
                     instance=instance)
            return
        except pvm_exc.HttpError as e:
            # See if we were operating on the LPAR that we're deleting
            # and it wasn't found
            resp = e.response
            exp = '/ManagedSystem/.*/LogicalPartition/.*-.*-.*-.*-.*'
            if (resp.status == 404 and re.search(exp, resp.reqpath)):
                # It's the LPAR, so just return.
                LOG.warn(_LW('VM was not found during destroy operation.'),
                         instance=instance)
                return
            else:
                raise

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
        vol_drv = self._get_inst_vol_adpt(context, instance,
                                          conn_info=connection_info)
        flow.add(tf_stg.ConnectVolume(vol_drv))

        # Build the engine & run!
        engine = tf_eng.load(flow)
        engine.run()

    def detach_volume(self, connection_info, instance, mountpoint,
                      encryption=None):
        """Detach the volume attached to the instance."""
        self._log_operation('detach_volume', instance)

        # Define the flow
        flow = tf_lf.Flow("detach_volume")

        # Determine if there are volumes to connect.  If so, add a connection
        # for each type.
        vol_drv = self._get_inst_vol_adpt(ctx.get_admin_context(), instance,
                                          conn_info=connection_info)
        flow.add(tf_stg.DisconnectVolume(vol_drv))

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

        :param instance: nova.objects.instance.Instance
        """
        self._log_operation('rescue', instance)

        # We need the image size, which isn't in the system meta data
        # so get the all the info.
        image_meta = self.image_api.get(context, image_meta['id'])

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
        # TODO(IBM): Currently, sending the bootmode=sms options causes
        # the poweron job to fail.  Bypass it for now.  The VM can be
        # powered on manually to sms.
        # flow.add(tf_vm.PowerOn(self.adapter, self.host_uuid,
        #                       instance, pwr_opts=dict(bootmode='sms')))

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
        flow.add(tf_net.PlugVifs(self.virtapi, self.adapter, instance,
                                 network_info, self.host_uuid))

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
        flow.add(tf_net.UnplugVifs(self.adapter, instance, network_info,
                                   self.host_uuid))

        # Build the engine & run!
        engine = tf_eng.load(flow)
        engine.run()

    def get_available_nodes(self, refresh=False):
        """Returns nodenames of all nodes managed by the compute service.

        This method is for multi compute-nodes support. If a driver supports
        multi compute-nodes, this method returns a list of nodenames managed
        by the service. Otherwise, this method should return
        [hypervisor_hostname].
        """

        return [self.host_wrapper.mtms.mtms_str]

    def legacy_nwinfo(self):
        """Indicate if the driver requires the legacy network_info format.
        """
        return False

    def get_host_ip_addr(self):
        """Retrieves the IP address of the dom0
        """
        # This code was pulled from the libvirt driver.
        ips = compute_utils.get_machine_ips()
        if CONF.my_ip not in ips:
            LOG.warn(_LW('my_ip address (%(my_ip)s) was not found on '
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

        # We may be passed a flavor that is in dict format, but the
        # downstream code is expecting an object, so convert it.
        if flavor and not isinstance(flavor, flavor_obj.Flavor):
            flav_obj = flavor_obj.Flavor.get_by_id(context, flavor['id'])
        else:
            flav_obj = flavor

        if flav_obj and flav_obj.root_gb < instance.root_gb:
            raise exception.InstanceFaultRollback(
                exception.ResizeError(reason=_('Cannot reduce disk size.')))

        if dest == self.get_host_ip_addr():
            self._log_operation('resize', instance)
            # This is a local resize
            # Check for disk resizes before VM resources
            if flav_obj.root_gb > instance.root_gb:
                vm.power_off(self.adapter, instance, self.host_uuid)
                # Resize the root disk
                self.disk_dvr.extend_disk(context, instance, dict(type='boot'),
                                          flav_obj.root_gb)

            # Do any VM resource changes
            self._resize_vm(context, instance, flav_obj, retry_interval)
        else:
            self._log_operation('migration', instance)
            raise NotImplementedError()

        # TODO(IBM): The caller is expecting disk info returned
        return disk_info

    def _resize_vm(self, context, instance, flav_obj, retry_interval=0):

        def _delay(attempt, max_attempts, *args, **kwds):
            LOG.info(_LI('Retrying to update VM.'), instance=instance)
            time.sleep(retry_interval)

        @pvm_retry.retry(delay_func=_delay)
        def _update_vm():
            LOG.debug('Resizing instance %s.', instance.name,
                      instance=instance)
            entry = vm.get_instance_wrapper(self.adapter, instance,
                                            self.host_uuid)

            pwrd = vm.power_off(self.adapter, instance,
                                self.host_uuid, entry=entry)
            # If it was powered off then the etag changed, fetch it again
            if pwrd:
                entry = vm.get_instance_wrapper(self.adapter, instance,
                                                self.host_uuid)

            vm.update(self.adapter, self.host_wrapper,
                      instance, flav_obj, entry=entry)

        # Update the VM
        _update_vm()

    def finish_migration(self, context, migration, instance, disk_info,
                         network_info, image_meta, resize_instance,
                         block_device_info=None, power_on=True):
        """Completes a resize.

        :param context: the context for the migration/resize
        :param migration: the migrate/resize information
        :param instance: nova.objects.instance.Instance being migrated/resized
        :param disk_info: the newly transferred disk information
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param image_meta: image object returned by nova.image.glance that
                           defines the image from which this instance
                           was created
        :param resize_instance: True if the instance is being resized,
                                False otherwise
        :param block_device_info: instance volume block device info
        :param power_on: True if the instance should be powered on, False
                         otherwise
        """
        # TODO(IBM): Finish this up

        if power_on:
            vm.power_on(self.adapter, instance, self.host_uuid)

    def confirm_migration(self, migration, instance, network_info):
        """Confirms a resize, destroying the source VM.

        :param migration: the migrate/resize information
        :param instance: nova.objects.instance.Instance
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        """
        # TODO(IBM): Anything to do here?
        pass

    def finish_revert_migration(self, context, instance, network_info,
                                block_device_info=None, power_on=True):
        """Finish reverting a resize.

        :param context: the context for the finish_revert_migration
        :param instance: nova.objects.instance.Instance being migrated/resized
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param block_device_info: instance volume block device info
        :param power_on: True if the instance should be powered on, False
                         otherwise
        """
        self._log_operation('revert resize', instance)
        # TODO(IBM): What to do here?  Do we want to recreate the LPAR
        # Or just change the settings back to the flavor?

        # Get the flavor from the instance, so we can revert it
        admin_ctx = ctx.get_admin_context(read_deleted='yes')
        flav_obj = (
            flavor_obj.Flavor.get_by_id(admin_ctx,
                                        instance.instance_type_id))
        # TODO(IBM)  Get the entry once for both power_off and update
        vm.power_off(self.adapter, instance, self.host_uuid)
        vm.update(self.adapter, self.host_uuid, instance, flav_obj)

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
        return mig.check_source(context, block_device_info)

    def pre_live_migration(self, context, instance, block_device_info,
                           network_info, disk_info, migrate_data=None):
        """Prepare an instance for live migration

        :param context: security context
        :param instance: nova.objects.instance.Instance object
        :param block_device_info: instance block device information
        :param network_info: instance network information
        :param disk_info: instance disk information
        :param migrate_data: implementation specific data dict.
        """
        LOG.info(_LI("Pre live migration processing."),
                 instance=instance)
        mig = self.live_migrations[instance.uuid]
        mig.pre_live_migration(context, block_device_info, network_info,
                               disk_info, migrate_data)

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
        :param migrate_data: implementation specific params.

        """
        self._log_operation('live_migration', instance)
        try:
            mig = self.live_migrations[instance.uuid]
            try:
                mig.live_migration(context, migrate_data)
            except Exception as e:
                LOG.exception(e)
                LOG.debug("Rolling back live migration.", instance=instance)
                mig.rollback_live_migration(context)
                recover_method(context, instance, dest,
                               block_migration, migrate_data)
                raise lpm.LiveMigrationFailed(name=instance.name,
                                              reason=six.text_type(e))

            LOG.debug("Calling post live migration method.", instance=instance)
            # Post method to update host in OpenStack and finish live-migration
            post_method(context, instance, dest, block_migration, migrate_data)
        finally:
            # Remove the migration record on the source side.
            del self.live_migrations[instance.uuid]

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
        :param migrate_data: implementation specific params

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
        :param migrate_data: if not None, it is a dict which has data
        """
        pass

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
        mig.post_live_migration_at_destination(network_info)
        del self.live_migrations[instance.uuid]

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
        port = pvm_vterm.open_localhost_vnc_vterm(self.adapter, lpar_uuid)
        host = CONF.vnc.vncserver_proxyclient_address
        return console_type.ConsoleVNC(host=host, port=port)

    def _get_inst_vol_adpt(self, context, instance, conn_info=None,
                           tx_mgr=None):
        """Returns the appropriate volume driver based on connection type.

        Checks the connection info for connection-type and return the
        connector, if no connection info is provided returns the default
        connector.
        :param context: security context
        :param instance: Nova instance for which the volume adapter is needed.
        :param conn_info: BDM connection information of the instance to
                          get the volume adapter type (vSCSI/NPIV) requested.
        :param tx_mgr: (Optional) The FeedTask that can be used to defer the
                       mapping actions against the Virtual I/O Server for.  If
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
                  {'cls': vol_cls, 'inst': instance})
        return vol_cls(self.adapter, self.host_uuid,
                       instance, conn_info, tx_mgr=tx_mgr)


def _inst_dict(input_dict):
    """Builds a dictionary with instances as values based on the input classes.

    :param input_dict: A dictionary with keys, whose values are class
                       names.
    :return: A dictionary with the same keys.  But the values are instances
             of the class.  No parameters are passed in  to the init methods.
    """
    response = dict()

    for key in input_dict.keys():
        class_inst = importutils.import_class(input_dict[key])
        response[key] = class_inst()

    return response
