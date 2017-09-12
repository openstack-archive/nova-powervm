# Copyright 2016, 2017 IBM Corp.
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

import eventlet
from nova import utils as n_utils
from oslo_concurrency import lockutils
from oslo_log import log as logging
from oslo_utils import uuidutils
from pypowervm import const as pvm_const
from pypowervm import exceptions as pvm_exc
import six
import time

from nova_powervm.virt.powervm.nvram import api
from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)
LOCK_NVRAM_UPDT_SET = 'nvram_update_set'
LOCK_NVRAM_STORE = 'nvram_update'


class NvramManager(object):
    """The manager of the NVRAM store and fetch process.

    This class uses two locks. One for controlling access to the set of
    instance uuids to update the NVRAM for and another to control actually
    updating the NVRAM for the instance itself.

    An update to the instance uuid store should always lock the update lock
    first and then get the set lock. There should never be a case where the set
    lock is acquired before the update lock. This can lead to deadlock cases.

    NVRAM events for an instance come in spurts primarily during power on and
    off, from what has been observed so far. By using a set of instance uuids,
    rapid requests to store the NVRAM can be collapsed down into a single
    request (optimal).
    """

    def __init__(self, store_api, adapter, host_uuid):
        """Create the manager.

        :param store_api: the NvramStore api to use.
        :param adapter: pypowervm Adapter
        :param host_uuid:  powervm host uuid string
        """
        super(NvramManager, self).__init__()
        self._api = store_api
        self._adapter = adapter
        self._host_uuid = host_uuid

        self._update_set = set()
        self._queue = eventlet.queue.LightQueue()
        self._shutdown = False
        self._update_thread = n_utils.spawn(self._update_thread)
        LOG.debug('NVRAM store manager started.')

    def shutdown(self):
        """Shutdown the NVRAM Manager."""

        LOG.debug('NVRAM store manager shutting down.')
        self._shutdown = True
        # Remove all pending updates
        self._clear_set()
        # Signal the thread to stop
        self._queue.put(None)
        self._update_thread.wait()

    def store(self, instance, immediate=False):
        """Store the NVRAM for an instance.

        :param instance: The instance UUID OR instance object of the instance
                         to store the NVRAM for.
        :param immediate: Force the update to take place immediately.
                          Otherwise, the request is queued for asynchronous
                          update.
        """
        inst_uuid = (instance if
                     uuidutils.is_uuid_like(instance) else instance.uuid)
        if immediate:
            self._update_nvram(instance_uuid=inst_uuid)
        else:
            # Add it to the list to update
            self._add_to_set(inst_uuid)
            # Trigger the thread
            self._queue.put(inst_uuid, block=False)
            # Sleep so the thread gets a chance to run
            time.sleep(0)

    def fetch(self, instance):
        """Fetch the NVRAM for an instance.

        :param instance: The instance UUID OR instance object of the instance
                         to fetch the NVRAM for.
        :returns: The NVRAM data for the instance.
        """
        inst_uuid = (instance if
                     uuidutils.is_uuid_like(instance) else instance.uuid)
        try:
            return self._api.fetch(inst_uuid)
        except Exception as e:
            LOG.exception(('Could not fetch NVRAM for instance with UUID %s.'),
                          inst_uuid)
            raise api.NVRAMDownloadException(instance=inst_uuid,
                                             reason=six.text_type(e))

    @lockutils.synchronized(LOCK_NVRAM_STORE)
    def remove(self, instance):
        """Remove the stored NVRAM for an instance.

        :param instance: The nova instance object OR instance UUID.
        """
        inst_uuid = (instance if
                     uuidutils.is_uuid_like(instance) else instance.uuid)
        # Remove any pending updates
        self._pop_from_set(uuid=inst_uuid)
        # Remove it from the store
        try:
            self._api.delete(inst_uuid)
        except Exception:
            # Delete exceptions should not end the operation
            LOG.exception(('Could not delete NVRAM for instance with UUID '
                          '%s.'), inst_uuid)

    @lockutils.synchronized(LOCK_NVRAM_UPDT_SET)
    def _add_to_set(self, instance_uuid):
        """Add an instance uuid to the set of uuids to store the NVRAM."""
        self._update_set.add(instance_uuid)

    @lockutils.synchronized(LOCK_NVRAM_UPDT_SET)
    def _pop_from_set(self, uuid=None):
        """Pop an instance uuid off the set of instances to update.

        :param uuid: The uuid of the instance to update or if not specified
                     pull the next instance uuid off the set.
        :returns: The instance uuid.
        """
        try:
            if uuid is None:
                return self._update_set.pop()
            else:
                self._update_set.remove(uuid)
                return uuid
        except KeyError:
            return None

    @lockutils.synchronized(LOCK_NVRAM_UPDT_SET)
    def _clear_set(self):
        """Clear the set of instance uuids to store NVRAM for."""
        self._update_set.clear()

    @lockutils.synchronized(LOCK_NVRAM_STORE)
    def _update_nvram(self, instance_uuid=None):
        """Perform an update of NVRAM for instance.

        :param instance_uuid: The instance uuid of the instance to update or if
                              not specified pull the next one off the set to
                              update.
        """
        if instance_uuid is None:
            instance_uuid = self._pop_from_set()
            if instance_uuid is None:
                return
        else:
            # Remove any pending updates
            self._pop_from_set(uuid=instance_uuid)

        try:
            LOG.debug('Updating NVRAM for instance with uuid: %s',
                      instance_uuid)
            data = vm.get_instance_wrapper(
                self._adapter, instance_uuid, xag=[pvm_const.XAG.NVRAM]).nvram
            LOG.debug('NVRAM for instance with uuid %(uuid)s: %(data)s',
                      {'uuid': instance_uuid, 'data': data})
            if data is not None:
                self._api.store(instance_uuid, data)
        except pvm_exc.Error:
            # Update exceptions should not end the operation.
            LOG.exception('Could not update NVRAM for instance with uuid %s.',
                          instance_uuid)

    def _update_thread(self):
        """The thread that is charged with updating the NVRAM store."""

        LOG.debug('NVRAM store manager update thread started.')
        # Loop until it's time to shut down
        while not self._shutdown:
            if self._queue.get(block=True) is None:
                LOG.debug('NVRAM store manager update thread is ending.')
                return

            self._update_nvram()
            time.sleep(0)
