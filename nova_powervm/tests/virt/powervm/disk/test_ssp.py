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


import fixtures
import mock

import copy
from nova.objects import image_meta
from nova import test
import pypowervm.adapter as pvm_adp
from pypowervm import const
import pypowervm.entities as pvm_ent
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.tests.test_utils import pvmhttp
from pypowervm.wrappers import cluster as pvm_clust
from pypowervm.wrappers import storage as pvm_stg
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm.disk import driver as disk_dvr
from nova_powervm.virt.powervm.disk import ssp as ssp_dvr
from nova_powervm.virt.powervm import exception as npvmex


SSP = 'ssp.txt'
CLUST = 'cluster.txt'
VIO = 'fake_vios_with_volume_group_data.txt'


class SSPFixture(fixtures.Fixture):
    """Patch out PyPowerVM SSP EntryWrapper search and refresh."""

    def __init__(self):
        pass

    def mockpatch(self, methstr):
        return self.useFixture(fixtures.MockPatch(methstr)).mock

    def setUp(self):
        super(SSPFixture, self).setUp()
        self.mock_search = self.mockpatch(
            'pypowervm.wrappers.cluster.Cluster.search')
        self.mock_clust_refresh = self.mockpatch(
            'pypowervm.wrappers.cluster.Cluster.refresh')
        self.mock_ssp_refresh = self.mockpatch(
            'pypowervm.wrappers.storage.SSP.refresh')
        self.mock_ssp_update = self.mockpatch(
            'pypowervm.wrappers.storage.SSP.update')
        self.mock_get_tier = self.mockpatch(
            'pypowervm.tasks.storage.default_tier_for_ssp')


class TestSSPDiskAdapter(test.TestCase):
    """Unit Tests for the LocalDisk storage driver."""

    def setUp(self):
        super(TestSSPDiskAdapter, self).setUp()

        class Instance(object):
            uuid = fx.FAKE_INST_UUID
            name = 'instance-name'

        self.instance = Instance()

        self.apt = self.useFixture(pvm_fx.AdapterFx()).adpt

        def resp(file_name):
            return pvmhttp.load_pvm_resp(
                file_name, adapter=self.apt).get_response()

        self.ssp_resp = resp(SSP)
        self.clust_resp = resp(CLUST)
        self.vio = resp(VIO)

        self.sspfx = self.useFixture(SSPFixture())

        # For _fetch_cluster() with configured name
        self.mock_search = self.sspfx.mock_search
        # EntryWrapper.search always returns a list of wrappers.
        self.mock_search.return_value = [pvm_clust.Cluster.wrap(
            self.clust_resp)]

        # For _refresh_cluster()
        self.mock_clust_refresh = self.sspfx.mock_clust_refresh
        self.mock_clust_refresh.return_value = pvm_clust.Cluster.wrap(
            self.clust_resp)

        # For _fetch_ssp() fresh
        self.apt.read_by_href.return_value = self.ssp_resp

        # For _fetch_ssp() refresh
        self.mock_ssp_refresh = self.sspfx.mock_ssp_refresh
        self.mock_ssp_refresh.return_value = pvm_stg.SSP.wrap(self.ssp_resp)

        # For _tier
        self.mock_get_tier = self.sspfx.mock_get_tier

        # By default, assume the config supplied a Cluster name
        self.flags(cluster_name='clust1', group='powervm')

    def _get_ssp_stor(self):
        ssp_stor = ssp_dvr.SSPDiskAdapter(
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

    def test_tier_cache(self):
        # default_tier_for_ssp not yet invoked
        self.mock_get_tier.assert_not_called()
        ssp = self._get_ssp_stor()
        # default_tier_for_ssp invoked by constructor
        self.mock_get_tier.assert_called_once_with(ssp._ssp_wrap)
        self.assertEqual(self.mock_get_tier.return_value, ssp._tier)
        # default_tier_for_ssp not called again.
        self.assertEqual(1, self.mock_get_tier.call_count)

    def test_capabilities(self):
        ssp_stor = self._get_ssp_stor()
        # Ensure return shared storage
        self.assertTrue(ssp_stor.capabilities.get('shared_storage'))

    def test_get_info(self):
        ssp_stor = self._get_ssp_stor()
        expected = {'cluster_name': 'neoclust1',
                    'ssp_name': 'neossp1',
                    'ssp_uuid': 'e357a79a-7a3d-35b6-8405-55ab6a2d0de7'}
        # Ensure the base method returns empty dict
        self.assertEqual(expected, ssp_stor.get_info())

    def test_validate(self):
        ssp_stor = self._get_ssp_stor()
        fake_data = {}
        # Ensure returns error message when no data
        self.assertIsNotNone(ssp_stor.validate(fake_data))

        # Get our own data and it should always match!
        fake_data = ssp_stor.get_info()
        # Ensure returns no error on good data
        self.assertIsNone(ssp_stor.validate(fake_data))

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
        self.flags(cluster_name='', group='powervm')
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
        self.mock_search.return_value = []
        self.assertRaises(npvmex.ClusterNotFoundByName, self._get_ssp_stor)

    def test_init_TooManyClustersFound(self):
        """Search-by-name returns more than one result."""
        clust1 = pvm_clust.Cluster.bld(None, 'newclust1',
                                       pvm_stg.PV.bld(None, 'hdisk1'),
                                       pvm_clust.Node.bld('vios1'))
        clust2 = pvm_clust.Cluster.bld(None, 'newclust2',
                                       pvm_stg.PV.bld(None, 'hdisk2'),
                                       pvm_clust.Node.bld(None, 'vios2'))
        self.mock_search.return_value = [clust1.entry, clust2.entry]
        self.assertRaises(npvmex.TooManyClustersFound, self._get_ssp_stor)

    def test_init_NoConfigNoClusterFound(self):
        """No cluster name specified in config, no clusters on host."""
        self.flags(cluster_name='', group='powervm')
        self.apt.read.return_value = self._bld_resp(status=204)
        self.assertRaises(npvmex.NoConfigNoClusterFound, self._get_ssp_stor)

    def test_init_NoConfigTooManyClusters(self):
        """No SSP name specified in config, more than one SSP on host."""
        self.flags(cluster_name='', group='powervm')
        clust1 = pvm_clust.Cluster.bld(None, 'newclust1',
                                       pvm_stg.PV.bld(None, 'hdisk1'),
                                       pvm_clust.Node.bld(None, 'vios1'))
        clust2 = pvm_clust.Cluster.bld(None, 'newclust2',
                                       pvm_stg.PV.bld(None, 'hdisk2'),
                                       pvm_clust.Node.bld(None, 'vios2'))
        self.apt.read.return_value = self._bld_resp(
            entry_or_list=[clust1.entry, clust2.entry])
        self.assertRaises(npvmex.NoConfigTooManyClusters, self._get_ssp_stor)

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
        self.mock_get_tier.return_value.refresh.return_value.capacity = 10
        self.assertEqual(10.0, ssp_stor.capacity)

    def test_capacity_used(self):
        ssp_stor = self._get_ssp_stor()
        self.assertEqual((49.88 - 48.98), ssp_stor.capacity_used)

    @mock.patch('pypowervm.tasks.cluster_ssp.get_or_upload_image_lu')
    @mock.patch('nova_powervm.virt.powervm.disk.driver.DiskAdapter.'
                '_get_image_name')
    @mock.patch('nova_powervm.virt.powervm.disk.ssp.SSPDiskAdapter.'
                '_any_vios_uuid')
    @mock.patch('nova_powervm.virt.powervm.disk.driver.DiskAdapter.'
                '_get_disk_name')
    @mock.patch('pypowervm.tasks.storage.crt_lu')
    def test_create_disk_from_image(self, mock_crt_lu, mock_gdn, mock_vuuid,
                                    mock_gin, mock_goru):
        context = mock.Mock()
        instance = mock.Mock()
        img_meta = mock.Mock()
        disk_size_gb = mock.Mock()
        image_type = mock.Mock()
        image_lu = mock.Mock()
        boot_lu = mock.Mock()

        ssp = self._get_ssp_stor()
        mock_goru.return_value = image_lu
        mock_crt_lu.return_value = ssp._ssp_wrap, boot_lu

        # Default image_type
        self.assertEqual(boot_lu, ssp.create_disk_from_image(
            context, instance, img_meta, disk_size_gb))
        mock_goru.assert_called_once_with(
            self.mock_get_tier.return_value, mock_gin.return_value,
            mock_vuuid.return_value, mock.ANY, img_meta.size)
        mock_gdn.assert_called_once_with(disk_dvr.DiskType.BOOT, instance)
        mock_crt_lu.assert_called_once_with(
            self.mock_get_tier.return_value, mock_gdn.return_value,
            disk_size_gb, typ=pvm_stg.LUType.DISK, clone=image_lu)
        # Reset
        mock_goru.reset_mock()
        mock_gdn.reset_mock()
        mock_crt_lu.reset_mock()

        # Specified image_type
        self.assertEqual(boot_lu, ssp.create_disk_from_image(
            context, instance, img_meta, disk_size_gb, image_type=image_type))
        mock_goru.assert_called_once_with(
            self.mock_get_tier.return_value, mock_gin.return_value,
            mock_vuuid.return_value, mock.ANY, img_meta.size)
        mock_gdn.assert_called_once_with(image_type, instance)
        mock_crt_lu.assert_called_once_with(
            self.mock_get_tier.return_value, mock_gdn.return_value,
            disk_size_gb, typ=pvm_stg.LUType.DISK, clone=image_lu)

    def test_get_image_name(self):
        """Generate image name from ImageMeta."""
        ssp = self._get_ssp_stor()

        def verify_image_name(name, checksum, expected):
            img_meta = image_meta.ImageMeta(name=name, checksum=checksum)
            self.assertEqual(expected, ssp._get_image_name(img_meta))
            self.assertTrue(len(expected) <= const.MaxLen.FILENAME_DEFAULT)

        verify_image_name('foo', 'bar', 'image_foo_bar')
        # Ensure a really long name gets truncated properly.  Note also '-'
        # chars are sanitized.
        verify_image_name(
            'Template_zw82enbix_PowerVM-CI-18y2385y9123785192364',
            'b518a8ba2b152b5607aceb5703fac072',
            'image_Template_zw82enbix_PowerVM_CI_18y2385y91'
            '_b518a8ba2b152b5607aceb5703fac072')

    @mock.patch('pypowervm.wrappers.storage.LUEnt.search')
    @mock.patch('nova_powervm.virt.powervm.disk.driver.DiskAdapter.'
                '_get_disk_name')
    def test_get_disk_ref(self, mock_dsk_nm, mock_srch):
        ssp = self._get_ssp_stor()
        self.assertEqual(mock_srch.return_value, ssp.get_disk_ref(
            self.instance, disk_dvr.DiskType.BOOT))
        mock_dsk_nm.assert_called_with(disk_dvr.DiskType.BOOT, self.instance)
        mock_srch.assert_called_with(
            ssp.adapter, parent=self.mock_get_tier.return_value,
            name=mock_dsk_nm.return_value, lu_type=pvm_stg.LUType.DISK,
            one_result=True)

        # Assert handles not finding it.
        mock_srch.return_value = None
        self.assertIsNone(
            ssp.get_disk_ref(self.instance, disk_dvr.DiskType.BOOT))

    @mock.patch('nova_powervm.virt.powervm.disk.ssp.SSPDiskAdapter.'
                'vios_uuids')
    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_map')
    @mock.patch('nova_powervm.virt.powervm.vios.get_active_vioses')
    def test_connect_disk(self, mock_active_vioses, mock_add_map,
                          mock_build_map, mock_vio_uuids):
        # vio is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTaskFx and FeedTask.
        feed = [pvm_vios.VIOS.wrap(self.vio)]
        mock_active_vioses.return_value = feed
        ft_fx = pvm_fx.FeedTaskFx(feed)
        self.useFixture(ft_fx)

        # The mock return values
        mock_add_map.return_value = True
        mock_build_map.return_value = 'fake_map'

        # Need the driver to return the actual UUID of the VIOS in the feed,
        # to match the FeedTask.
        ssp = self._get_ssp_stor()
        ssp.vios_uuids = [feed[0].uuid]
        inst = mock.Mock(uuid=fx.FAKE_INST_UUID)

        # As initialized above, remove_maps returns True to trigger update.
        ssp.connect_disk(mock.MagicMock(), inst, mock.MagicMock(),
                         stg_ftsk=None)
        self.assertEqual(1, mock_add_map.call_count)
        mock_add_map.assert_called_once_with(feed[0], 'fake_map')
        self.assertEqual(1, ft_fx.patchers['update'].mock.call_count)

    @mock.patch('nova_powervm.virt.powervm.disk.ssp.SSPDiskAdapter.'
                'vios_uuids')
    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_map')
    @mock.patch('nova_powervm.virt.powervm.vios.get_active_vioses')
    def test_connect_disk_no_update(self, mock_active_vioses, mock_add_map,
                                    mock_build_map, mock_vio_uuids):
        # vio is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTaskFx and FeedTask.
        feed = [pvm_vios.VIOS.wrap(self.vio)]
        mock_active_vioses.return_value = feed
        ft_fx = pvm_fx.FeedTaskFx(feed)
        self.useFixture(ft_fx)

        # The mock return values
        mock_add_map.return_value = None
        mock_build_map.return_value = 'fake_map'

        # Need the driver to return the actual UUID of the VIOS in the feed,
        # to match the FeedTask.
        ssp = self._get_ssp_stor()
        ssp.vios_uuids = [feed[0].uuid]
        inst = mock.Mock(uuid=fx.FAKE_INST_UUID)

        # As initialized above, remove_maps returns True to trigger update.
        ssp.connect_disk(mock.MagicMock(), inst, mock.MagicMock(),
                         stg_ftsk=None)
        self.assertEqual(1, mock_add_map.call_count)
        mock_add_map.assert_called_once_with(feed[0], 'fake_map')
        self.assertEqual(0, ft_fx.patchers['update'].mock.call_count)

    @mock.patch('pypowervm.tasks.storage.rm_tier_storage')
    def test_delete_disks(self, mock_rm_tstor):
        sspdrv = self._get_ssp_stor()
        sspdrv.delete_disks(None, None, ['disk1', 'disk2'])
        mock_rm_tstor.assert_called_once_with(['disk1', 'disk2'],
                                              tier=sspdrv._tier)

    @mock.patch('nova_powervm.virt.powervm.disk.ssp.SSPDiskAdapter.'
                'vios_uuids')
    @mock.patch('pypowervm.tasks.scsi_mapper.find_maps')
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps')
    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('nova_powervm.virt.powervm.vios.get_active_vioses')
    def test_disconnect_disk(self, mock_active_vioses, mock_build_map,
                             mock_remove_maps, mock_find_maps, mock_vio_uuids):
        # vio is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTaskFx and FeedTask.
        feed = [pvm_vios.VIOS.wrap(self.vio)]
        ft_fx = pvm_fx.FeedTaskFx(feed)
        mock_active_vioses.return_value = feed
        self.useFixture(ft_fx)

        # The mock return values
        mock_build_map.return_value = 'fake_map'

        # Need the driver to return the actual UUID of the VIOS in the feed,
        # to match the FeedTask.
        ssp = self._get_ssp_stor()
        ssp.vios_uuids = [feed[0].uuid]

        # Make the LU's to remove
        def mklu(udid):
            lu = pvm_stg.LU.bld(None, 'lu_%s' % udid, 1)
            lu._udid('27%s' % udid)
            return lu

        lu1 = mklu('abc')
        lu2 = mklu('def')

        def remove_resp(vios_w, client_lpar_id, match_func=None,
                        include_orphans=False):
            return [mock.Mock(backing_storage=lu1),
                    mock.Mock(backing_storage=lu2)]

        mock_remove_maps.side_effect = remove_resp
        mock_find_maps.side_effect = remove_resp

        # As initialized above, remove_maps returns True to trigger update.
        lu_list = ssp.disconnect_image_disk(mock.Mock(), self.instance,
                                            stg_ftsk=None)
        self.assertEqual({lu1, lu2}, set(lu_list))
        self.assertEqual(1, mock_remove_maps.call_count)
        self.assertEqual(1, ft_fx.patchers['update'].mock.call_count)

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
                           xag=[const.XAG.VIO_SMAP])
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
        self.assertRaises(npvmex.InstanceDiskMappingFailed,
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
