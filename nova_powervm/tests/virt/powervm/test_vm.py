# Copyright 2014, 2018 IBM Corp.
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

from __future__ import absolute_import

import fixtures
import logging
import mock

from nova.compute import power_state
from nova.compute import task_states
from nova import exception
from nova import objects
from nova import test
from nova.virt import event
from pypowervm import exceptions as pvm_exc
from pypowervm.helpers import log_helper as pvm_log
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.tests.test_utils import pvmhttp
from pypowervm.utils import lpar_builder as lpar_bld
from pypowervm.wrappers import base_partition as pvm_bp
from pypowervm.wrappers import logical_partition as pvm_lpar

from nova_powervm.tests.virt import powervm
from nova_powervm.virt.powervm import exception as nvex
from nova_powervm.virt.powervm import vm


LPAR_HTTPRESP_FILE = "lpar.txt"
LPAR_MAPPING = (
    {
        'z3-9-5-126-127-00000001': '089ffb20-5d19-4a8c-bb80-13650627d985',
        'z3-9-5-126-208-000001f0': '668b0882-c24a-4ae9-91c8-297e95e3fe29'
    })

LOG = logging.getLogger(__name__)
logging.basicConfig()


class FakeAdapterResponse(object):
    def __init__(self, status):
        self.status = status


class TestVMBuilder(test.NoDBTestCase):

    def setUp(self):
        super(TestVMBuilder, self).setUp()

        self.adpt = mock.MagicMock()
        self.host_w = mock.MagicMock()
        self.lpar_b = vm.VMBuilder(self.host_w, self.adpt)

        self.san_lpar_name = self.useFixture(fixtures.MockPatch(
            'pypowervm.util.sanitize_partition_name_for_api')).mock
        self.san_lpar_name.side_effect = lambda name: name

    def test_resize_attributes_maintained(self):
        lpar_w = mock.MagicMock()
        lpar_w.io_config.max_virtual_slots = 200
        lpar_w.proc_config.shared_proc_cfg.pool_id = 56
        lpar_w.avail_priority = 129
        lpar_w.srr_enabled = False
        lpar_w.proc_compat_mode = 'POWER7'
        lpar_w.allow_perf_data_collection = True
        vm_bldr = vm.VMBuilder(self.host_w, self.adpt, cur_lpar_w=lpar_w)
        self.assertEqual(200, vm_bldr.stdz.max_slots)
        self.assertEqual(56, vm_bldr.stdz.spp)
        self.assertEqual(129, vm_bldr.stdz.avail_priority)
        self.assertFalse(vm_bldr.stdz.srr)
        self.assertEqual('POWER7', vm_bldr.stdz.proc_compat)
        self.assertTrue(vm_bldr.stdz.enable_lpar_metric)

    def test_max_vslots_is_the_greater(self):
        lpar_w = mock.MagicMock()
        lpar_w.io_config.max_virtual_slots = 64
        lpar_w.proc_config.shared_proc_cfg.pool_id = 56
        lpar_w.avail_priority = 129
        lpar_w.srr_enabled = False
        lpar_w.proc_compat_mode = 'POWER7'
        lpar_w.allow_perf_data_collection = True
        slot_mgr = mock.MagicMock()
        slot_mgr.build_map.get_max_vslots.return_value = 128
        vm_bldr = vm.VMBuilder(
            self.host_w, self.adpt, slot_mgr=slot_mgr, cur_lpar_w=lpar_w)
        self.assertEqual(128, vm_bldr.stdz.max_slots)

    def test_conf_values(self):
        # Test driver CONF values are passed to the standardizer
        self.flags(uncapped_proc_weight=75, proc_units_factor=.25,
                   group='powervm')
        lpar_bldr = vm.VMBuilder(self.host_w, self.adpt)
        self.assertEqual(75, lpar_bldr.stdz.uncapped_weight)
        self.assertEqual(.25, lpar_bldr.stdz.proc_units_factor)

    def test_format_flavor(self):
        """Perform tests against _format_flavor."""
        instance = objects.Instance(**powervm.TEST_INSTANCE)
        flavor = instance.get_flavor()
        # LP 1561128, simplified remote restart is enabled by default
        lpar_attrs = {'memory': 2048,
                      'name': 'instance-00000001',
                      'uuid': '49629a5c-f4c4-4721-9511-9725786ff2e5',
                      'vcpu': 1, 'srr_capability': True}

        # Test dedicated procs
        flavor.extra_specs = {'powervm:dedicated_proc': 'true'}
        test_attrs = dict(lpar_attrs, dedicated_proc='true')

        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)
        self.san_lpar_name.assert_called_with(instance.name)
        self.san_lpar_name.reset_mock()

        # Test dedicated procs, min/max vcpu and sharing mode
        flavor.extra_specs = {'powervm:dedicated_proc': 'true',
                              'powervm:dedicated_sharing_mode':
                              'share_idle_procs_active',
                              'powervm:min_vcpu': '1',
                              'powervm:max_vcpu': '3'}
        test_attrs = dict(lpar_attrs,
                          dedicated_proc='true',
                          sharing_mode='sre idle procs active',
                          min_vcpu='1', max_vcpu='3')
        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)
        self.san_lpar_name.assert_called_with(instance.name)
        self.san_lpar_name.reset_mock()

        # Test shared proc sharing mode
        flavor.extra_specs = {'powervm:uncapped': 'true'}
        test_attrs = dict(lpar_attrs, sharing_mode='uncapped')
        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)
        self.san_lpar_name.assert_called_with(instance.name)
        self.san_lpar_name.reset_mock()

        # Test availability priority
        flavor.extra_specs = {'powervm:availability_priority': '150'}
        test_attrs = dict(lpar_attrs, avail_priority='150')
        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)
        self.san_lpar_name.assert_called_with(instance.name)
        self.san_lpar_name.reset_mock()

        # Test the Enable LPAR Metrics for true value
        flavor.extra_specs = {'powervm:enable_lpar_metric': 'true'}
        test_attrs = dict(lpar_attrs, enable_lpar_metric=True)
        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)
        self.san_lpar_name.assert_called_with(instance.name)
        self.san_lpar_name.reset_mock()

        # Test the Enable LPAR Metrics for false value
        flavor.extra_specs = {'powervm:enable_lpar_metric': 'false'}
        test_attrs = dict(lpar_attrs, enable_lpar_metric=False)
        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)
        self.san_lpar_name.assert_called_with(instance.name)
        self.san_lpar_name.reset_mock()

        # Test processor compatibility
        flavor.extra_specs = {'powervm:processor_compatibility': 'POWER8'}
        test_attrs = dict(lpar_attrs, processor_compatibility='POWER8')
        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)
        self.san_lpar_name.assert_called_with(instance.name)
        self.san_lpar_name.reset_mock()

        flavor.extra_specs = {'powervm:processor_compatibility': 'POWER6+'}
        test_attrs = dict(
            lpar_attrs,
            processor_compatibility=pvm_bp.LPARCompat.POWER6_PLUS)
        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)
        self.san_lpar_name.assert_called_with(instance.name)
        self.san_lpar_name.reset_mock()

        flavor.extra_specs = {'powervm:processor_compatibility':
                              'POWER6+_Enhanced'}
        test_attrs = dict(
            lpar_attrs,
            processor_compatibility=pvm_bp.LPARCompat.POWER6_PLUS_ENHANCED)
        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)
        self.san_lpar_name.assert_called_with(instance.name)
        self.san_lpar_name.reset_mock()

        # Test min, max proc units
        flavor.extra_specs = {'powervm:min_proc_units': '0.5',
                              'powervm:max_proc_units': '2.0'}
        test_attrs = dict(lpar_attrs, min_proc_units='0.5',
                          max_proc_units='2.0')
        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)
        self.san_lpar_name.assert_called_with(instance.name)
        self.san_lpar_name.reset_mock()

        # Test min, max mem
        flavor.extra_specs = {'powervm:min_mem': '1024',
                              'powervm:max_mem': '4096'}
        test_attrs = dict(lpar_attrs, min_mem='1024', max_mem='4096')
        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)
        self.san_lpar_name.assert_called_with(instance.name)
        self.san_lpar_name.reset_mock()

        # Test remote restart set to false
        flavor.extra_specs = {'powervm:srr_capability': 'false'}
        test_attrs = dict(lpar_attrs, srr_capability=False)
        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)

        # Test PPT set
        flavor.extra_specs = {'powervm:ppt_ratio': '1:64'}
        test_attrs = dict(lpar_attrs, ppt_ratio='1:64')
        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)

        # Test enforce affinity check set to true
        flavor.extra_specs = {'powervm:enforce_affinity_check': 'true'}
        test_attrs = dict(lpar_attrs, enforce_affinity_check=True)
        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)

        # Test enforce affinity check set to false
        flavor.extra_specs = {'powervm:enforce_affinity_check': 'false'}
        test_attrs = dict(lpar_attrs, enforce_affinity_check=False)
        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)

        # Test enforce affinity check set to invalid value
        flavor.extra_specs = {'powervm:enforce_affinity_check': 'invalid'}
        self.assertRaises(exception.ValidationError,
                          self.lpar_b._format_flavor, instance)

        # Test PPT ratio not set when rebuilding to non-supported host
        flavor.extra_specs = {'powervm:ppt_ratio': '1:4096'}
        instance.task_state = task_states.REBUILD_SPAWNING
        test_attrs = dict(lpar_attrs)
        self.lpar_b.host_w.get_capability.return_value = False
        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)
        self.lpar_b.host_w.get_capability.assert_called_once_with(
            'physical_page_table_ratio_capable')

        # Test affinity check not set when rebuilding to non-supported host
        self.lpar_b.host_w.get_capability.reset_mock()
        flavor.extra_specs = {'powervm:enforce_affinity_check': 'true'}
        self.assertEqual(self.lpar_b._format_flavor(instance), test_attrs)
        self.lpar_b.host_w.get_capability.assert_called_once_with(
            'affinity_check_capable')

    @mock.patch('pypowervm.wrappers.shared_proc_pool.SharedProcPool.search')
    def test_spp_pool_id(self, mock_search):
        # The default pool is always zero.  Validate the path.
        self.assertEqual(0, self.lpar_b._spp_pool_id('DefaultPool'))
        self.assertEqual(0, self.lpar_b._spp_pool_id(None))

        # Further invocations require calls to the adapter.  Build a minimal
        # mocked SPP wrapper
        spp = mock.MagicMock()
        spp.id = 1

        # Three invocations.  First has too many elems.  Second has none.
        # Third is just right.  :-)
        mock_search.side_effect = [[spp, spp], [], [spp]]

        self.assertRaises(exception.ValidationError, self.lpar_b._spp_pool_id,
                          'fake_name')
        self.assertRaises(exception.ValidationError, self.lpar_b._spp_pool_id,
                          'fake_name')

        self.assertEqual(1, self.lpar_b._spp_pool_id('fake_name'))

    def test_flavor_bool(self):
        true_iterations = ['true', 't', 'yes', 'y', 'TrUe', 'YeS', 'Y', 'T']
        for t in true_iterations:
            self.assertTrue(self.lpar_b._flavor_bool(t, 'key'))

        false_iterations = ['false', 'f', 'no', 'n', 'FaLSe', 'nO', 'F', 'N']
        for f in false_iterations:
            self.assertFalse(self.lpar_b._flavor_bool(f, 'key'))

        raise_iterations = ['NotGood', '', 'invalid']
        for r in raise_iterations:
            self.assertRaises(exception.ValidationError,
                              self.lpar_b._flavor_bool, r, 'key')


class TestVM(test.NoDBTestCase):
    def setUp(self):
        super(TestVM, self).setUp()
        self.apt = self.useFixture(pvm_fx.AdapterFx(
            traits=pvm_fx.LocalPVMTraits)).adpt
        self.apt.helpers = [pvm_log.log_helper]

        self.san_lpar_name = self.useFixture(fixtures.MockPatch(
            'pypowervm.util.sanitize_partition_name_for_api')).mock
        self.san_lpar_name.side_effect = lambda name: name

        lpar_http = pvmhttp.load_pvm_resp(LPAR_HTTPRESP_FILE, adapter=self.apt)
        self.assertNotEqual(lpar_http, None,
                            "Could not load %s " %
                            LPAR_HTTPRESP_FILE)

        self.resp = lpar_http.response

    def test_translate_event(self):
        # (expected event, pvm state, power_state)
        tests = [
            (event.EVENT_LIFECYCLE_STARTED, "running", power_state.SHUTDOWN),
            (None, "running", power_state.RUNNING)
        ]
        for t in tests:
            self.assertEqual(t[0], vm.translate_event(t[1], t[2]))

    @mock.patch.object(objects.Instance, 'get_by_uuid')
    def test_get_instance(self, mock_get_uuid):
        mock_get_uuid.return_value = '1111'
        self.assertEqual('1111', vm.get_instance('ctx', 'ABC'))

        mock_get_uuid.side_effect = [
            exception.InstanceNotFound({'instance_id': 'fake_instance'}),
            '222'
        ]
        self.assertEqual('222', vm.get_instance('ctx', 'ABC'))

    def test_uuid_set_high_bit(self):
        self.assertEqual(
            vm._uuid_set_high_bit('65e7a5f0-ceb2-427d-a6d1-e47f0eb38708'),
            'e5e7a5f0-ceb2-427d-a6d1-e47f0eb38708')
        self.assertEqual(
            vm._uuid_set_high_bit('f6f79d3f-eef1-4009-bfd4-172ab7e6fff4'),
            'f6f79d3f-eef1-4009-bfd4-172ab7e6fff4')

    def test_translate_vm_state(self):
        self.assertEqual(power_state.RUNNING,
                         vm._translate_vm_state('running'))
        self.assertEqual(power_state.RUNNING,
                         vm._translate_vm_state('migrating running'))
        self.assertEqual(power_state.RUNNING,
                         vm._translate_vm_state('starting'))
        self.assertEqual(power_state.RUNNING,
                         vm._translate_vm_state('open firmware'))
        self.assertEqual(power_state.RUNNING,
                         vm._translate_vm_state('shutting down'))
        self.assertEqual(power_state.RUNNING,
                         vm._translate_vm_state('suspending'))

        self.assertEqual(power_state.SHUTDOWN,
                         vm._translate_vm_state('migrating not active'))
        self.assertEqual(power_state.SHUTDOWN,
                         vm._translate_vm_state('not activated'))

        self.assertEqual(power_state.NOSTATE,
                         vm._translate_vm_state('unknown'))
        self.assertEqual(power_state.NOSTATE,
                         vm._translate_vm_state('hardware discovery'))
        self.assertEqual(power_state.NOSTATE,
                         vm._translate_vm_state('not available'))

        self.assertEqual(power_state.SUSPENDED,
                         vm._translate_vm_state('resuming'))
        self.assertEqual(power_state.SUSPENDED,
                         vm._translate_vm_state('suspended'))

        self.assertEqual(power_state.CRASHED,
                         vm._translate_vm_state('error'))

    def test_get_lpars(self):
        self.apt.read.return_value = self.resp
        lpars = vm.get_lpars(self.apt)
        # One of the LPARs is a management partition, so one less than the
        # total length should be returned.
        self.assertEqual(len(self.resp.feed.entries) - 1, len(lpars))

        exc = pvm_exc.Error('Not found', response=FakeAdapterResponse(404))
        self.apt.read.side_effect = exc
        self.assertRaises(pvm_exc.Error, vm.get_lpars, self.apt)

    def test_get_lpar_names(self):
        self.apt.read.return_value = self.resp
        lpar_list = vm.get_lpar_names(self.apt)
        # Check the first one in the feed and the length of the feed
        self.assertEqual(lpar_list[0], 'z3-9-5-126-208-000001f0')
        self.assertEqual(len(lpar_list), 20)

    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid', autospec=True)
    @mock.patch('pypowervm.tasks.vterm.close_vterm', autospec=True)
    def test_dlt_lpar(self, mock_vterm, mock_pvm_uuid):
        """Performs a delete LPAR test."""
        mock_pvm_uuid.return_value = 'pvm_uuid'

        vm.delete_lpar(self.apt, 'inst')
        mock_pvm_uuid.assert_called_once_with('inst')
        mock_vterm.assert_called_once_with(self.apt, 'pvm_uuid')
        self.apt.delete.assert_called_once_with('LogicalPartition',
                                                root_id='pvm_uuid')

        # Test Failure Path
        # build a mock response body with the expected HSCL msg
        resp = mock.Mock(body='error msg: HSCL151B more text')
        self.apt.delete.side_effect = pvm_exc.Error(
            'Mock Error Message', response=resp)

        # Reset counters
        mock_pvm_uuid.reset_mock()
        self.apt.reset_mock()
        mock_vterm.reset_mock()

        self.assertRaises(pvm_exc.Error,
                          vm.delete_lpar, self.apt, 'inst')
        mock_pvm_uuid.assert_called_once_with('inst')
        mock_vterm.assert_called_once_with(self.apt, 'pvm_uuid')
        self.apt.delete.assert_called_once_with('LogicalPartition',
                                                root_id='pvm_uuid')

        # Test HttpNotFound - exception not raised
        mock_pvm_uuid.reset_mock()
        self.apt.reset_mock()
        mock_vterm.reset_mock()

        resp.status = 404
        self.apt.delete.side_effect = pvm_exc.HttpNotFound(resp=resp)
        vm.delete_lpar(self.apt, 'inst')
        mock_pvm_uuid.assert_called_once_with('inst')
        mock_vterm.assert_called_once_with(self.apt, 'pvm_uuid')
        self.apt.delete.assert_called_once_with('LogicalPartition',
                                                root_id='pvm_uuid')

        # Test Other HttpError
        mock_pvm_uuid.reset_mock()
        self.apt.reset_mock()
        mock_vterm.reset_mock()

        resp.status = 111
        self.apt.delete.side_effect = pvm_exc.HttpError(resp=resp)
        self.assertRaises(pvm_exc.HttpError, vm.delete_lpar, self.apt, 'inst')
        mock_pvm_uuid.assert_called_once_with('inst')
        mock_vterm.assert_called_once_with(self.apt, 'pvm_uuid')
        self.apt.delete.assert_called_once_with('LogicalPartition',
                                                root_id='pvm_uuid')

        # Test HttpNotFound closing vterm
        mock_pvm_uuid.reset_mock()
        self.apt.reset_mock()
        mock_vterm.reset_mock()

        resp.status = 404
        mock_vterm.side_effect = pvm_exc.HttpNotFound(resp=resp)
        vm.delete_lpar(self.apt, 'inst')
        mock_pvm_uuid.assert_called_once_with('inst')
        mock_vterm.assert_called_once_with(self.apt, 'pvm_uuid')
        self.apt.delete.assert_not_called()

        # Test Other HttpError closing vterm
        mock_pvm_uuid.reset_mock()
        self.apt.reset_mock()
        mock_vterm.reset_mock()

        resp.status = 111
        mock_vterm.side_effect = pvm_exc.HttpError(resp=resp)
        self.assertRaises(pvm_exc.HttpError, vm.delete_lpar, self.apt, 'inst')
        mock_pvm_uuid.assert_called_once_with('inst')
        mock_vterm.assert_called_once_with(self.apt, 'pvm_uuid')
        self.apt.delete.assert_not_called()

    @mock.patch('nova_powervm.virt.powervm.vm.VMBuilder._add_IBMi_attrs',
                autospec=True)
    @mock.patch('pypowervm.utils.lpar_builder.DefaultStandardize',
                autospec=True)
    @mock.patch('pypowervm.utils.lpar_builder.LPARBuilder.build',
                autospec=True)
    @mock.patch('pypowervm.utils.validation.LPARWrapperValidator.validate_all',
                autospec=True)
    def test_crt_lpar(self, mock_vld_all, mock_bld, mock_stdz, mock_ibmi):
        instance = objects.Instance(**powervm.TEST_INSTANCE)
        flavor = instance.get_flavor()
        flavor.extra_specs = {'powervm:dedicated_proc': 'true'}

        host_wrapper = mock.Mock()
        lparw = pvm_lpar.LPAR.wrap(self.resp.feed.entries[0])
        mock_bld.return_value = lparw
        self.apt.create.return_value = lparw.entry
        vm.create_lpar(self.apt, host_wrapper, instance, nvram='data')
        self.apt.create.assert_called_once_with(
            lparw, host_wrapper.schema_type, child_type='LogicalPartition',
            root_id=host_wrapper.uuid, service='uom', timeout=-1)
        mock_stdz.assert_called_once_with(host_wrapper, uncapped_weight=64,
                                          proc_units_factor=0.1)
        self.assertEqual(lparw.nvram, 'data')
        self.assertTrue(mock_vld_all.called)

        # Test srr and slot_mgr
        self.apt.reset_mock()
        mock_vld_all.reset_mock()
        mock_stdz.reset_mock()
        flavor.extra_specs = {'powervm:srr_capability': 'true'}
        self.apt.create.return_value = lparw.entry
        mock_slot_mgr = mock.Mock(build_map=mock.Mock(
            get_max_vslots=mock.Mock(return_value=123)))
        vm.create_lpar(self.apt, host_wrapper, instance,
                       slot_mgr=mock_slot_mgr)
        self.assertTrue(self.apt.create.called)
        self.assertTrue(mock_vld_all.called)
        self.assertTrue(lparw.srr_enabled)
        mock_stdz.assert_called_once_with(host_wrapper, uncapped_weight=64,
                                          proc_units_factor=0.1, max_slots=123)
        # The save is called with the LPAR's actual value, which in this mock
        # setup comes from lparw
        mock_slot_mgr.register_max_vslots.assert_called_with(
            lparw.io_config.max_virtual_slots)

        # Test to verify the LPAR Creation with invalid name specification
        mock_bld.side_effect = lpar_bld.LPARBuilderException("Invalid Name")
        host_wrapper = mock.Mock()
        self.assertRaises(exception.BuildAbortException, vm.create_lpar,
                          self.apt, host_wrapper, instance)

        resp = mock.Mock(status=202, method='fake', path='/dev/',
                         reason='Failure')
        mock_bld.side_effect = pvm_exc.HttpError(resp)
        try:
            vm.create_lpar(self.apt, host_wrapper, instance)
        except nvex.PowerVMAPIFailed as e:
            self.assertEqual(e.kwargs['inst_name'], instance.name)
            self.assertEqual(e.kwargs['reason'], mock_bld.side_effect)
        flavor.extra_specs = {'powervm:BADATTR': 'true'}
        host_wrapper = mock.Mock()
        self.assertRaises(exception.InvalidAttribute, vm.create_lpar,
                          self.apt, host_wrapper, instance)

    @mock.patch('pypowervm.wrappers.logical_partition.LPAR.get')
    def test_get_instance_wrapper(self, mock_get):
        mock_get.side_effect = pvm_exc.HttpNotFound(resp=mock.Mock(status=404))
        instance = objects.Instance(**powervm.TEST_INSTANCE)
        # vm.get_instance_wrapper(self.apt, instance, 'lpar_uuid')
        self.assertRaises(exception.InstanceNotFound, vm.get_instance_wrapper,
                          self.apt, instance, 'lpar_uuid')

    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper',
                autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.VMBuilder', autospec=True)
    def test_update(self, mock_vmb, mock_get_inst):
        instance = objects.Instance(**powervm.TEST_INSTANCE)
        entry = mock.Mock()
        name = "new_name"
        entry.update.return_value = 'NewEntry'
        bldr = mock_vmb.return_value
        lpar_bldr = bldr.lpar_builder.return_value
        new_entry = vm.update(self.apt, 'mock_host_wrap', instance,
                              entry=entry, name=name)
        # Ensure the lpar was rebuilt
        lpar_bldr.rebuild.assert_called_once_with(entry)
        entry.update.assert_called_once_with()
        self.assertEqual(name, entry.name)
        self.assertEqual('NewEntry', new_entry)
        self.san_lpar_name.assert_called_with(name)

    @mock.patch('pypowervm.utils.transaction.entry_transaction', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper',
                autospec=True)
    def test_rename(self, mock_get_inst, mock_entry_transaction):
        instance = objects.Instance(**powervm.TEST_INSTANCE)

        mock_entry_transaction.side_effect = lambda x: x

        entry = mock.Mock()
        entry.update.return_value = 'NewEntry'
        new_entry = vm.rename(self.apt, instance, 'new_name', entry=entry)
        self.assertEqual('new_name', entry.name)
        entry.update.assert_called_once_with()
        mock_entry_transaction.assert_called_once_with(mock.ANY)
        self.assertEqual('NewEntry', new_entry)
        self.san_lpar_name.assert_called_with('new_name')
        self.san_lpar_name.reset_mock()

        # Test optional entry parameter
        entry.reset_mock()
        mock_get_inst.return_value = entry
        new_entry = vm.rename(self.apt, instance, 'new_name')
        mock_get_inst.assert_called_once_with(self.apt, instance)
        self.assertEqual('new_name', entry.name)
        entry.update.assert_called_once_with()
        self.assertEqual('NewEntry', new_entry)
        self.san_lpar_name.assert_called_with('new_name')

    def test_add_IBMi_attrs(self):
        inst = mock.Mock()
        # Non-ibmi distro
        attrs = {}
        inst.system_metadata = {'image_os_distro': 'rhel'}
        bldr = vm.VMBuilder(mock.Mock(), mock.Mock())
        bldr._add_IBMi_attrs(inst, attrs)
        self.assertDictEqual(attrs, {})

        inst.system_metadata = {}
        bldr._add_IBMi_attrs(inst, attrs)
        self.assertDictEqual(attrs, {})

        # ibmi distro
        inst.system_metadata = {'image_os_distro': 'ibmi'}
        bldr._add_IBMi_attrs(inst, attrs)
        self.assertDictEqual(attrs, {'env': 'OS400'})

    @mock.patch('pypowervm.tasks.power.power_on', autospec=True)
    @mock.patch('oslo_concurrency.lockutils.lock', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper',
                autospec=True)
    def test_power_on(self, mock_wrap, mock_lock, mock_power_on):
        instance = objects.Instance(**powervm.TEST_INSTANCE)
        entry = mock.Mock(state=pvm_bp.LPARState.NOT_ACTIVATED)
        mock_wrap.return_value = entry

        self.assertTrue(vm.power_on(None, instance, opts='opts'))
        mock_power_on.assert_called_once_with(entry, None, add_parms='opts')
        mock_lock.assert_called_once_with('power_%s' % instance.uuid)

        mock_power_on.reset_mock()
        mock_lock.reset_mock()

        stop_states = [
            pvm_bp.LPARState.RUNNING, pvm_bp.LPARState.STARTING,
            pvm_bp.LPARState.OPEN_FIRMWARE, pvm_bp.LPARState.SHUTTING_DOWN,
            pvm_bp.LPARState.ERROR, pvm_bp.LPARState.RESUMING,
            pvm_bp.LPARState.SUSPENDING]

        for stop_state in stop_states:
            entry.state = stop_state
            self.assertFalse(vm.power_on(None, instance))
            self.assertEqual(0, mock_power_on.call_count)
            mock_lock.assert_called_once_with('power_%s' % instance.uuid)
            mock_lock.reset_mock()

    @mock.patch('pypowervm.tasks.power.PowerOp', autospec=True)
    @mock.patch('pypowervm.tasks.power.power_off_progressive', autospec=True)
    @mock.patch('oslo_concurrency.lockutils.lock', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper',
                autospec=True)
    def test_power_off(self, mock_wrap, mock_lock, mock_power_off, mock_pop):
        instance = objects.Instance(**powervm.TEST_INSTANCE)
        entry = mock.Mock(state=pvm_bp.LPARState.NOT_ACTIVATED)
        mock_wrap.return_value = entry

        self.assertFalse(vm.power_off(None, instance))
        self.assertEqual(0, mock_power_off.call_count)
        self.assertEqual(0, mock_pop.stop.call_count)
        mock_lock.assert_called_once_with('power_%s' % instance.uuid)

        stop_states = [
            pvm_bp.LPARState.RUNNING, pvm_bp.LPARState.STARTING,
            pvm_bp.LPARState.OPEN_FIRMWARE, pvm_bp.LPARState.SHUTTING_DOWN,
            pvm_bp.LPARState.ERROR, pvm_bp.LPARState.RESUMING,
            pvm_bp.LPARState.SUSPENDING]
        for stop_state in stop_states:
            entry.state = stop_state
            mock_power_off.reset_mock()
            mock_pop.stop.reset_mock()
            mock_lock.reset_mock()
            self.assertTrue(vm.power_off(None, instance))
            mock_power_off.assert_called_once_with(entry)
            self.assertEqual(0, mock_pop.stop.call_count)
            mock_lock.assert_called_once_with('power_%s' % instance.uuid)
            mock_power_off.reset_mock()
            mock_lock.reset_mock()
            self.assertTrue(vm.power_off(
                None, instance, force_immediate=True, timeout=5))
            self.assertEqual(0, mock_power_off.call_count)
            mock_pop.stop.assert_called_once_with(
                entry, opts=mock.ANY, timeout=5)
            self.assertEqual('PowerOff(immediate=true, operation=shutdown)',
                             str(mock_pop.stop.call_args[1]['opts']))
            mock_lock.assert_called_once_with('power_%s' % instance.uuid)

    @mock.patch('pypowervm.tasks.power.power_off_progressive', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper',
                autospec=True)
    def test_power_off_negative(self, mock_wrap, mock_power_off):
        """Negative tests."""
        instance = objects.Instance(**powervm.TEST_INSTANCE)
        mock_wrap.return_value = mock.Mock(state=pvm_bp.LPARState.RUNNING)

        # Raise the expected pypowervm exception
        mock_power_off.side_effect = pvm_exc.VMPowerOffFailure(
            reason='Something bad.', lpar_nm='TheLPAR')
        # We should get a valid Nova exception that the compute manager expects
        self.assertRaises(exception.InstancePowerOffFailure,
                          vm.power_off, None, instance)

    @mock.patch('oslo_concurrency.lockutils.lock', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper',
                autospec=True)
    @mock.patch('pypowervm.tasks.power.power_on', autospec=True)
    @mock.patch('pypowervm.tasks.power.power_off_progressive', autospec=True)
    @mock.patch('pypowervm.tasks.power.PowerOp', autospec=True)
    def test_reboot(self, mock_pop, mock_pwroff, mock_pwron, mock_giw,
                    mock_lock):
        entry = mock.Mock()
        inst = mock.Mock(uuid='uuid')
        mock_giw.return_value = entry

        # VM is in 'not activated' state
        entry.state = pvm_bp.LPARState.NOT_ACTIVATED
        vm.reboot('adapter', inst, True)
        mock_pwron.assert_called_once_with(entry, None)
        self.assertEqual(0, mock_pwroff.call_count)
        self.assertEqual(0, mock_pop.stop.call_count)
        mock_lock.assert_called_once_with('power_uuid')

        mock_pwron.reset_mock()
        mock_lock.reset_mock()

        # VM is in an active state
        entry.state = pvm_bp.LPARState.RUNNING
        vm.reboot('adapter', inst, True)
        self.assertEqual(0, mock_pwron.call_count)
        self.assertEqual(0, mock_pwroff.call_count)
        mock_pop.stop.assert_called_once_with(entry, opts=mock.ANY)
        self.assertEqual(
            'PowerOff(immediate=true, operation=shutdown, restart=true)',
            str(mock_pop.stop.call_args[1]['opts']))
        mock_lock.assert_called_once_with('power_uuid')

        mock_pop.stop.reset_mock()
        mock_lock.reset_mock()

        # Same, but soft
        vm.reboot('adapter', inst, False)
        self.assertEqual(0, mock_pwron.call_count)
        mock_pwroff.assert_called_once_with(entry, restart=True)
        self.assertEqual(0, mock_pop.stop.call_count)
        mock_lock.assert_called_once_with('power_uuid')

        mock_pwroff.reset_mock()
        mock_lock.reset_mock()

        # Exception path
        mock_pwroff.side_effect = Exception()
        self.assertRaises(exception.InstanceRebootFailure, vm.reboot,
                          'adapter', inst, False)
        self.assertEqual(0, mock_pwron.call_count)
        mock_pwroff.assert_called_once_with(entry, restart=True)
        self.assertEqual(0, mock_pop.stop.call_count)
        mock_lock.assert_called_once_with('power_uuid')

    def test_get_pvm_uuid(self):

        nova_uuid = "dbbb48f1-2406-4019-98af-1c16d3df0204"
        # Test with uuid string
        self.assertEqual('5BBB48F1-2406-4019-98AF-1C16D3DF0204',
                         vm.get_pvm_uuid(nova_uuid))

        mock_inst = mock.Mock(uuid=nova_uuid)
        # Test with instance object
        self.assertEqual('5BBB48F1-2406-4019-98AF-1C16D3DF0204',
                         vm.get_pvm_uuid(mock_inst))

    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_qp', autospec=True)
    def test_instance_exists(self, mock_getvmqp, mock_getuuid):
        # Try the good case where it exists
        mock_getvmqp.side_effect = 'fake_state'
        mock_parms = (mock.Mock(), mock.Mock())
        self.assertTrue(vm.instance_exists(*mock_parms))

        # Test the scenario where it does not exist.
        mock_getvmqp.side_effect = exception.InstanceNotFound(instance_id=123)
        self.assertFalse(vm.instance_exists(*mock_parms))

    def test_get_vm_qp(self):
        def adapter_read(root_type, root_id=None, suffix_type=None,
                         suffix_parm=None, helpers=None):
            json_str = (u'{"IsVirtualServiceAttentionLEDOn":"false","Migration'
                        u'State":"Not_Migrating","CurrentProcessingUnits":0.1,'
                        u'"ProgressState":null,"PartitionType":"AIX/Linux","Pa'
                        u'rtitionID":1,"AllocatedVirtualProcessors":1,"Partiti'
                        u'onState":"not activated","RemoteRestartState":"Inval'
                        u'id","OperatingSystemVersion":"Unknown","AssociatedMa'
                        u'nagedSystem":"https://9.1.2.3:12443/rest/api/uom/Man'
                        u'agedSystem/98498bed-c78a-3a4f-b90a-4b715418fcb6","RM'
                        u'CState":"inactive","PowerManagementMode":null,"Parti'
                        u'tionName":"lpar-1-06674231-lpar","HasDedicatedProces'
                        u'sors":"false","ResourceMonitoringIPAddress":null,"Re'
                        u'ferenceCode":"00000000","CurrentProcessors":null,"Cu'
                        u'rrentMemory":512,"SharingMode":"uncapped"}')
            self.assertEqual('LogicalPartition', root_type)
            self.assertEqual('lpar_uuid', root_id)
            self.assertEqual('quick', suffix_type)
            resp = mock.MagicMock()
            if suffix_parm is None:
                resp.body = json_str
            elif suffix_parm == 'PartitionID':
                resp.body = '1'
            elif suffix_parm == 'CurrentProcessingUnits':
                resp.body = '0.1'
            elif suffix_parm == 'AssociatedManagedSystem':
                # The double quotes are important
                resp.body = ('"https://9.1.2.3:12443/rest/api/uom/ManagedSyste'
                             'm/98498bed-c78a-3a4f-b90a-4b715418fcb6"')
            else:
                self.fail('Unhandled quick property key %s' % suffix_parm)
            return resp

        def adpt_read_no_log(*args, **kwds):
            helpers = kwds['helpers']
            try:
                helpers.index(pvm_log.log_helper)
            except ValueError:
                # Successful path since the logger shouldn't be there
                return adapter_read(*args, **kwds)

            self.fail('Log helper was found when it should not be')

        ms_href = ('https://9.1.2.3:12443/rest/api/uom/ManagedSystem/98498bed-'
                   'c78a-3a4f-b90a-4b715418fcb6')
        self.apt.read.side_effect = adapter_read
        self.assertEqual(1, vm.get_vm_id(self.apt, 'lpar_uuid'))
        self.assertEqual(ms_href, vm.get_vm_qp(self.apt, 'lpar_uuid',
                                               'AssociatedManagedSystem'))
        self.apt.read.side_effect = adpt_read_no_log
        self.assertEqual(0.1, vm.get_vm_qp(self.apt, 'lpar_uuid',
                                           'CurrentProcessingUnits',
                                           log_errors=False))
        qp_dict = vm.get_vm_qp(self.apt, 'lpar_uuid', log_errors=False)
        self.assertEqual(ms_href, qp_dict['AssociatedManagedSystem'])
        self.assertEqual(1, qp_dict['PartitionID'])
        self.assertEqual(0.1, qp_dict['CurrentProcessingUnits'])

        resp = mock.MagicMock()
        resp.status = 404
        self.apt.read.side_effect = pvm_exc.HttpNotFound(resp)
        self.assertRaises(exception.InstanceNotFound, vm.get_vm_qp, self.apt,
                          'lpar_uuid', log_errors=False)

        self.apt.read.side_effect = pvm_exc.Error("message", response=None)
        self.assertRaises(pvm_exc.Error, vm.get_vm_qp, self.apt,
                          'lpar_uuid', log_errors=False)

        resp.status = 500
        self.apt.read.side_effect = pvm_exc.Error("message", response=resp)
        self.assertRaises(pvm_exc.Error, vm.get_vm_qp, self.apt,
                          'lpar_uuid', log_errors=False)

    def test_norm_mac(self):
        EXPECTED = "12:34:56:78:90:ab"
        self.assertEqual(EXPECTED, vm.norm_mac("12:34:56:78:90:ab"))
        self.assertEqual(EXPECTED, vm.norm_mac("1234567890ab"))
        self.assertEqual(EXPECTED, vm.norm_mac("12:34:56:78:90:AB"))
        self.assertEqual(EXPECTED, vm.norm_mac("1234567890AB"))

    @mock.patch('pypowervm.tasks.ibmi.update_ibmi_settings', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper',
                autospec=True)
    def test_update_ibmi_settings(self, mock_lparw, mock_ibmi):
        instance = mock.MagicMock()

        # Test update load source with vscsi boot
        boot_type = 'vscsi'
        vm.update_ibmi_settings(self.apt, instance, boot_type)
        mock_ibmi.assert_called_once_with(self.apt, mock.ANY, 'vscsi')
        mock_ibmi.reset_mock()

        # Test update load source with npiv boot
        boot_type = 'npiv'
        vm.update_ibmi_settings(self.apt, instance, boot_type)
        mock_ibmi.assert_called_once_with(self.apt, mock.ANY, 'npiv')

    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('pypowervm.wrappers.network.CNA.search')
    @mock.patch('pypowervm.wrappers.network.CNA.get')
    def test_get_cnas(self, mock_get, mock_search, mock_uuid):
        # No kwargs: get
        self.assertEqual(mock_get.return_value, vm.get_cnas(self.apt, 'inst'))
        mock_uuid.assert_called_once_with('inst')
        mock_get.assert_called_once_with(self.apt, parent_type=pvm_lpar.LPAR,
                                         parent_uuid=mock_uuid.return_value)
        mock_search.assert_not_called()
        # With kwargs: search
        mock_get.reset_mock()
        mock_uuid.reset_mock()
        self.assertEqual(mock_search.return_value, vm.get_cnas(
            self.apt, 'inst', one=2, three=4))
        mock_uuid.assert_called_once_with('inst')
        mock_search.assert_called_once_with(
            self.apt, parent_type=pvm_lpar.LPAR,
            parent_uuid=mock_uuid.return_value, one=2, three=4)
        mock_get.assert_not_called()

    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('pypowervm.wrappers.iocard.VNIC.search')
    @mock.patch('pypowervm.wrappers.iocard.VNIC.get')
    def test_get_vnics(self, mock_get, mock_search, mock_uuid):
        # No kwargs: get
        self.assertEqual(mock_get.return_value, vm.get_vnics(self.apt, 'inst'))
        mock_uuid.assert_called_once_with('inst')
        mock_get.assert_called_once_with(self.apt, parent_type=pvm_lpar.LPAR,
                                         parent_uuid=mock_uuid.return_value)
        mock_search.assert_not_called()
        # With kwargs: search
        mock_get.reset_mock()
        mock_uuid.reset_mock()
        self.assertEqual(mock_search.return_value, vm.get_vnics(
            self.apt, 'inst', one=2, three=4))
        mock_uuid.assert_called_once_with('inst')
        mock_search.assert_called_once_with(
            self.apt, parent_type=pvm_lpar.LPAR,
            parent_uuid=mock_uuid.return_value, one=2, three=4)
        mock_get.assert_not_called()
