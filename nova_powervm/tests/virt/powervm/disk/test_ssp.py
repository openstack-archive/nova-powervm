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

import copy

import fixtures
import mock
from oslo_config import cfg

from nova import test
import os
import pypowervm.adapter as pvm_adp
import pypowervm.entities as pvm_ent
from pypowervm.tests.wrappers.util import pvmhttp
from pypowervm.wrappers import cluster as pvm_clust
from pypowervm.wrappers import storage as pvm_stg
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm.disk import driver
from nova_powervm.virt.powervm.disk import ssp


SSP = 'fake_ssp.txt'
CLUST = 'fake_cluster.txt'


class SSPFixture(fixtures.Fixture):
    """Patch out PyPowerVM SSP EntryWrapper search and refresh."""

    def __init__(self):
        pass

    def setUp(self):
        super(SSPFixture, self).setUp()
        self._search_patcher = mock.patch(
            'pypowervm.wrappers.cluster.Cluster.search')
        self.mock_search = self._search_patcher.start()
        self.addCleanup(self._search_patcher.stop)

        self._clust_refresh_patcher = mock.patch(
            'pypowervm.wrappers.cluster.Cluster.refresh')
        self.mock_clust_refresh = self._clust_refresh_patcher.start()
        self.addCleanup(self._clust_refresh_patcher.stop)

        self._ssp_refresh_patcher = mock.patch(
            'pypowervm.wrappers.storage.SSP.refresh')
        self.mock_ssp_refresh = self._ssp_refresh_patcher.start()
        self.addCleanup(self._ssp_refresh_patcher.stop)


class TestSSPDiskAdapter(test.TestCase):
    """Unit Tests for the LocalDisk storage driver."""

    def setUp(self):
        super(TestSSPDiskAdapter, self).setUp()

        class Instance(object):
            uuid = 'instance-uuid'
            name = 'instance-name'

        self.instance = Instance()

        # Find directory for response file(s)
        data_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(data_dir, "..", 'data')

        self.pypvm = self.useFixture(fx.PyPowerVM())
        self.apt = self.pypvm.apt

        def resp(file_name):
            file_path = os.path.join(data_dir, file_name)
            return pvmhttp.load_pvm_resp(
                file_path, adapter=self.apt).get_response()

        self.ssp_resp = resp(SSP)
        self.clust_resp = resp(CLUST)

        self.sspfx = self.useFixture(SSPFixture())

        # For _fetch_cluster() with configured name
        self.mock_search = self.sspfx.mock_search
        # EntryWrapper.search always returns a feed.
        self.mock_search.return_value = self._bld_resp(
            entry_or_list=[self.clust_resp.entry])

        # For _refresh_cluster()
        self.mock_clust_refresh = self.sspfx.mock_clust_refresh
        self.mock_clust_refresh.return_value = pvm_clust.Cluster.wrap(
            self.clust_resp)

        # For _fetch_ssp() fresh
        self.apt.read_by_href.return_value = self.ssp_resp

        # For _fetch_ssp() refresh
        self.mock_ssp_refresh = self.sspfx.mock_ssp_refresh
        self.mock_ssp_refresh.return_value = pvm_stg.SSP.wrap(self.ssp_resp)

        # By default, assume the config supplied a Cluster name
        cfg.CONF.set_override('cluster_name', 'clust1')

    def _get_ssp_stor(self):
        ssp_stor = ssp.SSPDiskAdapter(
            {'adapter': self.apt,
             'host_uuid': '67dca605-3923-34da-bd8f-26a378fc817f',
             'mp_uuid': 'mp_uuid'})
        return ssp_stor

    def _bld_resp(self, status=200, entry_or_list=None):
        """Build a pypowervm.adapter.Response for mocking Adapter.search/read.

        :param status: HTTP status of the Response.
        :param entry_or_list: pypowervm.entity.Entry or list thereof.  If
                              None, the Response has no content.  If an Entry,
                              the Response looks like it got back <entry/>.  If
                              a list of Entry, the Response looks like it got
                              back <feed/>.
        :return: pypowervm.adapter.Response suitable for mocking
                 pypowervm.adapter.Adapter.search or read.
        """
        resp = pvm_adp.Response('meth', 'path', status, 'reason', {})
        resp.entry = None
        resp.feed = None
        if entry_or_list is None:
            resp.feed = pvm_ent.Feed({}, [])
        else:
            if isinstance(entry_or_list, list):
                resp.feed = pvm_ent.Feed({}, entry_or_list)
            else:
                resp.entry = entry_or_list
        return resp

    def test_init_green_with_config(self):
        """Bootstrap SSPStorage, testing call to _fetch_cluster.

        Driver init should search for cluster by name.
        """
        # Invoke __init__ => _fetch_cluster()
        self._get_ssp_stor()
        # _fetch_cluster() WITH configured name does a search, but not a read.
        # Refresh shouldn't be invoked.
        self.assertEqual(1, self.mock_search.call_count)
        self.assertEqual(0, self.apt.read.call_count)
        self.assertEqual(0, self.mock_clust_refresh.call_count)

    def test_init_green_no_config(self):
        """No cluster name specified in config; one cluster on host - ok."""
        cfg.CONF.clear_override('cluster_name')
        self.apt.read.return_value = self._bld_resp(
            entry_or_list=[self.clust_resp.entry])
        self._get_ssp_stor()
        # _fetch_cluster() WITHOUT a configured name does a feed GET (read),
        # not a search.
        # Refresh shouldn't be invoked.
        self.assertEqual(0, self.mock_search.call_count)
        self.assertEqual(1, self.apt.read.call_count)
        self.assertEqual(0, self.mock_clust_refresh.call_count)
        self.apt.read.assert_called_with('Cluster')

    def test_init_ClusterNotFoundByName(self):
        """Empty feed comes back from search - no cluster by that name."""
        self.mock_search.return_value = self._bld_resp(status=204)
        self.assertRaises(ssp.ClusterNotFoundByName, self._get_ssp_stor)

    def test_init_TooManyClustersFound(self):
        """Search-by-name returns more than one result."""
        clust1 = pvm_clust.Cluster.bld(None, 'newclust1',
                                       pvm_stg.PV.bld(None, 'hdisk1'),
                                       pvm_clust.Node.bld('vios1'))
        clust2 = pvm_clust.Cluster.bld(None, 'newclust2',
                                       pvm_stg.PV.bld(None, 'hdisk2'),
                                       pvm_clust.Node.bld(None, 'vios2'))
        self.mock_search.return_value = self._bld_resp(
            entry_or_list=[clust1.entry, clust2.entry])
        self.assertRaises(ssp.TooManyClustersFound, self._get_ssp_stor)

    def test_init_NoConfigNoClusterFound(self):
        """No cluster name specified in config, no clusters on host."""
        cfg.CONF.clear_override('cluster_name')
        self.apt.read.return_value = self._bld_resp(status=204)
        self.assertRaises(ssp.NoConfigNoClusterFound, self._get_ssp_stor)

    def test_init_NoConfigTooManyClusters(self):
        """No SSP name specified in config, more than one SSP on host."""
        cfg.CONF.clear_override('cluster_name')
        clust1 = pvm_clust.Cluster.bld(None, 'newclust1',
                                       pvm_stg.PV.bld(None, 'hdisk1'),
                                       pvm_clust.Node.bld(None, 'vios1'))
        clust2 = pvm_clust.Cluster.bld(None, 'newclust2',
                                       pvm_stg.PV.bld(None, 'hdisk2'),
                                       pvm_clust.Node.bld(None, 'vios2'))
        self.apt.read.return_value = self._bld_resp(
            entry_or_list=[clust1.entry, clust2.entry])
        self.assertRaises(ssp.NoConfigTooManyClusters, self._get_ssp_stor)

    def test_refresh_cluster(self):
        """_refresh_cluster with cached wrapper."""
        # Save original cluster wrapper for later comparison
        orig_clust_wrap = pvm_clust.Cluster.wrap(self.clust_resp)
        # Prime _clust_wrap
        ssp_stor = self._get_ssp_stor()
        # Verify baseline call counts
        self.assertEqual(1, self.mock_search.call_count)
        self.assertEqual(0, self.mock_clust_refresh.call_count)
        clust_wrap = ssp_stor._refresh_cluster()
        # This should call refresh
        self.assertEqual(1, self.mock_search.call_count)
        self.assertEqual(0, self.apt.read.call_count)
        self.assertEqual(1, self.mock_clust_refresh.call_count)
        self.assertEqual(clust_wrap.name, orig_clust_wrap.name)

    def test_fetch_ssp(self):
        # For later comparison
        orig_ssp_wrap = pvm_stg.SSP.wrap(self.ssp_resp)
        # Verify baseline call counts
        self.assertEqual(0, self.apt.read_by_href.call_count)
        self.assertEqual(0, self.mock_ssp_refresh.call_count)
        # This should prime self._ssp_wrap: calls read_by_href but not refresh.
        ssp_stor = self._get_ssp_stor()
        self.assertEqual(1, self.apt.read_by_href.call_count)
        self.assertEqual(0, self.mock_ssp_refresh.call_count)
        # Accessing the @property will trigger refresh
        ssp_wrap = ssp_stor._ssp
        self.assertEqual(1, self.apt.read_by_href.call_count)
        self.assertEqual(1, self.mock_ssp_refresh.call_count)
        self.assertEqual(ssp_wrap.name, orig_ssp_wrap.name)

    def test_vios_uuids(self):
        ssp_stor = self._get_ssp_stor()
        vios_uuids = ssp_stor.vios_uuids
        self.assertEqual({'10B06F4B-437D-9C18-CA95-34006424120D',
                          '6424120D-CA95-437D-9C18-10B06F4B3400'},
                         set(vios_uuids))
        s = set()
        for i in range(1000):
            u = ssp_stor._any_vios_uuid()
            # Make sure we got a good value
            self.assertIn(u, vios_uuids)
            s.add(u)
        # Make sure we hit all the values over 1000 iterations.  This isn't
        # guaranteed to work, but the odds of failure should be infinitesimal.
        self.assertEqual(set(vios_uuids), s)

        # Test VIOSes on other nodes, which won't have uuid or url
        with mock.patch.object(ssp_stor, '_cluster') as mock_clust:
            def mock_node(uuid, uri):
                node = mock.MagicMock()
                node.vios_uuid = uuid
                node.vios_uri = uri
                return node
            uri = ('https://9.1.2.3:12443/rest/api/uom/ManagedSystem/67dca605-'
                   '3923-34da-bd8f-26a378fc817f/VirtualIOServer/10B06F4B-437D-'
                   '9C18-CA95-34006424120D')
            node1 = mock_node(None, uri)
            node2 = mock_node('2', None)
            # This mock is good and should be returned
            node3 = mock_node('3', uri)
            mock_clust.nodes = [node1, node2, node3]
            self.assertEqual(['3'], ssp_stor.vios_uuids)

    def test_capacity(self):
        ssp_stor = self._get_ssp_stor()
        self.assertEqual(49.88, ssp_stor.capacity)

    def test_capacity_used(self):
        ssp_stor = self._get_ssp_stor()
        self.assertEqual((49.88 - 48.98), ssp_stor.capacity_used)

    @mock.patch('pypowervm.tasks.storage.crt_lu_linked_clone')
    @mock.patch('pypowervm.tasks.storage.upload_new_lu')
    @mock.patch('nova_powervm.virt.powervm.disk.driver.IterableToFileAdapter')
    @mock.patch('nova.image.API')
    def test_create_disk_from_new_image(self, mock_img_api, mock_it2fadp,
                                        mock_upload_lu, mock_crt_lnk_cln):
        b1G = 1024 * 1024 * 1024
        b2G = 2 * b1G
        ssp_stor = self._get_ssp_stor()
        img = dict(name='image-name', id='image-id', size=b2G)

        def verify_upload_new_lu(vios_uuid, ssp1, stream, lu_name, f_size):
            self.assertIn(vios_uuid, ssp_stor.vios_uuids)
            self.assertEqual(ssp_stor._ssp_wrap, ssp1)
            # 'image' + '_' + s/-/_/g(image['id']), per _get_image_name
            self.assertEqual('image_image_name', lu_name)
            self.assertEqual(b2G, f_size)
            return 'image_lu', None

        def verify_create_lu_linked_clone(ssp1, clust1, imglu, lu_name, sz_gb):
            # 'boot_' + sanitize('instance-name') _get_disk_name
            self.assertEqual('boot_instance_name', lu_name)
            self.assertEqual('image_lu', imglu)
            return ssp1, 'new_lu'

        mock_upload_lu.side_effect = verify_upload_new_lu
        mock_crt_lnk_cln.side_effect = verify_create_lu_linked_clone
        lu = ssp_stor.create_disk_from_image(None, self.instance, img, 1)
        self.assertEqual('new_lu', lu)

    @mock.patch('pypowervm.tasks.storage.crt_lu_linked_clone')
    @mock.patch('nova_powervm.virt.powervm.disk.driver.IterableToFileAdapter')
    @mock.patch('nova.image.API')
    def test_create_disk_from_existing_image(self, mock_img_api, mock_it2fadp,
                                             mock_crt_lnk_cln):
        b1G = 1024 * 1024 * 1024
        b2G = 2 * b1G
        ssp_stor = self._get_ssp_stor()
        img = dict(name='image-name', id='image-id', size=b2G)
        # Mock the 'existing' image LU
        img_lu = pvm_stg.LU.bld(None, 'image_image_name', 123,
                                typ=pvm_stg.LUType.IMAGE)
        ssp_stor._ssp_wrap.logical_units.append(img_lu)

        class Instance(object):
            uuid = 'instance-uuid'
            name = 'instance-name'

        def verify_create_lu_linked_clone(ssp1, clust1, imglu, lu_name, sz_gb):
            # 'boot_' + sanitize('instance-name') per _get_disk_name
            self.assertEqual('boot_instance_name', lu_name)
            self.assertEqual(img_lu, imglu)
            return ssp1, 'new_lu'

        mock_crt_lnk_cln.side_effect = verify_create_lu_linked_clone
        lu = ssp_stor.create_disk_from_image(None, Instance(), img, 1)
        self.assertEqual('new_lu', lu)

    @mock.patch('pypowervm.wrappers.virtual_io_server.VSCSIMapping.'
                '_client_lpar_href')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_vscsi_mapping')
    def test_connect_disk(self, mock_add_map, mock_href):
        ms_uuid = '67dca605-3923-34da-bd8f-26a378fc817f'

        def validate_add_vscsi_mapping(host_uuid, vios_uuid, lpar_uuid, inlu):
            self.assertEqual(ms_uuid, host_uuid)
            self.assertIn(vios_uuid, ('6424120D-CA95-437D-9C18-10B06F4B3400',
                                      '10B06F4B-437D-9C18-CA95-34006424120D'))
            self.assertEqual('lpar_uuid', lpar_uuid)
            self.assertEqual(lu, inlu)
        mock_add_map.side_effect = validate_add_vscsi_mapping

        ssp_stor = self._get_ssp_stor()
        lu = ssp_stor._ssp_wrap.logical_units[0]
        ssp_stor.connect_disk(None, self.instance, lu, 'lpar_uuid')
        self.assertEqual(2, mock_add_map.call_count)

    def test_delete_disks(self):
        def _mk_img_lu(idx):
            lu = pvm_stg.LU.bld(None, 'img_lu%d' % idx, 123,
                                typ=pvm_stg.LUType.IMAGE)
            lu._udid('xxImage-LU-UDID-%d' % idx)
            return lu

        def _mk_dsk_lu(idx, cloned_from_idx):
            lu = pvm_stg.LU.bld(None, 'dsk_lu%d' % idx, 123,
                                typ=pvm_stg.LUType.DISK)
            lu._udid('xxDisk-LU-UDID-%d' % idx)
            lu._cloned_from_udid('yyImage-LU-UDID-%d' % cloned_from_idx)
            return lu

        # We should be ignoring the return value from update - but it still
        # needs to be wrappable
        self.apt.update_by_path.return_value = pvm_stg.SSP.bld(
            self.apt, 'ssp', []).entry
        ssp_stor = self._get_ssp_stor()
        ssp1 = ssp_stor._ssp_wrap
        # Seed the SSP with three clones backed to two images:
        # img_lu1 => dsk_lu3, dsk_lu4
        img_lu1 = _mk_img_lu(1)
        dsk_lu3 = _mk_dsk_lu(3, 1)
        dsk_lu4 = _mk_dsk_lu(4, 1)
        # img_lu2 => dsk_lu5
        img_lu2 = _mk_img_lu(2)
        dsk_lu5 = _mk_dsk_lu(5, 2)
        ssp1.logical_units = [img_lu1, img_lu2, dsk_lu3, dsk_lu4, dsk_lu5]
        # We'll delete dsk_lu3 and dsk_lu5.  We expect img_lu2 to vanish too.
        ssp_stor.delete_disks(None, None, [dsk_lu3, dsk_lu5])
        self.assertSetEqual(
            {(lu.name, lu.udid) for lu in (img_lu1, dsk_lu4)},
            set([(lu.name, lu.udid) for lu in ssp1.logical_units]))
        # Update should have been called only once.
        self.assertEqual(1, self.apt.update_by_path.call_count)

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_lu_mapping')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_disconnect_image_disk(self, mock_vm_id, mock_rm_lu_map):
        ssp_stor = self._get_ssp_stor()
        mock_vm_id.return_value = 'lpar_id'

        def mklu(udid):
            lu = pvm_stg.LU.bld(None, 'lu_%s' % udid, 1)
            lu._udid('27%s' % udid)
            return lu

        lu1 = mklu('abc')
        lu2 = mklu('def')

        def remove_lu_mapping(adapter, vios_uuid, lpar_id, disk_prefixes=None):
            """Mock returning different sets of LUs for each VIOS."""
            self.assertEqual(adapter, self.apt)
            self.assertEqual('lpar_id', lpar_id)
            self.assertIn(vios_uuid, ('6424120D-CA95-437D-9C18-10B06F4B3400',
                                      '10B06F4B-437D-9C18-CA95-34006424120D'))
            return 'fake_vios', [lu1, lu2]

        mock_rm_lu_map.side_effect = remove_lu_mapping
        lu_list = ssp_stor.disconnect_image_disk(None, None, None)
        self.assertEqual({lu1, lu2}, set(lu_list))

    def test_shared_stg_calls(self):

        # Check the good paths
        ssp_stor = self._get_ssp_stor()
        data = ssp_stor.check_instance_shared_storage_local('context', 'inst')
        self.assertTrue(
            ssp_stor.check_instance_shared_storage_remote('context', data))
        ssp_stor.check_instance_shared_storage_cleanup('context', data)

        # Check bad paths...
        # No data
        self.assertFalse(
            ssp_stor.check_instance_shared_storage_remote('context', None))
        # Unexpected data format
        self.assertFalse(
            ssp_stor.check_instance_shared_storage_remote('context', 'bad'))
        # Good data, but not the same SSP uuid
        not_same = {'ssp_uuid': 'uuid value not the same'}
        self.assertFalse(
            ssp_stor.check_instance_shared_storage_remote('context', not_same))

    def _bld_mocks_for_instance_disk(self):
        inst = mock.Mock()
        inst.name = 'my-instance-name'
        lpar_wrap = mock.Mock()
        lpar_wrap.id = 4
        # Build mock VIOS Wrappers as the returns from VIOS.wrap.
        # vios1 and vios2 will both have the mapping for client ID 4 and LU
        # named boot_my_instance_name.
        vios1 = pvm_vios.VIOS.wrap(pvmhttp.load_pvm_resp(
            'fake_vios_ssp_npiv.txt', adapter=self.apt).get_response())
        vios1.scsi_mappings[3].backing_storage._name('boot_my_instance_name')
        resp1 = self._bld_resp(entry_or_list=vios1.entry)
        vios2 = copy.deepcopy(vios1)
        # Change name and UUID so we can tell the difference:
        vios2.name = 'vios2'
        vios2.entry.properties['id'] = '7C6475B3-73A5-B9FF-4799-B23B20292951'
        resp2 = self._bld_resp(entry_or_list=vios2.entry)
        # vios3 will not have the mapping
        vios3 = pvm_vios.VIOS.wrap(pvmhttp.load_pvm_resp(
            'fake_vios_ssp_npiv.txt', adapter=self.apt).get_response())
        vios3.name = 'vios3'
        vios3.entry.properties['id'] = 'B9FF4799-B23B-2029-2951-7C6475B373A5'
        resp3 = self._bld_resp(entry_or_list=vios3.entry)
        return inst, lpar_wrap, resp1, resp2, resp3

    def test_instance_disk_iter(self):
        def assert_read_calls(num):
            self.assertEqual(num, self.apt.read.call_count)
            self.apt.read.assert_has_calls(
                [mock.call(pvm_vios.VIOS.schema_type, root_id=mock.ANY,
                           xag=[pvm_vios.VIOS.xags.SCSI_MAPPING])
                 for i in range(num)])
        ssp_stor = self._get_ssp_stor()
        inst, lpar_wrap, rsp1, rsp2, rsp3 = self._bld_mocks_for_instance_disk()

        # Test with two VIOSes, both of which contain the mapping
        self.apt.read.side_effect = [rsp1, rsp2]
        count = 0
        for lu, vios in ssp_stor.instance_disk_iter(inst, lpar_wrap=lpar_wrap):
            count += 1
            self.assertEqual('274d7bb790666211e3bc1a00006cae8b01ac18997ab9bc23'
                             'fb24756e9713a93f90', lu.udid)
            self.assertEqual('vios1_181.68' if count == 1 else 'vios2',
                             vios.name)
        self.assertEqual(2, count)
        assert_read_calls(2)

        # Same, but prove that breaking out of the loop early avoids the second
        # Adapter.read call
        self.apt.reset_mock()
        self.apt.read.side_effect = [rsp1, rsp2]
        for lu, vios in ssp_stor.instance_disk_iter(inst, lpar_wrap=lpar_wrap):
            self.assertEqual('274d7bb790666211e3bc1a00006cae8b01ac18997ab9bc23'
                             'fb24756e9713a93f90', lu.udid)
            self.assertEqual('vios1_181.68', vios.name)
            break
        self.assertEqual(1, self.apt.read.call_count)
        assert_read_calls(1)

        # Now the first VIOS doesn't have the mapping, but the second does
        self.apt.reset_mock()
        self.apt.read.side_effect = [rsp3, rsp2]
        count = 0
        for lu, vios in ssp_stor.instance_disk_iter(inst, lpar_wrap=lpar_wrap):
            count += 1
            self.assertEqual('274d7bb790666211e3bc1a00006cae8b01ac18997ab9bc23'
                             'fb24756e9713a93f90', lu.udid)
            self.assertEqual('vios2', vios.name)
        self.assertEqual(1, count)
        assert_read_calls(2)

        # No hits
        self.apt.reset_mock()
        self.apt.read.side_effect = [rsp3, rsp3]
        for lu, vios in ssp_stor.instance_disk_iter(inst, lpar_wrap=lpar_wrap):
            self.fail()
        assert_read_calls(2)

    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_vscsi_mapping')
    def test_connect_instance_disk_to_mgmt(self, mock_add, mock_lw):
        ssp_stor = self._get_ssp_stor()
        inst, lpar_wrap, rsp1, rsp2, rsp3 = self._bld_mocks_for_instance_disk()
        mock_lw.return_value = lpar_wrap

        # Test with two VIOSes, both of which contain the mapping
        self.apt.read.side_effect = [rsp1, rsp2]
        lu, vios = ssp_stor.connect_instance_disk_to_mgmt(inst)
        self.assertEqual('274d7bb790666211e3bc1a00006cae8b01ac18997ab9bc23'
                         'fb24756e9713a93f90', lu.udid)
        # Should hit on the first VIOS
        self.assertIs(rsp1.entry, vios.entry)
        self.assertEqual(1, mock_add.call_count)
        mock_add.assert_called_with('67dca605-3923-34da-bd8f-26a378fc817f',
                                    vios, 'mp_uuid', lu)

        # Now the first VIOS doesn't have the mapping, but the second does
        mock_add.reset_mock()
        self.apt.read.side_effect = [rsp3, rsp2]
        lu, vios = ssp_stor.connect_instance_disk_to_mgmt(inst)
        self.assertEqual('274d7bb790666211e3bc1a00006cae8b01ac18997ab9bc23'
                         'fb24756e9713a93f90', lu.udid)
        # Should hit on the second VIOS
        self.assertIs(rsp2.entry, vios.entry)
        self.assertEqual(1, mock_add.call_count)
        mock_add.assert_called_with('67dca605-3923-34da-bd8f-26a378fc817f',
                                    vios, 'mp_uuid', lu)

        # No hits
        mock_add.reset_mock()
        self.apt.read.side_effect = [rsp3, rsp3]
        self.assertRaises(driver.InstanceDiskMappingFailed,
                          ssp_stor.connect_instance_disk_to_mgmt, inst)
        self.assertEqual(0, mock_add.call_count)

        # First add_vscsi_mapping call raises
        self.apt.read.side_effect = [rsp1, rsp2]
        mock_add.side_effect = [Exception("mapping failed"), None]
        # Should hit on the second VIOS
        self.assertIs(rsp2.entry, vios.entry)

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_lu_mapping')
    def test_disconnect_disk_from_mgmt(self, mock_rm_lu_map):
        ssp_stor = self._get_ssp_stor()
        ssp_stor.disconnect_disk_from_mgmt('vios_uuid', 'disk_name')
        mock_rm_lu_map.assert_called_with(ssp_stor.adapter, 'vios_uuid',
                                          'mp_uuid', disk_names=['disk_name'])
