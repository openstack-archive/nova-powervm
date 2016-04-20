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

from nova_powervm.virt.powervm.nvram import api


class NoopNvramStore(api.NvramStore):

    def store(self, instance, data, force=True):
        """Store the NVRAM into the storage service.

        :param instance: instance object
        :param data: the NVRAM data base64 encoded string
        :param force: boolean whether an update should always be saved,
                      otherwise, check to see if it's changed.
        """
        pass

    def fetch(self, instance):
        """Fetch the NVRAM from the storage service.

        :param instance: instance object
        :returns: the NVRAM data base64 encoded string
        """
        return None

    def delete(self, instance):
        """Delete the NVRAM from the storage service.

        :param instance: instance object
        """
        pass


class ExpNvramStore(NoopNvramStore):

    def fetch(self, instance):
        """Fetch the NVRAM from the storage service.

        :param instance: instance object
        :returns: the NVRAM data base64 encoded string
        """
        # Raise exception. This is to ensure fetch causes a failure
        # when an exception is raised
        raise Exception('Error')

    def delete(self, instance):
        """Delete the NVRAM from the storage service.

        :param instance: instance object
        """
        # Raise excpetion. This is to ensure delete does not fail
        # despite an exception being raised
        raise Exception('Error')
