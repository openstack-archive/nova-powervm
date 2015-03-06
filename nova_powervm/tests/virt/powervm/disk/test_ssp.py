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

from nova import test
import os
import pypowervm.adapter as pvm_adp
from pypowervm.tests.wrappers.util import pvmhttp
from pypowervm.wrappers import storage as pvm_stg

from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm.disk import ssp


SSP = 'fake_ssp.txt'


class TestSSPDiskAdapter(test.TestCase):
    """Unit Tests for the LocalDisk storage driver."""

    def setUp(self):
        super(TestSSPDiskAdapter, self).setUp()

        # Find directory for response file(s)
        data_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(data_dir, "..", 'data')

        def resp(file_name):
            file_path = os.path.join(data_dir, file_name)
            return pvmhttp.load_pvm_resp(file_path).get_response()

        self.ssp_resp = resp(SSP)
        self.pypvm = self.useFixture(fx.PyPowerVM())
        self.apt = self.pypvm.apt

        # Adapter.search always returns a feed.
        self.apt.search.return_value = self._bld_resp(
            entry_or_list=[self.ssp_resp.entry])

        # Default Adapter.read to 304 - we'll always get self.ssp_resp
        self.apt.read.return_value = self._bld_resp(status=304)

    def _get_ssp_stor(self):
        ssp_stor = ssp.SSPDiskAdapter({'adapter': self.apt,
                                       'host_uuid': 'host_uuid',
                                       'vios_name': 'vios_name',
                                       'vios_uuid': 'vios_uuid'})
        return ssp_stor

    def _bld_resp(self, status=200, entry_or_list=None):
        """Build a pypowervm.adapter.Response for mocking Adapter.search/read.

        :param status: HTTP status of the Response.
        :param entry_or_list: pypowervm.adapter.Entry or list thereof.  If
                              None, the Response has no content.  If an Entry,
                              the Response looks like it got back <entry/>.  If
                              a list of Entry, the Response looks like it got
                              back <feed/>.
        :return: pypowervm.adapter.Response suitable for mocking
                 pypowervm.adapter.Adapter.search or read.
        """
        resp = pvm_adp.Response('meth', 'path', status, 'reason', {})
        if entry_or_list is not None:
            if isinstance(entry_or_list, list):
                resp.feed = pvm_adp.Feed({}, entry_or_list)
            else:
                resp.entry = entry_or_list
        return resp

    def test_init(self):
        """Bootstrap SSPStorage, testing first call to _fetch_ssp_wrap.

        Driver init should search for SSP by name.
        """
        # Invoke __init__ => initial _fetch_ssp_wrap()
        self._get_ssp_stor()
        # Init should call _fetch_ssp_wrap() once.  First _fetch_ssp_wrap()
        # does a search, but not a read.
        self.assertEqual(1, self.apt.search.call_count)
        self.assertEqual(0, self.apt.read.call_count)

    def test_fetch_ssp_wrap_not_modified(self):
        """_fetch_ssp_wrap with etag match."""
        # Save original SSP wrapper for later comparison
        orig_ssp_wrap = pvm_stg.SSP.wrap(self.ssp_resp)
        # Prime _ssp_wrap
        ssp_stor = self._get_ssp_stor()
        # Verify baseline call counts
        self.assertEqual(1, self.apt.search.call_count)
        self.assertEqual(0, self.apt.read.call_count)
        # Default read already mocked to no-content 304 (Not Modified).
        # Expect _fetch_ssp_wrap to invoke read (not search), detect the 304,
        # and return the already-saved wrapper. (No exception proves we didn't
        # try to extract and wrap the nonexistent feed/entry from this
        # Response.)
        ssp_wrap = ssp_stor._fetch_ssp_wrap()
        # This second _fetch_ssp_wrap() should call read, but not search.
        self.assertEqual(1, self.apt.search.call_count)
        self.assertEqual(1, self.apt.read.call_count)
        self.assertEqual(ssp_wrap.name, orig_ssp_wrap.name)

    def test_fetch_ssp_wrap_etag_mismatch(self):
        """_fetch_ssp_wrap with etag mismatch (refetch)."""
        # Prime _ssp_wrap
        ssp_stor = self._get_ssp_stor()
        # Verify baseline call counts
        self.assertEqual(1, self.apt.search.call_count)
        self.assertEqual(0, self.apt.read.call_count)
        # Build a Response with a different SSP so we can tell when refetched
        new_ssp = pvm_stg.SSP.bld('newssp', [])
        self.apt.read.return_value = self._bld_resp(
            entry_or_list=new_ssp.entry)
        # _fetch_ssp_wrap() with etag mismatch
        ssp_wrap = ssp_stor._fetch_ssp_wrap()
        # This _fetch_ssp_wrap() should also call read, but not search.
        self.assertEqual(1, self.apt.search.call_count)
        self.assertEqual(1, self.apt.read.call_count)
        self.assertEqual(ssp_wrap.name, new_ssp.name)

    def test_capacity(self):
        ssp_stor = self._get_ssp_stor()
        self.assertEqual(49.88, ssp_stor.capacity)

    def test_capacity_used(self):
        ssp_stor = self._get_ssp_stor()
        self.assertEqual((49.88 - 48.98), ssp_stor.capacity_used)
