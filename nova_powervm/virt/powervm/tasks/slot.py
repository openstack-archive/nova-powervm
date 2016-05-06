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


from oslo_log import log as logging
from taskflow import task

from nova_powervm.virt.powervm.i18n import _LI

LOG = logging.getLogger(__name__)


class SaveSlotStore(task.Task):

    """Will run the save of the slot store.

    This is typically done after some action (such as add nic, deploy, add
    volume, etc...) has run and the slot map itself has been updated.  One of
    the last actions is to now save the slot map back to the storage system.
    """

    def __init__(self, instance, slot_mgr):
        """Create the task.

        :param instance: The nova instance.
        :param slot_mgr: A NovaSlotManager.  Contains the object that will be
                         saved.
        """
        self.instance = instance
        self.slot_mgr = slot_mgr
        self.orig = None

        super(SaveSlotStore, self).__init__(name='save_slot_store')

    def execute(self):
        LOG.info(_LI('Saving slot topology for instance %(inst)s'),
                 {'inst': self.instance.name}, instance=self.instance)
        LOG.debug("Topology for instance %(inst)s: %(topo)s",
                  {'inst': self.instance.name, 'topo': self.slot_mgr.topology})
        self.slot_mgr.save()


class DeleteSlotStore(task.Task):

    """Will run the delete of the slot store.

    This removes the slot store for an entire instance.  Typically run when the
    VM is destroyed.
    """

    def __init__(self, instance, slot_mgr):
        """Create the task.

        :param instance: The nova instance.
        :param slot_mgr: A NovaSlotManager.  Contains the object that will be
                         deleted.
        """
        self.instance = instance
        self.slot_mgr = slot_mgr

        super(DeleteSlotStore, self).__init__(name='delete_slot_store')

    def execute(self):
        LOG.info(_LI('Deleting slot store for instance %(inst)s'),
                 {'inst': self.instance.name}, instance=self.instance)
        self.slot_mgr.delete()
