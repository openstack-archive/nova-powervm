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
from pypowervm.tests.wrappers.util import pvmhttp
from pypowervm.wrappers import logical_partition as pvm_lpar

from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm import mgmt

LPAR_HTTPRESP_FILE = "lpar.txt"


class TestVM(test.TestCase):
    def setUp(self):
        super(TestVM, self).setUp()
        self.pypvm = self.useFixture(fx.PyPowerVM())
        self.apt = self.pypvm.apt

        lpar_http = pvmhttp.load_pvm_resp(LPAR_HTTPRESP_FILE, adapter=self.apt)
        self.assertNotEqual(lpar_http, None,
                            "Could not load %s " %
                            LPAR_HTTPRESP_FILE)

        self.resp = lpar_http.response

    def test_get_mgmt_partition(self):
        self.apt.read.return_value = self.resp
        mp_wrap = mgmt.get_mgmt_partition(self.apt)
        self.assertIsInstance(mp_wrap, pvm_lpar.LPAR)
        self.assertTrue(mp_wrap.is_mgmt_partition)
