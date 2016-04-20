# Copyright 2016 IBM Corp.
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
from pypowervm import const as pvm_const
from pypowervm import exceptions as pvm_exc
import six
import time

from nova_powervm.virt.powervm.i18n import _LE
from nova_powervm.virt.powervm.i18n import _LW
from nova_powervm.virt.powervm.nvram import api
from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)
LOCK_NVRAM_UPDT_LIST = 'nvram_update_list'
LOCK_NVRAM_STORE = 'nvram_update'


class NvramManager(object):
    """The manager of the NVRAM store and fetch process.

    This class uses two locks. One for controlling access to the list of
    instances to update the NVRAM for and another to control actually updating
    the NVRAM for the instance itself.

    An update to the instance store should always lock the update lock first
    and then get the list lock.  There should never be a case where the list
    lock is acquired before the update lock.  This can lead to deadlock cases.

    NVRAM events for an instance come in spurts primarily during power on and
    off, from what has been observed so far.  By using a dictionary and the
    instance.uuid as the key, rapid requests to store the NVRAM can be
    collapsed down into a single request (optimal).
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

        self._update_list = {}
        self._queue = eventlet.queue.LightQueue()
        self._shutdown = False
        self._update_thread = n_utils.spawn(self._update_thread)
        LOG.debug('NVRAM store manager started.')

    def shutdown(self):
        """Shutdown the NVRAM Manager."""

        LOG.debug('NVRAM store manager shutting down.')
        self._shutdown = True
        # Remove all pending updates
        self._clear_list()
        # Signal the thread to stop
        self._queue.put(None)
        self._update_thread.wait()

    def store(self, instance, immediate=False):
        """Store the NVRAM for an instance.

        :param instance: The instance to store the NVRAM for.
        :param immediate: Force the update to take place immediately.
                          Otherwise, the request is queued for asynchronous
                          update.
        """
        if immediate:
            self._update_nvram(instance=instance)
        else:
            # Add it to the list to update
            self._add_to_list(instance)
            # Trigger the thread
            self._queue.put(instance.uuid, block=False)
            # Sleep so the thread gets a chance to run
            time.sleep(0)

    def fetch(self, instance):
        """Fetch the NVRAM for an instance.

        :param instance: The instance to fetch the NVRAM for.
        :returns: The NVRAM data for the instance.
        """
        try:
            return self._api.fetch(instance)
        except Exception as e:
            LOG.exception(_LE('Could not update NVRAM: %s'), e,
                          instance=instance)
            raise api.NVRAMDownloadException(instance=instance.name,
                                             reason=six.text_type(e))

    @lockutils.synchronized(LOCK_NVRAM_STORE)
    def remove(self, instance):
        """Remove the stored NVRAM for an instance.

        :param instance: The instance for which the NVRAM will be removed.
        """
        # Remove any pending updates
        self._pop_from_list(uuid=instance.uuid)
        # Remove it from the store
        try:
            self._api.delete(instance)
        except Exception as e:
            # Delete exceptions should not end the operation
            LOG.warning(_LW('Could not delete NVRAM: %s'), e,
                        instance=instance)

    @lockutils.synchronized(LOCK_NVRAM_UPDT_LIST)
    def _add_to_list(self, instance):
        """Add an instance to the list of instances to store the NVRAM."""
        self._update_list[instance.uuid] = instance

    @lockutils.synchronized(LOCK_NVRAM_UPDT_LIST)
    def _pop_from_list(self, uuid=None):
        """Pop an instance off the list of instance to update.

        :param uuid: The uuid of the instance to update or if not specified
                     pull the next instance off the list.
        returns: The uuid and instance.
        """
        try:
            if uuid is None:
                return self._update_list.popitem()
            else:
                return self._update_list.pop(uuid)
        except KeyError:
            return None, None

    @lockutils.synchronized(LOCK_NVRAM_UPDT_LIST)
    def _clear_list(self):
        """Clear the list of instance to store NVRAM for."""
        self._update_list.clear()

    @lockutils.synchronized(LOCK_NVRAM_STORE)
    def _update_nvram(self, instance=None):
        """Perform an update of NVRAM for instance.

        :param instance: The instance to update or if not specified pull the
                         next one off the list to update.
        """
        if instance is None:
            uuid, instance = self._pop_from_list()
            if uuid is None:
                return
        else:
            # Remove any pending updates
            self._pop_from_list(uuid=instance.uuid)

        try:
            LOG.debug('Updating NVRAM for instance: %s', instance.uuid)
            data = self._get_data(instance)
            if data is not None:
                self._api.store(instance, data)
        except Exception as e:
            # Update exceptions should not end the operation.
            LOG.exception(_LE('Could not update NVRAM: %s'), e,
                          instance=instance)

    def _get_data(self, instance):
        """Get the NVRAM data for the instance.

        :param inst: The instance to get the data for.
        :returns: The NVRAM data for the instance.
        """
        data = None
        try:
            # Get the data from the adapter.
            entry = vm.get_instance_wrapper(self._adapter, instance,
                                            self._host_uuid,
                                            xag=[pvm_const.XAG.NVRAM])
            data = entry.nvram
            LOG.debug('NVRAM for instance: %s', data, instance=instance)
        except pvm_exc.HttpError as e:
            # The VM might have been deleted since the store request.
            if e.response.status not in ['404']:
                LOG.exception(e)
                LOG.warning(_LW('Unable to store the NVRAM for instance: '
                                '%s'), instance.name)
        return data

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
