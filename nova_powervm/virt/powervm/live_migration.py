# Copyright 2015, 2017 IBM Corp.
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
#

import abc
import six

from nova import exception
from nova.objects import migrate_data as mig_obj
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import excutils
from pypowervm.tasks import management_console as mgmt_task
from pypowervm.tasks import migration as mig
from pypowervm.tasks import storage as stor_task
from pypowervm.tasks import vterm
from pypowervm import util

from nova_powervm import conf as cfg
from nova_powervm.virt.powervm.i18n import _
from nova_powervm.virt.powervm import media
from nova_powervm.virt.powervm.tasks import storage as tf_stg
from nova_powervm.virt.powervm import vif
from nova_powervm.virt.powervm import vm


LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class LiveMigrationFailed(exception.NovaException):
    msg_fmt = _("Live migration of instance '%(name)s' failed for reason: "
                "%(reason)s")


class LiveMigrationVolume(exception.NovaException):
    msg_fmt = _("Cannot migrate %(name)s because the volume %(volume)s "
                "cannot be attached on the destination host %(host)s.")


def _verify_migration_capacity(host_w, instance):
    """Check that the counts are valid for in progress and supported."""
    mig_stats = host_w.migration_data
    if (mig_stats['active_migrations_in_progress'] >=
            mig_stats['active_migrations_supported']):

        msg = (_("Cannot migrate %(name)s because the host %(host)s only "
                 "allows %(allowed)s concurrent migrations and "
                 "%(running)s migrations are currently running.") %
               dict(name=instance.name, host=host_w.system_name,
                    running=mig_stats['active_migrations_in_progress'],
                    allowed=mig_stats['active_migrations_supported']))
        raise exception.MigrationPreCheckError(reason=msg)


@six.add_metaclass(abc.ABCMeta)
class LiveMigration(object):

    def __init__(self, drvr, instance, mig_data):
        self.drvr = drvr
        self.instance = instance
        self.mig_data = mig_data


class LiveMigrationDest(LiveMigration):

    def __init__(self, drvr, instance):
        super(LiveMigrationDest, self).__init__(
            drvr, instance, mig_obj.PowerVMLiveMigrateData())

    @staticmethod
    def _get_dest_user_id():
        """Get the user id to use on the target host."""
        # We'll always use wlp
        return 'wlp'

    def check_destination(self, context, src_compute_info, dst_compute_info):
        """Check the destination host

        Here we check the destination host to see if it's capable of migrating
        the instance to this host.

        :param context: security context
        :param src_compute_info: Info about the sending machine
        :param dst_compute_info: Info about the receiving machine
        :returns: a PowerVMLiveMigrateData object
        """

        # Refresh the host wrapper since we're pulling values that may change
        self.drvr.host_wrapper.refresh()

        src_stats = src_compute_info['stats']
        dst_stats = dst_compute_info['stats']
        # Check the lmb sizes for compatibility
        if (src_stats['memory_region_size'] !=
                dst_stats['memory_region_size']):
            msg = (_("Cannot migrate instance '%(name)s' because the "
                     "memory region size of the source (%(source_mrs)d MB) "
                     "does not match the memory region size of the target "
                     "(%(target_mrs)d MB).") %
                   dict(name=self.instance.name,
                        source_mrs=src_stats['memory_region_size'],
                        target_mrs=dst_stats['memory_region_size']))

            raise exception.MigrationPreCheckError(reason=msg)

        _verify_migration_capacity(self.drvr.host_wrapper, self.instance)

        self.mig_data.host_mig_data = self.drvr.host_wrapper.migration_data
        self.mig_data.dest_ip = CONF.my_ip
        self.mig_data.dest_user_id = self._get_dest_user_id()
        self.mig_data.dest_sys_name = self.drvr.host_wrapper.system_name
        self.mig_data.dest_proc_compat = (
            ','.join(self.drvr.host_wrapper.proc_compat_modes))

        LOG.debug('src_compute_info: %s', src_compute_info)
        LOG.debug('dst_compute_info: %s', dst_compute_info)
        LOG.debug('Migration data: %s', self.mig_data)

        return self.mig_data

    def pre_live_migration(self, context, block_device_info, network_infos,
                           disk_info, migrate_data, vol_drvs):

        """Prepare an instance for live migration

        :param context: security context
        :param block_device_info: instance block device information
        :param network_infos: instance network information
        :param disk_info: instance disk information
        :param migrate_data: a PowerVMLiveMigrateData object
        :param vol_drvs: volume drivers for the attached volumes
        """
        LOG.debug('Running pre live migration on destination. Migration data: '
                  '%s', migrate_data, instance=self.instance)

        # Set the ssh auth key.
        mgmt_task.add_authorized_key(self.drvr.adapter,
                                     migrate_data.public_key)

        # For each network info, run the pre-live migration.  This tells the
        # system what the target vlans will be.
        vea_vlan_mappings = {}
        for network_info in network_infos:
            vif.pre_live_migrate_at_destination(
                self.drvr.adapter, self.drvr.host_uuid, self.instance,
                network_info, vea_vlan_mappings)
        migrate_data.vea_vlan_mappings = vea_vlan_mappings

        # For each volume, make sure it's ready to migrate
        for vol_drv in vol_drvs:
            LOG.info('Performing pre migration for volume %(volume)s',
                     dict(volume=vol_drv.volume_id), instance=self.instance)
            try:
                vol_drv.pre_live_migration_on_destination(
                    migrate_data.vol_data)
            except Exception:
                LOG.exception("PowerVM error preparing instance for live "
                              "migration.", instance=self.instance)
                # It failed.
                vol_exc = LiveMigrationVolume(
                    host=self.drvr.host_wrapper.system_name,
                    name=self.instance.name, volume=vol_drv.volume_id)
                raise exception.MigrationPreCheckError(reason=vol_exc.message)

        # Scrub stale/orphan mappings and storage to minimize probability of
        # collisions on the destination.
        stor_task.ComprehensiveScrub(self.drvr.adapter).execute()

        # Save the migration data, we'll use it if the LPM fails
        self.pre_live_vol_data = migrate_data.vol_data
        return migrate_data

    def post_live_migration_at_destination(self, network_infos, vol_drv_iter):
        """Do post migration cleanup on destination host.

        :param network_infos: instance network information
        :param vol_drv_iter: volume driver iterator for the attached volumes
                             and BDM information.
        """
        # The LPAR should be on this host now.
        LOG.debug("Post live migration at destination.",
                  instance=self.instance)

        # For each volume, make sure it completes the migration
        for bdm, vol_drv in vol_drv_iter:
            LOG.info('Performing post migration for volume %(volume)s',
                     dict(volume=vol_drv.volume_id), instance=self.instance)
            try:
                vol_drv.post_live_migration_at_destination(
                    self.pre_live_vol_data)
                # Save the BDM for the updated connection info.
                tf_stg.SaveBDM(bdm, self.instance).execute()
            except Exception:
                LOG.exception("PowerVM error cleaning up destination host "
                              "after migration.", instance=self.instance)
                # It failed.
                raise LiveMigrationVolume(
                    host=self.drvr.host_wrapper.system_name,
                    name=self.instance.name, volume=vol_drv.volume_id)

    def rollback_live_migration_at_destination(
            self, context, instance, network_infos, block_device_info,
            destroy_disks=True, migrate_data=None):
        """Clean up destination node after a failed live migration.

        :param context: security context
        :param instance: instance object that was being migrated
        :param network_infos: instance network infos
        :param block_device_info: instance block device information
        :param destroy_disks:
            if true, destroy disks at destination during cleanup
        :param migrate_data: a LiveMigrateData object

        """
        # Clean up any network infos
        for network_info in network_infos:
            vif.rollback_live_migration_at_destination(
                self.drvr.adapter, self.drvr.host_uuid, self.instance,
                network_info, migrate_data.vea_vlan_mappings)

    def cleanup_volume(self, vol_drv):
        """Cleanup a volume after a failed migration.

        :param vol_drv: volume driver for the attached volume
        """
        LOG.info('Performing detach for volume %(volume)s',
                 dict(volume=vol_drv.volume_id), instance=self.instance)
        # Ensure the volume data is present before trying cleanup
        if hasattr(self, 'pre_live_vol_data'):
            try:
                vol_drv.cleanup_volume_at_destination(self.pre_live_vol_data)
            except Exception:
                LOG.exception("PowerVM error cleaning volume after failed "
                              "migration.", instance=self.instance)
                # Log the exception but no need to raise one because
                # the VM is still on the source host.


class LiveMigrationSrc(LiveMigration):

    def check_source(self, context, block_device_info, vol_drvs):
        """Check the source host

        Here we check the source host to see if it's capable of migrating
        the instance to the destination host.  There may be conditions
        that can only be checked on the source side.

        Also, get the instance ready for the migration by removing any
        virtual optical devices attached to the LPAR.

        :param context: security context
        :param block_device_info: result of _get_instance_block_device_info
        :param vol_drvs: volume drivers for the attached volumes
        :returns: a PowerVMLiveMigrateData object
        """

        lpar_w = vm.get_instance_wrapper(self.drvr.adapter, self.instance)
        self.lpar_w = lpar_w

        LOG.debug('Dest Migration data: %s', self.mig_data,
                  instance=self.instance)

        # Check proc compatibility modes
        if (lpar_w.proc_compat_mode and lpar_w.proc_compat_mode not in
                self.mig_data.dest_proc_compat.split(',')):
            msg = (_("Cannot migrate %(name)s because its "
                     "processor compatibility mode %(mode)s "
                     "is not in the list of modes \"%(modes)s\" "
                     "supported by the target host.") %
                   dict(name=self.instance.name,
                        mode=lpar_w.proc_compat_mode,
                        modes=', '.join(
                            self.mig_data.dest_proc_compat.split(','))))

            raise exception.MigrationPreCheckError(reason=msg)

        # Check if VM is ready for migration
        self._check_migration_ready(lpar_w, self.drvr.host_wrapper)

        if lpar_w.migration_state != 'Not_Migrating':
            msg = (_("Live migration of instance '%(name)s' failed because "
                     "the migration state is: %(state)s") %
                   dict(name=self.instance.name,
                        state=lpar_w.migration_state))
            raise exception.MigrationPreCheckError(reason=msg)

        # Check the number of migrations for capacity
        _verify_migration_capacity(self.drvr.host_wrapper, self.instance)

        self.mig_data.public_key = mgmt_task.get_public_key(self.drvr.adapter)

        # Get the 'source' pre-migration data for the volume drivers.
        vol_data = {}
        for vol_drv in vol_drvs:
            vol_drv.pre_live_migration_on_source(vol_data)
        self.mig_data.vol_data = vol_data

        LOG.debug('Source migration data: %s', self.mig_data,
                  instance=self.instance)

        # Create a FeedTask to scrub any orphaned mappings/storage associated
        # with this LPAR.  (Don't run it yet - we want to do the VOpt removal
        # within the same FeedTask.)
        stg_ftsk = stor_task.ScrubOrphanStorageForLpar(self.drvr.adapter,
                                                       lpar_w.id)
        # Add subtasks to remove the VOpt devices under the same FeedTask.
        media.ConfigDrivePowerVM(self.drvr.adapter).dlt_vopt(
            lpar_w.uuid, stg_ftsk=stg_ftsk, remove_mappings=False)
        # Now execute the FeedTask, performing both scrub and VOpt removal.
        stg_ftsk.execute()

        # Ensure the vterm is non-active
        vterm.close_vterm(self.drvr.adapter, lpar_w.uuid)

        return self.mig_data

    def live_migration(self, context, migrate_data):
        """Start the live migration.

        :param context: security context
        :param migrate_data: a PowerVMLiveMigrateData object
        """
        LOG.debug("Starting migration. Migrate data: %s", migrate_data,
                  instance=self.instance)

        # The passed in mig data has more info (dest data added), so replace
        self.mig_data = migrate_data

        # Get the vFC and vSCSI live migration mappings
        vol_data = migrate_data.vol_data
        vfc_mappings = vol_data.get('vfc_lpm_mappings')
        if vfc_mappings is not None:
            vfc_mappings = jsonutils.loads(vfc_mappings)
        vscsi_mappings = vol_data.get('vscsi_lpm_mappings')
        if vscsi_mappings is not None:
            vscsi_mappings = jsonutils.loads(vscsi_mappings)

        # Run the pre-live migration on the network objects
        network_infos = self.instance.info_cache.network_info
        trunks_to_del = []
        for network_info in network_infos:
            trunks_to_del.extend(vif.pre_live_migrate_at_source(
                self.drvr.adapter, self.drvr.host_uuid, self.instance,
                network_info))

        # Convert the network mappings into something the API can understand.
        vlan_mappings = self._convert_nl_io_mappings(
            migrate_data.vea_vlan_mappings)

        try:
            # Migrate the LPAR!
            mig.migrate_lpar(
                self.lpar_w, self.mig_data.dest_sys_name,
                validate_only=False, tgt_mgmt_svr=self.mig_data.dest_ip,
                tgt_mgmt_usr=self.mig_data.dest_user_id,
                virtual_fc_mappings=vfc_mappings,
                virtual_scsi_mappings=vscsi_mappings,
                vlan_mappings=vlan_mappings, sdn_override=True,
                vlan_check_override=True)

            # Delete the source side network trunk adapters
            for trunk_to_del in trunks_to_del:
                trunk_to_del.delete()
        except Exception:
            with excutils.save_and_reraise_exception(logger=LOG):
                LOG.exception("Live migration failed.", instance=self.instance)
        finally:
            LOG.debug("Finished migration.", instance=self.instance)

    def _convert_nl_io_mappings(self, mappings):
        if not mappings:
            return None

        resp = []
        for mac, value in six.iteritems(mappings):
            resp.append("%s/%s" % (util.sanitize_mac_for_api(mac), value))
        return resp

    def post_live_migration(self, vol_drvs, migrate_data):
        """Post operation of live migration at source host.

        This method is focused on storage.

        :param vol_drvs: volume drivers for the attached volume
        :param migrate_data: a PowerVMLiveMigrateData object
        """
        # For each volume, make sure the source is cleaned
        for vol_drv in vol_drvs:
            LOG.info('Performing post migration for volume %(volume)s',
                     dict(volume=vol_drv.volume_id), instance=self.instance)
            try:
                vol_drv.post_live_migration_at_source(migrate_data.vol_data)
            except Exception:
                LOG.exception("PowerVM error cleaning source host after live "
                              "migration.", instance=self.instance)
                # Log the exception but no need to raise one because
                # the VM is already moved.  By raising an exception that
                # results in the VM being on the new host but the instance
                # data reflecting it on the old host.

    def post_live_migration_at_source(self, network_infos):
        """Do post migration cleanup on source host.

        This method is network focused.

        :param network_infos: instance network information
        """
        LOG.debug("Post live migration at source.", instance=self.instance)
        for network_info in network_infos:
            vif.post_live_migrate_at_source(
                self.drvr.adapter, self.drvr.host_uuid, self.instance,
                network_info)

    def rollback_live_migration(self, context):
        """Roll back a failed migration.

        :param context: security context
        """
        LOG.debug("Rollback live migration.", instance=self.instance)
        # If an error happened then let's try to recover
        # In most cases the recovery will happen automatically, but if it
        # doesn't, then force it.
        try:
            self.lpar_w.refresh()
            if self.lpar_w.migration_state != 'Not_Migrating':
                self.migration_recover()

        except Exception:
            LOG.exception("Migration rollback failed.", instance=self.instance)
        finally:
            LOG.debug("Finished migration rollback.", instance=self.instance)

    def _check_migration_ready(self, lpar_w, host_w):
        """See if the lpar is ready for LPM.

        :param lpar_w: LogicalPartition wrapper
        :param host_w: ManagedSystem wrapper
        """
        ready, msg = lpar_w.can_lpm(host_w,
                                    migr_data=self.mig_data.host_mig_data)
        if not ready:
            msg = (_("Live migration of instance '%(name)s' failed because it "
                     "is not ready. Reason: %(reason)s") %
                   dict(name=self.instance.name, reason=msg))
            raise exception.MigrationPreCheckError(reason=msg)

    def migration_abort(self):
        """Abort the migration.

        Invoked if the operation exceeds the configured timeout.
        """
        LOG.debug("Abort migration.", instance=self.instance)
        try:
            mig.migrate_abort(self.lpar_w)
        except Exception:
            LOG.exception("Abort of live migration has failed. This is "
                          "non-blocking.", instance=self.instance)

    def migration_recover(self):
        """Recover migration if the migration failed for any reason. """
        LOG.debug("Recover migration.", instance=self.instance)
        mig.migrate_recover(self.lpar_w, force=True)
