# Copyright 2014 IBM Corp.
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


from nova.openstack.common import log as logging
from nova.virt import driver
from nova.virt import fake  # TODO(IBM): Remove this in the future

LOG = logging.getLogger(__name__)


class PowerVMDriver(driver.ComputeDriver):

    """PowerVM Implementation of Compute Driver."""

    def __init__(self, virtapi):
        super(PowerVMDriver, self).__init__(virtapi)

        # Use the fake driver for scaffolding for now
        fake.set_nodes(['fake-PowerVM'])
        self._fake = fake.FakeDriver(virtapi)

    def init_host(self, host):
        """Initialize anything that is necessary for the driver to function,
        including catching up with currently running VM's on the given host.
        """
        pass

    def get_info(self, instance):
        """Get the current status of an instance, by name (not ID!)

        Returns a dict containing:

        :state:           the running state, one of the power_state codes
        :max_mem:         (int) the maximum memory in KBytes allowed
        :mem:             (int) the memory in KBytes used by the domain
        :num_cpu:         (int) the number of virtual CPUs for the domain
        :cpu_time:        (int) the CPU time used in nanoseconds
        """

        info = self._fake.get_info(instance)

        return info

    def list_instances(self):
        """Return the names of all the instances known to the virtualization
        layer, as a list.
        """
        return self._fake.list_instances()

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None,
              instance_type=None):
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
        :param instance_type: The instance_type for the instance to be spawned.
        """
        return self._fake.spawn(context, instance, image_meta, injected_files,
                                admin_password, network_info,
                                block_device_info)

    def destroy(self, instance, network_info, block_device_info=None,
                destroy_disks=True):
        """Destroy (shutdown and delete) the specified instance.

        If the instance is not found (for example if networking failed), this
        function should still succeed.  It's probably a good idea to log a
        warning in that case.

        :param instance: Instance object as returned by DB layer.
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param block_device_info: Information about block devices that should
                                  be detached from the instance.
        :param destroy_disks: Indicates if disks should be destroyed

        """
        return self._fake.destroy(instance, network_info, block_device_info,
                                  destroy_disks)

    def attach_volume(self, connection_info, instance, mountpoint):
        """Attach the disk to the instance at mountpoint using info."""
        return self._fake.attach_volume(connection_info, instance, mountpoint)

    def detach_volume(self, connection_info, instance, mountpoint):
        """Detach the disk attached to the instance."""
        return self._fake.detach_volume(connection_info, instance, mountpoint)

    def snapshot(self, context, instance, image_id, update_task_state):
        """Snapshots the specified instance.

        :param context: security context
        :param instance: Instance object as returned by DB layer.
        :param image_id: Reference to a pre-created image that will
                         hold the snapshot.
        """
        raise self._fake.snapshot(context, instance, image_id,
                                  update_task_state)

    def power_off(self, instance):
        """Power off the specified instance."""
        raise NotImplementedError()

    def power_on(self, instance):
        """Power on the specified instance."""
        raise NotImplementedError()

    def get_available_resource(self, nodename):
        """Retrieve resource information.

        This method is called when nova-compute launches, and
        as part of a periodic task

        :param nodename:
            node which the caller want to get resources from
            a driver that manages only one node can safely ignore this
        :returns: Dictionary describing resources
        """

        data = self._fake.get_available_resource(nodename)
        return data

    def get_host_uptime(self, host):
        """Returns the result of calling "uptime" on the target host."""
        raise NotImplementedError()

    def plug_vifs(self, instance, network_info):
        """Plug VIFs into networks."""
        pass

    def unplug_vifs(self, instance, network_info):
        """Unplug VIFs from networks."""
        pass

    def get_host_stats(self, refresh=False):
        """Return currently known host stats."""

        data = self._fake.get_host_stats(refresh)
        return data

    def get_available_nodes(self):
        """Returns nodenames of all nodes managed by the compute service.

        This method is for multi compute-nodes support. If a driver supports
        multi compute-nodes, this method returns a list of nodenames managed
        by the service. Otherwise, this method should return
        [hypervisor_hostname].
        """

        return self._fake.get_available_nodes()

    def legacy_nwinfo(self):
        """Indicate if the driver requires the legacy network_info format.
        """
        return False

    def check_can_live_migrate_destination(self, ctxt, instance_ref,
                                           src_compute_info, dst_compute_info,
                                           block_migration=False,
                                           disk_over_commit=False):
        """Validate the destination host is capable of live partition
        migration.

        :param ctxt: security context
        :param instance_ref: instance to be migrated
        :param src_compute_info: source host information
        :param dst_compute_info: destination host information
        :param block_migration: if true, prepare for block migration
        :param disk_over_commit: if true, allow disk over commit
        :returns dest_check_data: dictionary containing destination data
        """
        dest_check_data = \
            self._fake.check_can_live_migrate_destination(
                ctxt, instance_ref, src_compute_info, dst_compute_info,
                block_migration=False, disk_over_commit=False)
        return dest_check_data

    def check_can_live_migrate_source(self, ctxt, instance_ref,
                                      dest_check_data):
        """Validate the source host is capable of live partition
        migration.

        :param context: security context
        :param instance_ref: instance to be migrated
        :param dest_check_data: results from check_can_live_migrate_destination
        :returns migrate_data: dictionary containing source and
            destination data for migration
        """
        migrate_data = \
            self._fake.check_can_live_migrate_source(ctxt,
                                                     instance_ref,
                                                     dest_check_data)
        return migrate_data

    def pre_live_migration(self, context, instance,
                           block_device_info, network_info,
                           migrate_data=None):
        """Perfoms any required prerequisites on the destination
        host prior to live partition migration.

        :param context: security context
        :param instance: instance to be migrated
        :param block_device_info: instance block device information
        :param network_info: instance network information
        :param migrate_data: implementation specific data dictionary
        """
        self._fake.pre_live_migration(context, instance,
                                      block_device_info,
                                      network_info,
                                      migrate_data)

    def live_migration(self, ctxt, instance_ref, dest,
                       post_method, recover_method,
                       block_migration=False, migrate_data=None):
        """Live migrates a partition from one host to another.

        :param ctxt: security context
        :params instance_ref: instance to be migrated.
        :params dest: destination host
        :params post_method: post operation method.
            nova.compute.manager.post_live_migration.
        :params recover_method: recovery method when any exception occurs.
            nova.compute.manager.recover_live_migration.
        :params block_migration: if true, migrate VM disk.
        :params migrate_data: implementation specific data dictionary.
        """
        self._fake.live_migration(ctxt, instance_ref, dest,
                                  post_method, recover_method,
                                  migrate_data, block_migration=False)

    def post_live_migration_at_destination(self, ctxt, instance_ref,
                                           network_info,
                                           block_migration=False,
                                           block_device_info=None):
        """Performs post operations on the destination host
        following a successful live migration.

        :param ctxt: security context
        :param instance_ref: migrated instance
        :param network_info: dictionary of network info for instance
        :param block_migration: boolean for block migration
        """
        self._fake.post_live_migration_at_destination(
            ctxt, instance_ref, network_info,
            block_migration=False, block_device_info=None)
