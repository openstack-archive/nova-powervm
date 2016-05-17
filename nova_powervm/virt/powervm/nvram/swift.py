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
from nova_powervm.virt.powervm.i18n import _
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
        self._container_found = False

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

    def _get_object_names(self, container, prefix=None):
        # If this is the first pass, the container may not exist yet.  Check
        # to make sure it does, otherwise the list of the object names will
        # fail.
        if not self._container_found:
            container_names = self._get_container_names()
            self._container_found = (container in container_names)

            # If the container was still not found, then just return an empty
            # list.  There are no objects.
            if not self._container_found:
                return []

        results = self._run_operation(
            None, 'list', options={'long': True, 'prefix': prefix},
            container=container)
        return self._get_name_from_listing(results)

    def _store(self, inst_key, inst_name, data, exists=None):
        """Store the NVRAM into the storage service.

        :param instance: instance object
        :param data: the NVRAM data base64 encoded string
        :param exists: (Optional, Default: None) If specified, tells the upload
                       whether or not the object exists.  Should be a boolean
                       or None.  If left as None, the method will look up
                       whether or not it exists.
        """
        source = six.StringIO(data)

        # If the object doesn't exist, we tell it to 'leave_segments'.  This
        # prevents a lookup and saves the logs from an ERROR in the swift
        # client (that really isn't an error...sigh).  It should be empty
        # if not the first upload (which defaults to leave_segments=False)
        # so that it overrides the existing element on a subsequent upload.
        if exists is None:
            exists = self._exists(inst_key)
        options = dict(leave_segments=True) if not exists else None

        obj = swft_srv.SwiftUploadObject(source, object_name=inst_key)
        for result in self._run_operation(None, 'upload', self.container,
                                          [obj], options=options):
            if not result['success']:
                # The upload failed.
                raise api.NVRAMUploadException(instance=inst_name,
                                               reason=result)

    @lockutils.synchronized('nvram')
    def store(self, instance, data, force=True):
        """Store the NVRAM into the storage service.

        :param instance: instance object
        :param data: the NVRAM data base64 encoded string
        :param force: boolean whether an update should always be saved,
                      otherwise, check to see if it's changed.
        """
        exists = self._exists(instance.uuid)
        if not force and exists:
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

        self._store(instance.uuid, instance.name, data, exists=exists)
        LOG.debug('NVRAM updated for instance: %s' % instance.name)

    def store_slot_map(self, inst_key, data):
        """Store the Slot Map to Swift.

        :param inst_key: The instance key to use for the storage operation.
        :param data: The data of the object to store.  This should be a string.
        """
        self._store(inst_key, inst_key, data)

    def fetch_slot_map(self, inst_key):
        """Fetch the Slot Map object.

        :param inst_key: The instance key to use for the storage operation.
        :returns: The slot map (as a string)
        """
        return self._fetch(inst_key)[0]

    def fetch(self, instance):
        """Fetch the NVRAM from the storage service.

        :param instance: instance object
        :returns: the NVRAM data base64 encoded string
        """
        data, result = self._fetch(instance.uuid)
        if not data:
            raise api.NVRAMDownloadException(instance=instance.name,
                                             reason=result)
        return data

    def _exists(self, object_key):
        # Search by prefix, but since this is just a prefix, we need to loop
        # and do a check to make sure it fully looks.
        obj_names = self._get_object_names(self.container, prefix=object_key)
        for obj in obj_names:
            if object_key == obj:
                return True
        return False

    def _fetch(self, object_key):
        # Check if the object exists.  If not, return a result accordingly.
        if not self._exists(object_key):
            return None, _('Object does not exist in Swift.')

        try:
            # Create a temp file for download into
            with tempfile.NamedTemporaryFile(delete=False) as f:
                options = {
                    'out_file': f.name
                }
            # The file is now created and closed for the swift client to use.
            for result in self._run_operation(
                None, 'download', container=self.container,
                objects=[object_key], options=options):

                if result['success']:
                    with open(f.name, 'r') as f:
                        return f.read(), result
                else:
                    return None, result
        finally:
            try:
                os.remove(f.name)
            except Exception:
                LOG.warning(_LW('Could not remove temporary file: %s'), f.name)

    def delete_slot_map(self, inst_key):
        """Delete the Slot Map from Swift.

        :param inst_key: The instance key to use for the storage operation.
        """
        for result in self._run_operation(
            None, 'delete', container=self.container,
            objects=[inst_key]):

            LOG.debug('Delete slot map result: %s' % str(result))
            if not result['success']:
                raise api.NVRAMDeleteException(reason=result,
                                               instance=inst_key)

    def delete(self, instance):
        """Delete the NVRAM into the storage service.

        :param instance: instance object
        """
        for result in self._run_operation(
            None, 'delete', container=self.container,
            objects=[instance.uuid]):

            LOG.debug('Delete result: %s' % str(result), instance=instance)
            if not result['success']:
                raise api.NVRAMDeleteException(instance=instance.name,
                                               reason=result)
