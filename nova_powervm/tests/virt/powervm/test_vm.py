# Copyright 2014, 2015 IBM Corp.
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
#

import logging

import mock

from nova.compute import power_state
from nova import exception
from nova import test
from pypowervm import exceptions as pvm_exc
from pypowervm.tests.wrappers.util import pvmhttp

from nova_powervm.virt.powervm import vm

LPAR_HTTPRESP_FILE = "lpar.txt"
LPAR_MAPPING = (
    {
        'nova-z3-9-5-126-127-00000001': '089ffb20-5d19-4a8c-bb80-13650627d985',
        'nova-z3-9-5-126-208-000001f0': '668b0882-c24a-4ae9-91c8-297e95e3fe29'
    })

LOG = logging.getLogger(__name__)
logging.basicConfig()


class FakeAdapterResponse(object):
    def __init__(self, status):
        self.status = status


class FakeInstance(object):
    def __init__(self):
        self.name = 'fake_instance'


class FakeFlavor(object):
    def __init__(self):
        self.name = 'fake_flavor'
        self.memory_mb = 256
        self.vcpus = 1


class TestVM(test.TestCase):
    def setUp(self):
        super(TestVM, self).setUp()
        lpar_http = pvmhttp.load_pvm_resp(LPAR_HTTPRESP_FILE)
        self.assertNotEqual(lpar_http, None,
                            "Could not load %s " %
                            LPAR_HTTPRESP_FILE)

        self.resp = lpar_http.response

    @mock.patch('pypowervm.adapter.Adapter')
    def test_uuid_cache(self, mock_adr):
        cache = vm.UUIDCache(mock_adr)
        cache.add('n1', '123')
        self.assertEqual(cache.lookup('n1'), '123')

        self.assertEqual(cache.lookup('nothing', fetch=False), None)

        cache.remove('n1')
        self.assertEqual(cache.lookup('n1', fetch=False), None)
        self.assertRaises(exception.InstanceNotFound,
                          cache.lookup, 'n1', fetch=True)
        # Ensure removing one that doesn't exist is just ignored
        self.assertEqual(cache.remove('nonExistant'), None)

        cache.load_from_feed(self.resp.feed)
        for lpar in LPAR_MAPPING:
            self.assertEqual(cache.lookup(lpar), LPAR_MAPPING[lpar].upper())

        # Test fetching. Start with a fresh cache and try to look it up
        cache = vm.UUIDCache(mock_adr)

        def return_response(*args, **kwds):
            return self.resp

        mock_adr.read.side_effect = return_response
        self.assertEqual(cache.lookup('nova-z3-9-5-126-127-00000001'),
                         '089ffb20-5d19-4a8c-bb80-13650627d985'.upper())
        # Test it returns None even when we try to look it up
        self.assertEqual(cache.lookup('NoneExistant'), None)

        exc = pvm_exc.Error('Not found', response=FakeAdapterResponse(404))
        mock_adr.read.side_effect = exc
        self.assertRaises(exception.InstanceNotFound,
                          cache.lookup, 'NoneExistant')

    @mock.patch('pypowervm.adapter.Adapter')
    def test_instance_info(self, mock_adr):

        # Test at least one state translation
        self.assertEqual(vm._translate_vm_state('running'),
                         power_state.RUNNING)

        inst_info = vm.InstanceInfo(mock_adr, 'inst_name', '1234')
        # Test the static properties
        self.assertEqual(inst_info.id, '1234')
        self.assertEqual(inst_info.cpu_time_ns, 0)

        # Check that we raise an exception if the instance is gone.
        exc = pvm_exc.Error('Not found', response=FakeAdapterResponse(404))
        mock_adr.read.side_effect = exc
        self.assertRaises(exception.InstanceNotFound,
                          inst_info.__getattribute__, 'state')

        # Reset the test inst_info
        inst_info = vm.InstanceInfo(mock_adr, 'inst_name', '1234')

        class FakeResp2(object):
            def __init__(self, body):
                self.body = '"%s"' % body

        resp = FakeResp2('running')

        def return_resp(*args, **kwds):
            return resp

        mock_adr.read.side_effect = return_resp
        self.assertEqual(inst_info.state, power_state.RUNNING)

        # Check the __eq__ method
        inst_info1 = vm.InstanceInfo(mock_adr, 'inst_name', '1234')
        inst_info2 = vm.InstanceInfo(mock_adr, 'inst_name', '1234')
        self.assertEqual(inst_info1, inst_info2)
        inst_info2 = vm.InstanceInfo(mock_adr, 'name', '4321')
        self.assertNotEqual(inst_info1, inst_info2)

    @mock.patch('pypowervm.adapter.Adapter')
    def test_get_lpar_feed(self, mock_adr):
        mock_adr.read.return_value = self.resp
        feed = vm.get_lpar_feed(mock_adr, 'host_uuid')
        self.assertEqual(feed, self.resp.feed)

        exc = pvm_exc.Error('Not found', response=FakeAdapterResponse(404))
        mock_adr.read.side_effect = exc
        feed = vm.get_lpar_feed(mock_adr, 'host_uuid')
        self.assertEqual(feed, None)

    @mock.patch('pypowervm.adapter.Adapter')
    @mock.patch('nova_powervm.virt.powervm.vm.get_lpar_feed')
    def test_get_lpar_list(self, mock_feed, mock_adr):
        mock_feed.return_value = self.resp.feed
        lpar_list = vm.get_lpar_list(mock_adr, 'host_uuid')
        # Check the first one in the feed and the length of the feed
        self.assertEqual(lpar_list[0], 'nova-z3-9-5-126-127-00000001')
        self.assertEqual(len(lpar_list), 21)

    @mock.patch('pypowervm.adapter.Adapter')
    def test_crt_lpar(self, mock_adr):
        instance = FakeInstance()
        flavor = FakeFlavor()
        vm.crt_lpar(mock_adr, 'host_uuid', instance, flavor)
        self.assertTrue(mock_adr.create.called)
