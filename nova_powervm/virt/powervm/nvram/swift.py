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

import copy
import hashlib
import os
import retrying
import six
import tempfile
import types

from nova_powervm import conf as cfg
from nova_powervm.conf import powervm
from nova_powervm.virt.powervm.i18n import _
from nova_powervm.virt.powervm.nvram import api

from oslo_concurrency import lockutils
from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import uuidutils
from swiftclient import exceptions as swft_exc
from swiftclient import service as swft_srv


LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class SwiftNvramStore(api.NvramStore):

    def __init__(self):
        super(SwiftNvramStore, self).__init__()
        self.container = CONF.powervm.swift_container
        # Build the swift service options
        self.options = self._init_swift_options()
        self.swift_service = swft_srv.SwiftService(options=self.options)
        self._container_found = False

    @staticmethod
    def _init_swift_options():
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
            'os_endpoint_type': CONF.powervm.swift_endpoint_type,
        }

        return options

    def _run_operation(self, f, *args, **kwargs):
        """Convenience method to call the Swift client service."""

        # Get the function to call
        func = getattr(self.swift_service, f)
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
                LOG.debug('SwiftOperation result: %s', str(result))
            return result
        except swft_srv.SwiftError:
            with excutils.save_and_reraise_exception(logger=LOG):
                LOG.exception("Error running swift operation.")

    @classmethod
    def _get_name_from_listing(cls, results):
        names = []
        for result in results:
            if result['success']:
                for obj in result['listing']:
                    names.append(obj['name'])
        return names

    def _get_container_names(self):
        results = self._run_operation('list', options={'long': True})
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
            'list', options={'long': True, 'prefix': prefix},
            container=container)
        return self._get_name_from_listing(results)

    def _store(self, inst_key, data, exists=None):
        """Store the NVRAM into the storage service.

        :param inst_key: The key by which to store the data in the repository.
        :param data: the NVRAM data base64 encoded string
        :param exists: (Optional, Default: None) If specified, tells the upload
                       whether or not the object exists.  Should be a boolean
                       or None.  If left as None, the method will look up
                       whether or not it exists.
        """

        # If the object doesn't exist, we tell it to 'leave_segments'.  This
        # prevents a lookup and saves the logs from an ERROR in the swift
        # client (that really isn't an error...sigh).  It should be empty
        # if not the first upload (which defaults to leave_segments=False)
        # so that it overrides the existing element on a subsequent upload.
        if exists is None:
            exists = self._exists(inst_key)
        options = dict(leave_segments=True) if not exists else None

        # The swift client already has a retry opertaion. The retry method
        # takes a 'reset' function as a parameter. This parameter is 'None'
        # for all operations except upload. For upload, it's set to a default
        # method that throws a ClientException if the object to upload doesn't
        # implement tell/see/reset. If the authentication error occurs during
        # upload, this ClientException is raised with no retry. For any other
        # operation, swift client will retry and succeed.
        @retrying.retry(retry_on_result=lambda result: result,
                        wait_fixed=250, stop_max_attempt_number=2)
        def _run_upload_operation():
            """Run the upload operation

            Attempts retry for a maximum number of two times. The upload
            operation will fail with ClientException, if there is an
            authentication error. The second attempt only happens if the
            first attempt failed with ClientException. A return value of
            True means we should retry, and False means no failure during
            upload, thus no retry is required.

            Raises RetryError if the upload failed during second attempt,
            as the number of attempts for retry is reached.

            """
            source = six.StringIO(data)
            obj = swft_srv.SwiftUploadObject(source, object_name=inst_key)

            results = self._run_operation('upload', self.container,
                                          [obj], options=options)
            for result in results:
                if not result['success']:
                    # TODO(arun-mani - Bug 1611011): Filed for updating swift
                    # client to return http status code in case of failure
                    if isinstance(result['error'], swft_exc.ClientException):
                        # If upload failed during nvram/slot_map update due to
                        # expired keystone token, retry swift-client operation
                        # to allow regeneration of token
                        LOG.warning('NVRAM upload failed due to invalid '
                                    'token. Retrying upload.')
                        return True
                    # The upload failed.
                    raise api.NVRAMUploadException(instance=inst_key,
                                                   reason=result)
            return False
        try:
            _run_upload_operation()
        except retrying.RetryError as re:
            # The upload failed.
            reason = (_('Unable to store NVRAM after %d attempts') %
                      re.last_attempt.attempt_number)
            raise api.NVRAMUploadException(instance=inst_key, reason=reason)

    @lockutils.synchronized('nvram')
    def store(self, instance, data, force=True):
        """Store the NVRAM into the storage service.

        :param instance: The nova instance object OR instance UUID.
        :param data: the NVRAM data base64 encoded string
        :param force: boolean whether an update should always be saved,
                      otherwise, check to see if it's changed.
        """
        inst_uuid = (instance if
                     uuidutils.is_uuid_like(instance) else instance.uuid)
        exists = self._exists(inst_uuid)
        if not force and exists:
            # See if the entry exists and has not changed.
            results = self._run_operation('stat', options={'long': True},
                                          container=self.container,
                                          objects=[inst_uuid])
            result = results[0]
            if result['success']:
                existing_hash = result['headers']['etag']
                if six.PY3:
                    data = data.encode('ascii')
                md5 = hashlib.md5(data).hexdigest()
                if existing_hash == md5:
                    LOG.info(('NVRAM has not changed for instance with '
                             'UUID %s.'), inst_uuid)
                    return

        self._store(inst_uuid, data, exists=exists)
        LOG.debug('NVRAM updated for instance with UUID %s', inst_uuid)

    def store_slot_map(self, inst_key, data):
        """Store the Slot Map to Swift.

        :param inst_key: The instance key to use for the storage operation.
        :param data: The data of the object to store.  This should be a string.
        """
        self._store(inst_key, data)

    def fetch_slot_map(self, inst_key):
        """Fetch the Slot Map object.

        :param inst_key: The instance key to use for the storage operation.
        :returns: The slot map (as a string)
        """
        return self._fetch(inst_key)[0]

    def fetch(self, instance):
        """Fetch the NVRAM from the storage service.

        :param instance: The nova instance object or instance UUID.
        :returns: the NVRAM data base64 encoded string
        """
        inst_uuid = (instance if
                     uuidutils.is_uuid_like(instance) else instance.uuid)
        data, result = self._fetch(inst_uuid)
        if not data:
            raise api.NVRAMDownloadException(instance=inst_uuid,
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
            results = self._run_operation(
                'download', container=self.container, objects=[object_key],
                options=options)
            for result in results:
                if result['success']:
                    with open(f.name, 'r') as f:
                        return f.read(), result
                else:
                    return None, result
        finally:
            try:
                os.remove(f.name)
            except Exception:
                LOG.warning('Could not remove temporary file: %s', f.name)

    def delete_slot_map(self, inst_key):
        """Delete the Slot Map from Swift.

        :param inst_key: The instance key to use for the storage operation.
        """
        for result in self._run_operation('delete', container=self.container,
                                          objects=[inst_key]):

            LOG.debug('Delete slot map result: %s', str(result))
            if not result['success']:
                raise api.NVRAMDeleteException(reason=result,
                                               instance=inst_key)

    def delete(self, instance):
        """Delete the NVRAM into the storage service.

        :param instance: The nova instance object OR instance UUID.
        """
        inst_uuid = (instance if
                     uuidutils.is_uuid_like(instance) else instance.uuid)
        for result in self._run_operation('delete', container=self.container,
                                          objects=[inst_uuid]):

            LOG.debug('Delete result for instance with UUID %(inst_uuid)s: '
                      '%(res)s', {'inst_uuid': inst_uuid, 'res': result})
            if not result['success']:
                raise api.NVRAMDeleteException(instance=inst_uuid,
                                               reason=result)
