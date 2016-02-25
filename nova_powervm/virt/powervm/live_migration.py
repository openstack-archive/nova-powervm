# Copyright 2015, 2016 IBM Corp.
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
from nova import exception
from oslo_log import log as logging
from oslo_serialization import jsonutils
from pypowervm.tasks import management_console as mgmt_task
from pypowervm.tasks import migration as mig
from pypowervm.tasks import storage as stor_task
from pypowervm.tasks import vterm
import six

from nova_powervm import conf as cfg
from nova_powervm.objects import migrate_data as mig_obj
from nova_powervm.virt.powervm.i18n import _
from nova_powervm.virt.powervm.i18n import _LE
from nova_powervm.virt.powervm.i18n import _LI
from nova_powervm.virt.powervm import media
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

        LOG.debug('src_compute_info: %s' % src_compute_info)
        LOG.debug('dst_compute_info: %s' % dst_compute_info)
        LOG.debug('Migration data: %s' % self.mig_data)

        return self.mig_data

    def pre_live_migration(self, context, block_device_info, network_info,
                           disk_info, migrate_data, vol_drvs):

        """Prepare an instance for live migration

        :param context: security context
        :param block_device_info: instance block device information
        :param network_info: instance network information
        :param disk_info: instance disk information
        :param migrate_data: a PowerVMLiveMigrateData object
        :param vol_drvs: volume drivers for the attached volumes
        """
        LOG.debug('Running pre live migration on destination.',
                  instance=self.instance)
        LOG.debug('Migration data: %s' % migrate_data)

        # Set the ssh auth key.
        mgmt_task.add_authorized_key(self.drvr.adapter,
                                     migrate_data.public_key)

        # For each volume, make sure it's ready to migrate
        for vol_drv in vol_drvs:
            LOG.info(_LI('Performing pre migration for volume %(volume)s'),
                     dict(volume=vol_drv.volume_id))
            try:
                vol_drv.pre_live_migration_on_destination(
                    migrate_data.vol_data)
            except Exception as e:
                LOG.exception(e)
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

    def post_live_migration_at_destination(self, network_info, vol_drvs):
        """Do post migration cleanup on destination host.

        :param network_info: instance network information
        :param vol_drvs: volume drivers for the attached volumes
        """
        # The LPAR should be on this host now.
        LOG.debug("Post live migration at destination.",
                  instance=self.instance)

        # An unbounded dictionary that each volume adapter can use to persist
        # data from one call to the next.
        mig_vol_stor = {}

        # For each volume, make sure it completes the migration
        for vol_drv in vol_drvs:
            LOG.info(_LI('Performing post migration for volume %(volume)s'),
                     dict(volume=vol_drv.volume_id))
            try:
                vol_drv.post_live_migration_at_destination(mig_vol_stor)
            except Exception as e:
                LOG.exception(e)
                # It failed.
                raise LiveMigrationVolume(
                    host=self.drvr.host_wrapper.system_name,
                    name=self.instance.name, volume=vol_drv.volume_id)

    def cleanup_volume(self, vol_drv):
        """Cleanup a volume after a failed migration.

        :param vol_drv: volume driver for the attached volume
        """
        LOG.info(_LI('Performing detach for volume %(volume)s'),
                 dict(volume=vol_drv.volume_id))
        try:
            vol_drv.cleanup_volume_at_destination(self.pre_live_vol_data)
        except Exception as e:
            LOG.exception(e)
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

        lpar_w = vm.get_instance_wrapper(
            self.drvr.adapter, self.instance, self.drvr.host_uuid)
        self.lpar_w = lpar_w

        LOG.debug('Dest Migration data: %s' % self.mig_data)

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

        LOG.debug('Src Migration data: %s' % self.mig_data)

        # Create a FeedTask to scrub any orphaned mappings/storage associated
        # with this LPAR.  (Don't run it yet - we want to do the VOpt removal
        # within the same FeedTask.)
        stg_ftsk = stor_task.ScrubOrphanStorageForLpar(self.drvr.adapter,
                                                       lpar_w.id)
        # Add subtasks to remove the VOpt devices under the same FeedTask.
        media.ConfigDrivePowerVM(self.drvr.adapter, self.drvr.host_uuid
                                 ).dlt_vopt(lpar_w.uuid, stg_ftsk=stg_ftsk,
                                            remove_mappings=False)
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
        LOG.debug("Starting migration.", instance=self.instance)
        LOG.debug("Migrate data: %s" % migrate_data)

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

        try:
            # Migrate the LPAR!
            mig.migrate_lpar(self.lpar_w, self.mig_data.dest_sys_name,
                             validate_only=False,
                             tgt_mgmt_svr=self.mig_data.dest_ip,
                             tgt_mgmt_usr=self.mig_data.dest_user_id,
                             virtual_fc_mappings=vfc_mappings,
                             virtual_scsi_mappings=vscsi_mappings)

        except Exception:
            LOG.error(_LE("Live migration failed."), instance=self.instance)
            raise
        finally:
            LOG.debug("Finished migration.", instance=self.instance)

    def post_live_migration(self, vol_drvs, migrate_data):
        """Post operation of live migration at source host.

        This method is focused on storage.

        :param vol_drvs: volume drivers for the attached volume
        :param migrate_data: a PowerVMLiveMigrateData object
        """
        # For each volume, make sure the source is cleaned
        for vol_drv in vol_drvs:
            LOG.info(_LI('Performing post migration for volume %(volume)s'),
                     dict(volume=vol_drv.volume_id))
            try:
                vol_drv.post_live_migration_at_source(migrate_data.vol_data)
            except Exception as e:
                LOG.exception(e)
                # Log the exception but no need to raise one because
                # the VM is already moved.  By raising an exception that
                # results in the VM being on the new host but the instance
                # data reflecting it on the old host.

    def post_live_migration_at_source(self, network_info):
        """Do post migration cleanup on source host.

        This method is network focused.

        :param network_info: instance network information
        """
        LOG.debug("Post live migration at source.", instance=self.instance)

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

        except Exception as ex:
            LOG.error(_LE("Migration recover failed with error: %s"), ex,
                      instance=self.instance)
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
        except Exception as ex:
            LOG.error(_LE("Abort of live migration has failed. "
                          "This is non-blocking. "
                          "Exception is logged below."))
            LOG.exception(ex)

    def migration_recover(self):
        """Recover migration if the migration failed for any reason. """
        LOG.debug("Recover migration.", instance=self.instance)
        mig.migrate_recover(self.lpar_w, force=True)
