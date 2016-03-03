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

import abc
from nova import exception as nex
import six

from nova_powervm.virt.powervm.i18n import _


class NVRAMUploadException(nex.NovaException):
    msg_fmt = _("The NVRAM could not be stored for instance %(instance)s. "
                "Reason: %(reason)s")


class NVRAMDownloadException(nex.NovaException):
    msg_fmt = _("The NVRAM could not be fetched for instance %(instance)s. "
                "Reason: %(reason)s")


class NVRAMDeleteException(nex.NovaException):
    msg_fmt = _("The NVRAM could not be deleted for instance %(instance)s. "
                "Reason: %(reason)s")


class NVRAMConfigOptionNotSet(nex.NovaException):
    msg_fmt = _("The configuration option '%(option)s' must be set.")


@six.add_metaclass(abc.ABCMeta)
class NvramStore(object):

    @abc.abstractmethod
    def store(self, instance, data, force=True):
        """Store the NVRAM into the storage service.

        :param instance: instance object
        :param data: the NVRAM data base64 encoded string
        :param force: boolean whether an update should always be saved,
                      otherwise, check to see if it's changed.
        """

    @abc.abstractmethod
    def fetch(self, instance):
        """Fetch the NVRAM from the storage service.

        :param instance: instance object
        :returns: the NVRAM data base64 encoded string
        """

    @abc.abstractmethod
    def delete(self, instance):
        """Delete the NVRAM from the storage service.

        :param instance: instance object
        """
