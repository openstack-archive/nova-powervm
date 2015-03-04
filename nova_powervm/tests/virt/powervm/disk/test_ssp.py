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

    def get_ssp_stor(self, adpt):
        apt = self.apt
        resp = self.ssp_resp
        # Mock up Adapter.search() results.
        # Need to coerce the ssp_resp - which contains a single entry - into
        # looking like a feed (Adapter.search always returns a feed).
        resp.feed = pvm_adp.Feed({}, [resp.entry])
        resp.entry = None
        apt.search.return_value = resp
        ssp_stor = ssp.SSPDiskAdapter({'adapter': adpt,
                                       'host_uuid': 'host_uuid',
                                       'vios_name': 'vios_name',
                                       'vios_uuid': 'vios_uuid'})
        return ssp_stor

    def test_init(self):
        """Bootstrap SSPStorage, testing first call to _fetch_ssp_wrap.

        Driver init should search for SSP by name.
        """
        # Invoke __init__ => initial _fetch_ssp_wrap()
        self.get_ssp_stor(self.apt)
        # Init should call _fetch_ssp_wrap() once.  First _fetch_ssp_wrap()
        # does a search, but not a read.
        self.assertEqual(1, self.apt.search.call_count)
        self.assertEqual(0, self.apt.read.call_count)

    def test_fetch_ssp_wrap_304(self):
        """_fetch_ssp_wrap with etag match."""
        # Save original SSP wrapper for later comparison
        orig_ssp_wrap = pvm_stg.SSP.wrap(self.ssp_resp)
        # Prime _ssp_wrap
        ssp_stor = self.get_ssp_stor(self.apt)
        # Verify baseline call counts
        self.assertEqual(1, self.apt.search.call_count)
        self.assertEqual(0, self.apt.read.call_count)
        # Build a 304 response.  Expect _fetch_ssp_wrap to invoke read (not
        # search), detect the 304, and return the already-saved wrapper.
        # (No exception proves we didn't try to extract and wrap the
        # nonexistent feed/entry from this Response.)
        resp = pvm_adp.Response('meth', 'path', 304, 'reason', {})
        self.apt.read.return_value = resp
        # _fetch_ssp_wrap() with etag match
        ssp_wrap = ssp_stor._fetch_ssp_wrap()
        # This second _fetch_ssp_wrap() should call read, but not search.
        self.assertEqual(1, self.apt.search.call_count)
        self.assertEqual(1, self.apt.read.call_count)
        self.assertEqual(ssp_wrap.name, orig_ssp_wrap.name)

    def test_fetch_ssp_wrap_etag_mismatch(self):
        """_fetch_ssp_wrap with etag mismatch (refetch)."""
        # Prime _ssp_wrap
        ssp_stor = self.get_ssp_stor(self.apt)
        # Verify baseline call counts
        self.assertEqual(1, self.apt.search.call_count)
        self.assertEqual(0, self.apt.read.call_count)
        # Build a Response with a different SSP so we can tell when refetched
        resp = pvm_adp.Response('meth', 'path', 200, 'reason', {})
        new_ssp = pvm_stg.SSP.bld('newssp', [])
        resp.entry = new_ssp.entry
        self.apt.read.return_value = resp
        # _fetch_ssp_wrap() with etag mismatch
        ssp_wrap = ssp_stor._fetch_ssp_wrap()
        # This _fetch_ssp_wrap() should also call read, but not search.
        self.assertEqual(1, self.apt.search.call_count)
        self.assertEqual(1, self.apt.read.call_count)
        self.assertEqual(ssp_wrap.name, new_ssp.name)
