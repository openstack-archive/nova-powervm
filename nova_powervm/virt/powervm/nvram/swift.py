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

import copy
import hashlib
import os
import six
import tempfile
import types

from nova_powervm import conf as cfg
from nova_powervm.conf import powervm
from nova_powervm.virt.powervm.i18n import _LI
from nova_powervm.virt.powervm.i18n import _LW
from nova_powervm.virt.powervm.nvram import api

from oslo_concurrency import lockutils
from oslo_log import log as logging
from swiftclient import service as swft_srv

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class SwiftNvramStore(api.NvramStore):

    def __init__(self):
        super(SwiftNvramStore, self).__init__()
        self.container = CONF.powervm.swift_container
        # Build the swift service options
        self.options = self._init_swift()

    @staticmethod
    def _init_swift():
        """Initialize all the options needed to communicate with Swift."""

        for opt in powervm.swift_opts:
            if opt.required and getattr(CONF.powervm, opt.name) is None:
                raise api.NVRAMConfigOptionNotSet(option=opt.name)

        options = {
            'auth_version': CONF.powervm.swift_auth_version,
            'os_username': CONF.powervm.swift_username,
            'os_user_domain_name': CONF.powervm.swift_user_domain_name,
            'os_password': CONF.powervm.swift_password,
            'os_project_name': CONF.powervm.swift_project_name,
            'os_project_domain_name': CONF.powervm.swift_project_domain_name,
            'os_auth_url': CONF.powervm.swift_auth_url,
            'os_cacert': CONF.powervm.swift_cacert,
        }

        return options

    def _run_operation(self, service_options, f, *args, **kwargs):
        """Convenience method to call the Swift client service."""

        service_options = (self.options if service_options is None
                           else service_options)
        with swft_srv.SwiftService(options=service_options) as swift:
            # Get the function to call
            func = getattr(swift, f)
            try:
                result = func(*args, **kwargs)
                # For generators we have to copy the results because the
                # service is going out of scope.
                if isinstance(result, types.GeneratorType):
                    results = []
                    LOG.debug('SwiftOperation results:')
                    for r in result:
                        results.append(copy.deepcopy(r))
                        LOG.debug(str(r))
                    result = results
                else:
                    LOG.debug('SwiftOperation result: %s' % str(result))
                return result
            except swft_srv.SwiftError as e:
                LOG.exception(e)
                raise

    @classmethod
    def _get_name_from_listing(cls, results):
        names = []
        for result in results:
            if result['success']:
                for obj in result['listing']:
                    names.append(obj['name'])
        return names

    def _get_container_names(self):
        results = self._run_operation(None, 'list', options={'long': True})
        return self._get_name_from_listing(results)

    def _get_object_names(self, container):
        results = self._run_operation(None, 'list', options={'long': True},
                                      container=container)
        return self._get_name_from_listing(results)

    def _store(self, instance, data):
        """Store the NVRAM into the storage service.

        :param instance: instance object
        :param data: the NVRAM data base64 encoded string
        """
        source = six.StringIO(data)
        obj = swft_srv.SwiftUploadObject(source, object_name=instance.uuid)
        for result in self._run_operation(None, 'upload', self.container,
                                          [obj]):
            if not result['success']:
                # The upload failed.
                raise api.NVRAMUploadException(instance=instance.name,
                                               reason=result)

    @lockutils.synchronized('nvram')
    def store(self, instance, data, force=True):
        """Store the NVRAM into the storage service.

        :param instance: instance object
        :param data: the NVRAM data base64 encoded string
        :param force: boolean whether an update should always be saved,
                      otherwise, check to see if it's changed.
        """

        if not force:
            # See if the entry exists and has not changed.
            results = self._run_operation(None, 'stat', options={'long': True},
                                          container=self.container,
                                          objects=[instance.uuid])
            result = results[0]
            if result['success']:
                existing_hash = result['headers']['etag']
                if six.PY3:
                    data = data.encode('ascii')
                md5 = hashlib.md5(data).hexdigest()
                if existing_hash == md5:
                    LOG.info(_LI('NVRAM has not changed for instance: %s'),
                             instance.name, instance=instance)
                    return

        self._store(instance, data)
        LOG.debug('NVRAM updated for instance: %s' % instance.name)

    def fetch(self, instance):
        """Fetch the NVRAM from the storage service.

        :param instance: instance object
        :returns: the NVRAM data base64 encoded string
        """
        try:
            # Create a temp file for download into
            with tempfile.NamedTemporaryFile(delete=False) as f:
                options = {
                    'out_file': f.name
                }
            # The file is now created and closed for the swift client to use.
            for result in self._run_operation(
                None, 'download', container=self.container,
                objects=[instance.uuid], options=options):

                if result['success']:
                    with open(f.name, 'r') as f:
                        return f.read()
                else:
                    raise api.NVRAMDownloadException(instance=instance.name,
                                                     reason=result)

        finally:
            try:
                os.remove(f.name)
            except Exception:
                LOG.warning(_LW('Could not remove temporary file: %s'), f.name)

    def delete(self, instance):
        """Delete the NVRAM into the storage service.

        :param instance: instance object
        """
        for result in self._run_operation(
            None, 'delete', container=self.container,
            objects=[instance.uuid]):
            # TODO(KYLEH): Not sure what to log here yet.
            LOG.debug('Delete result: %s' % str(result), instance=instance)
            if not result['success']:
                raise api.NVRAMDeleteException(instance=instance.name,
                                               reason=result)
