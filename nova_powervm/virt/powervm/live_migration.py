# Copyright 2015 IBM Corp.
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
from nova.i18n import _, _LE, _LI
from pypowervm.tasks import management_console as mgmt_task
from pypowervm.tasks import migration as mig
from pypowervm.tasks import vterm

from oslo_config import cfg
from oslo_log import log as logging
import six

from nova_powervm.virt.powervm import media
from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class LiveMigrationFailed(exception.NovaException):
    msg_fmt = _("Live migration of instance '%(name)s' failed for reason: "
                "%(reason)s")


class LiveMigrationInvalidState(exception.NovaException):
    msg_fmt = _("Live migration of instance '%(name)s' failed because the "
                "migration state is: %(state)s")


class LiveMigrationNotReady(exception.NovaException):
    msg_fmt = _("Live migration of instance '%(name)s' failed because it is "
                "not ready. Reason: %(reason)s")


class LiveMigrationMRS(exception.NovaException):
    msg_fmt = _("Cannot migrate instance '%(name)s' because the memory region "
                "size of the source (%(source_mrs)d MB) does not "
                "match the memory region size of the target "
                "(%(target_mrs)d MB).")


class LiveMigrationProcCompat(exception.NovaException):
    msg_fmt = _("Cannot migrate %(name)s because its "
                "processor compatibility mode %(mode)s "
                "is not in the list of modes \"%(modes)s\" "
                "supported by the target host.")


class LiveMigrationCapacity(exception.NovaException):
    msg_fmt = _("Cannot migrate %(name)s because the host %(host)s only "
                "allows %(allowed)s concurrent migrations and %(running)s "
                "migrations are currently running.")


class LiveMigrationVolume(exception.NovaException):
    msg_fmt = _("Cannot migrate %(name)s because the volume %(volume)s "
                "cannot be attached on the destination host %(host)s.")


def _verify_migration_capacity(host_w, instance):
    """Check that the counts are valid for in progress and supported."""
    mig_stats = host_w.migration_data
    if (mig_stats['active_migrations_in_progress'] >=
            mig_stats['active_migrations_supported']):

        raise LiveMigrationCapacity(
            name=instance.name, host=host_w.system_name,
            running=mig_stats['active_migrations_in_progress'],
            allowed=mig_stats['active_migrations_supported'])


@six.add_metaclass(abc.ABCMeta)
class LiveMigration(object):

    def __init__(self, drvr, instance, src_data, dest_data):
        self.drvr = drvr
        self.instance = instance
        self.src_data = src_data  # migration data from src host
        self.dest_data = dest_data  # migration data from dest host


class LiveMigrationDest(LiveMigration):

    def __init__(self, drvr, instance):
        super(LiveMigrationDest, self).__init__(drvr, instance, {}, {})

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
        :returns: a dict containing migration info
        """

        # Refresh the host wrapper since we're pulling values that may change
        self.drvr.host_wrapper.refresh()

        src_stats = src_compute_info['stats']
        dst_stats = dst_compute_info['stats']
        # Check the lmb sizes for compatability
        if (src_stats['memory_region_size'] !=
                dst_stats['memory_region_size']):
            raise LiveMigrationMRS(
                name=self.instance.name,
                source_mrs=src_stats['memory_region_size'],
                target_mrs=dst_stats['memory_region_size'])

        _verify_migration_capacity(self.drvr.host_wrapper, self.instance)

        self.dest_data['dest_ip'] = CONF.my_ip
        self.dest_data['dest_user_id'] = self._get_dest_user_id()
        self.dest_data['dest_sys_name'] = self.drvr.host_wrapper.system_name
        self.dest_data['dest_proc_compat'] = (
            ','.join(self.drvr.host_wrapper.proc_compat_modes))

        LOG.debug('src_compute_info: %s' % src_compute_info)
        LOG.debug('dst_compute_info: %s' % dst_compute_info)
        LOG.debug('Migration data: %s' % self.dest_data)

        return self.dest_data

    def pre_live_migration(self, context, block_device_info, network_info,
                           disk_info, migrate_data, vol_drvs):

        """Prepare an instance for live migration

        :param context: security context
        :param instance: nova.objects.instance.Instance object
        :param block_device_info: instance block device information
        :param network_info: instance network information
        :param disk_info: instance disk information
        :param migrate_data: implementation specific data dict
        :param vol_drvs: volume drivers for the attached volumes
        """
        LOG.debug('Running pre live migration on destination.',
                  instance=self.instance)
        LOG.debug('Migration data: %s' % migrate_data)

        # Set the ssh auth key if needed.
        src_mig_data = migrate_data.get('migrate_data', {})
        pub_key = src_mig_data.get('public_key')
        if pub_key is not None:
            mgmt_task.add_authorized_key(self.drvr.adapter, pub_key)

        # For each volume, make sure it's ready to migrate
        for vol_drv in vol_drvs:
            LOG.info(_LI('Performing pre migration for volume %(volume)s'),
                     dict(volume=vol_drv.volume_id))
            try:
                vol_drv.pre_live_migration_on_destination()
            except Exception as e:
                LOG.exception(e)
                # It failed.
                raise LiveMigrationVolume(
                    host=self.drvr.host_wrapper.system_name,
                    name=self.instance.name, volume=vol_drv.volume_id)

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

        # For each volume, make sure it's ready to migrate
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


class LiveMigrationSrc(LiveMigration):

    def __init__(self, drvr, instance, dest_data):
        super(LiveMigrationSrc, self).__init__(drvr, instance, {}, dest_data)

    def check_source(self, context, block_device_info):
        """Check the source host

        Here we check the source host to see if it's capable of migrating
        the instance to the destination host.  There may be conditions
        that can only be checked on the source side.

        Also, get the instance ready for the migration by removing any
        virtual optical devices attached to the LPAR.

        :param context: security context
        :param block_device_info: result of _get_instance_block_device_info
        :returns: a dict containing migration info
        """

        lpar_w = vm.get_instance_wrapper(
            self.drvr.adapter, self.instance, self.drvr.host_uuid)
        self.lpar_w = lpar_w

        LOG.debug('Dest Migration data: %s' % self.dest_data)

        # Only 'migrate_data' is sent to the destination on prelive call.
        mig_data = {'public_key': mgmt_task.get_public_key(self.drvr.adapter)}
        self.src_data['migrate_data'] = mig_data
        LOG.debug('Src Migration data: %s' % self.src_data)

        # Check proc compatability modes
        if (lpar_w.proc_compat_mode and lpar_w.proc_compat_mode not in
                self.dest_data['dest_proc_compat'].split(',')):
            raise LiveMigrationProcCompat(
                name=self.instance.name, mode=lpar_w.proc_compat_mode,
                modes=', '.join(self.dest_data['dest_proc_compat'].split(',')))

        # Check if VM is ready for migration
        self._check_migration_ready(lpar_w, self.drvr.host_wrapper)

        if lpar_w.migration_state != 'Not_Migrating':
            raise LiveMigrationInvalidState(name=self.instance.name,
                                            state=lpar_w.migration_state)

        # Check the number of migrations for capacity
        _verify_migration_capacity(self.drvr.host_wrapper, self.instance)

        # Remove the VOpt devices
        LOG.debug('Removing VOpt.', instance=self.instance)
        media.ConfigDrivePowerVM(self.drvr.adapter, self.drvr.host_uuid
                                 ).dlt_vopt(lpar_w.uuid)
        LOG.debug('Removing VOpt finished.', instance=self.instance)

        # Ensure the vterm is non-active
        vterm.close_vterm(self.drvr.adapter, lpar_w.uuid)

        return self.src_data

    def live_migration(self, context, migrate_data):
        """Start the live migration.

        :param context: security context
        :param migrate_data: migration data from src and dest host.
        """
        LOG.debug("Starting migration.", instance=self.instance)
        LOG.debug("Migrate data: %s" % migrate_data)
        try:
            # Migrate the LPAR!
            mig.migrate_lpar(self.lpar_w, self.dest_data['dest_sys_name'],
                             validate_only=False,
                             tgt_mgmt_svr=self.dest_data['dest_ip'],
                             tgt_mgmt_usr=self.dest_data.get('dest_user_id'))

        except Exception:
            LOG.error(_LE("Live migration failed."), instance=self.instance)
            raise
        finally:
            LOG.debug("Finished migration.", instance=self.instance)

    def post_live_migration_at_source(self, network_info):
        """Do post migration cleanup on source host.

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
        ready, msg = lpar_w.can_lpm(host_w)
        if not ready:
            raise LiveMigrationNotReady(name=self.instance.name, reason=msg)

    def migration_abort(self):
        """Abort the migration if the operation exceeds the configured timeout.
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
