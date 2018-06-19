# Copyright IBM Corp. and contributors
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

from nova.objects import image_meta
from nova import test
from pypowervm import const
from pypowervm.tasks import storage as tsk_stg
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.wrappers import cluster as pvm_clust
from pypowervm.wrappers import storage as pvm_stg
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm.disk import driver as disk_dvr
from nova_powervm.virt.powervm.disk import ssp as ssp_dvr
from nova_powervm.virt.powervm import exception as npvmex


class SSPFixture(fixtures.Fixture):
    """Patch out PyPowerVM SSP and Cluster EntryWrapper methods."""

    def __init__(self):
        pass

    def mockpatch(self, methstr):
        return self.useFixture(fixtures.MockPatch(methstr)).mock

    def setUp(self):
        super(SSPFixture, self).setUp()
        self.mock_clust_get = self.mockpatch(
            'pypowervm.wrappers.cluster.Cluster.get')
        self.mock_clust_search = self.mockpatch(
            'pypowervm.wrappers.cluster.Cluster.search')
        self.mock_ssp_gbhref = self.mockpatch(
            'pypowervm.wrappers.storage.SSP.get_by_href')
        self.mock_ssp_update = self.mockpatch(
            'pypowervm.wrappers.storage.SSP.update')
        self.mock_get_tier = self.mockpatch(
            'pypowervm.tasks.storage.default_tier_for_ssp')


class TestSSPDiskAdapter(test.NoDBTestCase):
    """Unit Tests for the LocalDisk storage driver."""

    def setUp(self):
        super(TestSSPDiskAdapter, self).setUp()

        class Instance(object):
            uuid = fx.FAKE_INST_UUID
            name = 'instance-name'

        self.instance = Instance()

        self.apt = mock.Mock()
        self.host_uuid = 'host_uuid'

        self.sspfx = self.useFixture(SSPFixture())

        self.ssp_wrap = mock.Mock(spec=pvm_stg.SSP)
        self.ssp_wrap.refresh.return_value = self.ssp_wrap
        self.node1 = mock.Mock()
        self.node2 = mock.Mock()
        self.clust_wrap = mock.Mock(spec=pvm_clust.Cluster,
                                    nodes=[self.node1, self.node2])
        self.clust_wrap.refresh.return_value = self.clust_wrap
        self.vio_wrap = mock.Mock(spec=pvm_vios.VIOS, uuid='uuid')

        # For _fetch_cluster() with no name
        self.mock_clust_get = self.sspfx.mock_clust_get
        self.mock_clust_get.return_value = [self.clust_wrap]
        # For _fetch_cluster() with configured name
        self.mock_clust_search = self.sspfx.mock_clust_search
        # EntryWrapper.search always returns a list of wrappers.
        self.mock_clust_search.return_value = [self.clust_wrap]

        # For _fetch_ssp() fresh
        self.mock_ssp_gbhref = self.sspfx.mock_ssp_gbhref
        self.mock_ssp_gbhref.return_value = self.ssp_wrap

        # For _tier
        self.mock_get_tier = self.sspfx.mock_get_tier

        # By default, assume the config supplied a Cluster name
        self.flags(cluster_name='clust1', group='powervm')

        # Return the mgmt uuid
        self.mgmt_uuid = self.useFixture(fixtures.MockPatch(
            'nova_powervm.virt.powervm.mgmt.mgmt_uuid')).mock
        self.mgmt_uuid.return_value = 'mp_uuid'

    def _get_ssp_stor(self):
        return ssp_dvr.SSPDiskAdapter(self.apt, self.host_uuid)

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
        self.assertTrue(ssp_stor.capabilities.get('shared_storage'))
        self.assertFalse(ssp_stor.capabilities.get('has_imagecache'))
        self.assertTrue(ssp_stor.capabilities.get('snapshot'))

    def test_get_info(self):
        ssp_stor = self._get_ssp_stor()
        expected = {'cluster_name': self.clust_wrap.name,
                    'ssp_name': self.ssp_wrap.name,
                    'ssp_uuid': self.ssp_wrap.uuid}
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
        # _fetch_cluster() WITH configured name does a search, but not a get.
        # Refresh shouldn't be invoked.
        self.mock_clust_search.assert_called_once_with(self.apt, name='clust1')
        self.mock_clust_get.assert_not_called()
        self.clust_wrap.refresh.assert_not_called()

    def test_init_green_no_config(self):
        """No cluster name specified in config; one cluster on host - ok."""
        self.flags(cluster_name='', group='powervm')
        self._get_ssp_stor()
        # _fetch_cluster() WITHOUT configured name does feed GET, not a search.
        # Refresh shouldn't be invoked.
        self.mock_clust_search.assert_not_called()
        self.mock_clust_get.assert_called_once_with(self.apt)
        self.clust_wrap.refresh.assert_not_called()

    def test_init_ClusterNotFoundByName(self):
        """Empty feed comes back from search - no cluster by that name."""
        self.mock_clust_search.return_value = []
        self.assertRaises(npvmex.ClusterNotFoundByName, self._get_ssp_stor)

    def test_init_TooManyClustersFound(self):
        """Search-by-name returns more than one result."""
        self.mock_clust_search.return_value = ['newclust1', 'newclust2']
        self.assertRaises(npvmex.TooManyClustersFound, self._get_ssp_stor)

    def test_init_NoConfigNoClusterFound(self):
        """No cluster name specified in config, no clusters on host."""
        self.flags(cluster_name='', group='powervm')
        self.mock_clust_get.return_value = []
        self.assertRaises(npvmex.NoConfigNoClusterFound, self._get_ssp_stor)

    def test_init_NoConfigTooManyClusters(self):
        """No SSP name specified in config, more than one SSP on host."""
        self.flags(cluster_name='', group='powervm')
        self.mock_clust_get.return_value = ['newclust1', 'newclust2']
        self.assertRaises(npvmex.NoConfigTooManyClusters, self._get_ssp_stor)

    def test_refresh_cluster(self):
        """_refresh_cluster with cached wrapper."""
        # Save original cluster wrapper for later comparison
        orig_clust_wrap = self.clust_wrap
        # Prime _clust_wrap
        ssp_stor = self._get_ssp_stor()
        # Verify baseline call counts
        self.mock_clust_search.assert_called_once_with(self.apt, name='clust1')
        self.clust_wrap.refresh.assert_not_called()
        clust_wrap = ssp_stor._refresh_cluster()
        # This should call refresh
        self.mock_clust_search.assert_called_once_with(self.apt, name='clust1')
        self.mock_clust_get.assert_not_called()
        self.clust_wrap.refresh.assert_called_once_with()
        self.assertEqual(clust_wrap.name, orig_clust_wrap.name)

    def test_fetch_ssp(self):
        # For later comparison
        orig_ssp_wrap = self.ssp_wrap
        # Verify baseline call counts
        self.mock_ssp_gbhref.assert_not_called()
        self.ssp_wrap.refresh.assert_not_called()
        # This should prime self._ssp_wrap: calls read_by_href but not refresh.
        ssp_stor = self._get_ssp_stor()
        self.mock_ssp_gbhref.assert_called_once_with(self.apt,
                                                     self.clust_wrap.ssp_uri)
        self.ssp_wrap.refresh.assert_not_called()
        # Accessing the @property will trigger refresh
        ssp_wrap = ssp_stor._ssp
        self.mock_ssp_gbhref.assert_called_once_with(self.apt,
                                                     self.clust_wrap.ssp_uri)
        self.ssp_wrap.refresh.assert_called_once_with()
        self.assertEqual(ssp_wrap.name, orig_ssp_wrap.name)

    @mock.patch('pypowervm.util.get_req_path_uuid')
    def test_vios_uuids(self, mock_rpu):
        mock_rpu.return_value = self.host_uuid
        ssp_stor = self._get_ssp_stor()
        vios_uuids = ssp_stor.vios_uuids
        self.assertEqual({self.node1.vios_uuid, self.node2.vios_uuid},
                         set(vios_uuids))
        mock_rpu.assert_has_calls(
            [mock.call(node.vios_uri, preserve_case=True, root=True)
             for node in [self.node1, self.node2]])
        s = set()
        for i in range(1000):
            u = ssp_stor._any_vios_uuid()
            # Make sure we got a good value
            self.assertIn(u, vios_uuids)
            s.add(u)
        # Make sure we hit all the values over 1000 iterations.  This isn't
        # guaranteed to work, but the odds of failure should be infinitesimal.
        self.assertEqual(set(vios_uuids), s)

        mock_rpu.reset_mock()

        # Test VIOSes on other nodes, which won't have uuid or url
        node1 = mock.Mock(vios_uuid=None, vios_uri='uri1')
        node2 = mock.Mock(vios_uuid='2', vios_uri=None)
        # This mock is good and should be returned
        node3 = mock.Mock(vios_uuid='3', vios_uri='uri3')
        self.clust_wrap.nodes = [node1, node2, node3]
        self.assertEqual(['3'], ssp_stor.vios_uuids)
        # get_req_path_uuid was only called on the good one
        mock_rpu.assert_called_once_with('uri3', preserve_case=True, root=True)

    def test_capacity(self):
        ssp_stor = self._get_ssp_stor()
        self.mock_get_tier.return_value.refresh.return_value.capacity = 10
        self.assertAlmostEqual(10.0, ssp_stor.capacity)

    def test_capacity_used(self):
        ssp_stor = self._get_ssp_stor()
        self.ssp_wrap.capacity = 4.56
        self.ssp_wrap.free_space = 1.23
        self.assertAlmostEqual((4.56 - 1.23), ssp_stor.capacity_used)

    @mock.patch('pypowervm.tasks.cluster_ssp.get_or_upload_image_lu')
    @mock.patch('nova_powervm.virt.powervm.disk.driver.DiskAdapter.'
                '_get_image_name')
    @mock.patch('nova_powervm.virt.powervm.disk.ssp.SSPDiskAdapter.'
                '_any_vios_uuid')
    @mock.patch('nova_powervm.virt.powervm.disk.driver.DiskAdapter.'
                '_get_disk_name')
    @mock.patch('pypowervm.tasks.storage.crt_lu')
    @mock.patch('nova.image.api.API.download')
    @mock.patch('nova_powervm.virt.powervm.disk.driver.IterableToFileAdapter')
    def test_create_disk_from_image(self, mock_it2f, mock_dl, mock_crt_lu,
                                    mock_gdn, mock_vuuid, mock_gin, mock_goru):
        instance = mock.Mock()
        img_meta = mock.Mock()

        ssp = self._get_ssp_stor()
        mock_crt_lu.return_value = ssp._ssp_wrap, 'lu'

        mock_gin.return_value = 'img_name'
        mock_vuuid.return_value = 'vios_uuid'

        # Default image_type
        self.assertEqual('lu', ssp.create_disk_from_image(
            'ctx', instance, img_meta))
        mock_goru.assert_called_once_with(
            self.mock_get_tier.return_value, mock_gin.return_value,
            mock_vuuid.return_value, mock_it2f.return_value, img_meta.size,
            upload_type=tsk_stg.UploadType.IO_STREAM)
        mock_dl.assert_called_once_with('ctx', img_meta.id)
        mock_it2f.assert_called_once_with(mock_dl.return_value)
        mock_gdn.assert_called_once_with(disk_dvr.DiskType.BOOT, instance)
        mock_crt_lu.assert_called_once_with(
            self.mock_get_tier.return_value, mock_gdn.return_value,
            instance.flavor.root_gb, typ=pvm_stg.LUType.DISK,
            clone=mock_goru.return_value)

        # Reset
        mock_goru.reset_mock()
        mock_gdn.reset_mock()
        mock_crt_lu.reset_mock()
        mock_dl.reset_mock()
        mock_it2f.reset_mock()

        # Specified image_type
        self.assertEqual('lu', ssp.create_disk_from_image(
            'ctx', instance, img_meta, image_type='imgtyp'))
        mock_goru.assert_called_once_with(
            self.mock_get_tier.return_value, mock_gin.return_value,
            mock_vuuid.return_value, mock_it2f.return_value, img_meta.size,
            upload_type=tsk_stg.UploadType.IO_STREAM)
        mock_dl.assert_called_once_with('ctx', img_meta.id)
        mock_it2f.assert_called_once_with(mock_dl.return_value)
        mock_gdn.assert_called_once_with('imgtyp', instance)
        mock_crt_lu.assert_called_once_with(
            self.mock_get_tier.return_value, mock_gdn.return_value,
            instance.flavor.root_gb, typ=pvm_stg.LUType.DISK,
            clone=mock_goru.return_value)

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
                'vios_uuids', new_callable=mock.PropertyMock)
    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_map')
    @mock.patch('pypowervm.tasks.partition.get_active_vioses')
    def test_connect_disk(self, mock_active_vioses, mock_add_map,
                          mock_build_map, mock_vio_uuids):
        # vio is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTaskFx and FeedTask.
        feed = [self.vio_wrap]
        mock_active_vioses.return_value = feed
        ft_fx = pvm_fx.FeedTaskFx(feed)
        self.useFixture(ft_fx)

        # The mock return values
        mock_add_map.return_value = True
        mock_build_map.return_value = 'fake_map'

        # Need the driver to return the actual UUID of the VIOS in the feed,
        # to match the FeedTask.
        ssp = self._get_ssp_stor()
        mock_vio_uuids.return_value = [self.vio_wrap.uuid]
        inst = mock.Mock(uuid=fx.FAKE_INST_UUID)

        # As initialized above, remove_maps returns True to trigger update.
        ssp.connect_disk(inst, mock.Mock(), stg_ftsk=None)
        mock_add_map.assert_called_once_with(self.vio_wrap, 'fake_map')
        self.vio_wrap.update.assert_called_once_with(timeout=mock.ANY)

    @mock.patch('nova_powervm.virt.powervm.disk.ssp.SSPDiskAdapter.'
                'vios_uuids', new_callable=mock.PropertyMock)
    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_map')
    @mock.patch('pypowervm.tasks.partition.get_active_vioses')
    def test_connect_disk_no_update(self, mock_active_vioses, mock_add_map,
                                    mock_build_map, mock_vio_uuids):
        # vio is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTaskFx and FeedTask.
        feed = [self.vio_wrap]
        mock_active_vioses.return_value = feed
        ft_fx = pvm_fx.FeedTaskFx(feed)
        self.useFixture(ft_fx)

        # The mock return values
        mock_add_map.return_value = None
        mock_build_map.return_value = 'fake_map'

        # Need the driver to return the actual UUID of the VIOS in the feed,
        # to match the FeedTask.
        ssp = self._get_ssp_stor()
        mock_vio_uuids.return_value = [self.vio_wrap.uuid]
        inst = mock.Mock(uuid=fx.FAKE_INST_UUID)

        # As initialized above, add_maps returns False to skip update.
        ssp.connect_disk(inst, mock.Mock(), stg_ftsk=None)
        mock_add_map.assert_called_once_with(self.vio_wrap, 'fake_map')
        self.vio_wrap.update.assert_not_called()

    @mock.patch('pypowervm.tasks.storage.rm_tier_storage')
    def test_delete_disks(self, mock_rm_tstor):
        sspdrv = self._get_ssp_stor()
        sspdrv.delete_disks(['disk1', 'disk2'])
        mock_rm_tstor.assert_called_once_with(['disk1', 'disk2'],
                                              tier=sspdrv._tier)

    @mock.patch('nova_powervm.virt.powervm.disk.ssp.SSPDiskAdapter.'
                'vios_uuids', new_callable=mock.PropertyMock)
    @mock.patch('pypowervm.tasks.scsi_mapper.find_maps')
    @mock.patch('pypowervm.tasks.scsi_mapper.remove_maps')
    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping')
    @mock.patch('pypowervm.tasks.partition.get_active_vioses')
    def test_disconnect_disk(self, mock_active_vioses, mock_build_map,
                             mock_remove_maps, mock_find_maps, mock_vio_uuids):
        # vio is a single-entry response.  Wrap it and put it in a list
        # to act as the feed for FeedTaskFx and FeedTask.
        feed = [self.vio_wrap]
        ft_fx = pvm_fx.FeedTaskFx(feed)
        mock_active_vioses.return_value = feed
        self.useFixture(ft_fx)

        # The mock return values
        mock_build_map.return_value = 'fake_map'

        # Need the driver to return the actual UUID of the VIOS in the feed,
        # to match the FeedTask.
        ssp = self._get_ssp_stor()
        mock_vio_uuids.return_value = [self.vio_wrap.uuid]

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
        lu_list = ssp.disconnect_disk(self.instance, stg_ftsk=None)
        self.assertEqual({lu1, lu2}, set(lu_list))
        mock_remove_maps.assert_called_once_with(
            self.vio_wrap, fx.FAKE_INST_UUID_PVM, match_func=mock.ANY)
        self.vio_wrap.update.assert_called_once_with(timeout=mock.ANY)

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
        lu_wrap = mock.Mock(spec=pvm_stg.LU)
        lu_wrap.configure_mock(name='boot_my_instance_name', udid='lu_udid')
        smap = mock.Mock(backing_storage=lu_wrap,
                         server_adapter=mock.Mock(lpar_id=4))
        # Build mock VIOS Wrappers as the returns from VIOS.wrap.
        # vios1 and vios2 will both have the mapping for client ID 4 and LU
        # named boot_my_instance_name.
        smaps = [mock.Mock(), mock.Mock(), mock.Mock(), smap]
        vios1 = mock.Mock(spec=pvm_vios.VIOS)
        vios1.configure_mock(name='vios1', uuid='uuid1', scsi_mappings=smaps)
        vios2 = mock.Mock(spec=pvm_vios.VIOS)
        vios2.configure_mock(name='vios2', uuid='uuid2', scsi_mappings=smaps)
        # vios3 will not have the mapping
        vios3 = mock.Mock(spec=pvm_vios.VIOS)
        vios3.configure_mock(name='vios3', uuid='uuid3',
                             scsi_mappings=[mock.Mock(), mock.Mock()])
        return inst, lpar_wrap, vios1, vios2, vios3

    @mock.patch('nova_powervm.virt.powervm.disk.ssp.SSPDiskAdapter.'
                'vios_uuids', new_callable=mock.PropertyMock)
    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.get')
    def test_get_bootdisk_iter(self, mock_vio_get, mock_lw, mock_vio_uuids):
        inst, lpar_wrap, vio1, vio2, vio3 = self._bld_mocks_for_instance_disk()
        mock_lw.return_value = lpar_wrap
        mock_vio_uuids.return_value = [1, 2]
        ssp_stor = self._get_ssp_stor()

        # Test with two VIOSes, both of which contain the mapping.  Force the
        # method to get the lpar_wrap.
        mock_vio_get.side_effect = [vio1, vio2]
        idi = ssp_stor._get_bootdisk_iter(inst)
        lu, vios = next(idi)
        self.assertEqual('lu_udid', lu.udid)
        self.assertEqual('vios1', vios.name)
        mock_vio_get.assert_called_once_with(self.apt, uuid=1,
                                             xag=[const.XAG.VIO_SMAP])
        lu, vios = next(idi)
        self.assertEqual('lu_udid', lu.udid)
        self.assertEqual('vios2', vios.name)
        mock_vio_get.assert_called_with(self.apt, uuid=2,
                                        xag=[const.XAG.VIO_SMAP])
        self.assertRaises(StopIteration, next, idi)
        self.assertEqual(2, mock_vio_get.call_count)
        mock_lw.assert_called_once_with(self.apt, inst)

        # Same, but prove that breaking out of the loop early avoids the second
        # get call.
        mock_vio_get.reset_mock()
        mock_lw.reset_mock()
        mock_vio_get.side_effect = [vio1, vio2]
        for lu, vios in ssp_stor._get_bootdisk_iter(inst):
            self.assertEqual('lu_udid', lu.udid)
            self.assertEqual('vios1', vios.name)
            break
        mock_vio_get.assert_called_once_with(self.apt, uuid=1,
                                             xag=[const.XAG.VIO_SMAP])

        # Now the first VIOS doesn't have the mapping, but the second does
        mock_vio_get.reset_mock()
        mock_vio_get.side_effect = [vio3, vio2]
        idi = ssp_stor._get_bootdisk_iter(inst)
        lu, vios = next(idi)
        self.assertEqual('lu_udid', lu.udid)
        self.assertEqual('vios2', vios.name)
        mock_vio_get.assert_has_calls(
            [mock.call(self.apt, uuid=uuid, xag=[const.XAG.VIO_SMAP])
             for uuid in (1, 2)])
        self.assertRaises(StopIteration, next, idi)
        self.assertEqual(2, mock_vio_get.call_count)

        # No hits
        mock_vio_get.reset_mock()
        mock_vio_get.side_effect = [vio3, vio3]
        self.assertEqual([], list(ssp_stor._get_bootdisk_iter(inst)))
        self.assertEqual(2, mock_vio_get.call_count)

    @mock.patch('nova_powervm.virt.powervm.disk.ssp.SSPDiskAdapter.'
                'vios_uuids', new_callable=mock.PropertyMock)
    @mock.patch('nova_powervm.virt.powervm.vm.get_instance_wrapper')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.get')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_vscsi_mapping')
    def test_connect_instance_disk_to_mgmt(self, mock_add, mock_vio_get,
                                           mock_lw, mock_vio_uuids):
        inst, lpar_wrap, vio1, vio2, vio3 = self._bld_mocks_for_instance_disk()
        mock_lw.return_value = lpar_wrap
        mock_vio_uuids.return_value = [1, 2]
        ssp_stor = self._get_ssp_stor()

        # Test with two VIOSes, both of which contain the mapping
        mock_vio_get.side_effect = [vio1, vio2]
        lu, vios = ssp_stor.connect_instance_disk_to_mgmt(inst)
        self.assertEqual('lu_udid', lu.udid)
        # Should hit on the first VIOS
        self.assertIs(vio1, vios)
        mock_add.assert_called_once_with(self.host_uuid, vio1, 'mp_uuid', lu)

        # Now the first VIOS doesn't have the mapping, but the second does
        mock_add.reset_mock()
        mock_vio_get.side_effect = [vio3, vio2]
        lu, vios = ssp_stor.connect_instance_disk_to_mgmt(inst)
        self.assertEqual('lu_udid', lu.udid)
        # Should hit on the second VIOS
        self.assertIs(vio2, vios)
        self.assertEqual(1, mock_add.call_count)
        mock_add.assert_called_once_with(self.host_uuid, vio2, 'mp_uuid', lu)

        # No hits
        mock_add.reset_mock()
        mock_vio_get.side_effect = [vio3, vio3]
        self.assertRaises(npvmex.InstanceDiskMappingFailed,
                          ssp_stor.connect_instance_disk_to_mgmt, inst)
        mock_add.assert_not_called()

        # First add_vscsi_mapping call raises
        mock_vio_get.side_effect = [vio1, vio2]
        mock_add.side_effect = [Exception("mapping failed"), None]
        # Should hit on the second VIOS
        self.assertIs(vio2, vios)

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_lu_mapping')
    def test_disconnect_disk_from_mgmt(self, mock_rm_lu_map):
        ssp_stor = self._get_ssp_stor()
        ssp_stor.disconnect_disk_from_mgmt('vios_uuid', 'disk_name')
        mock_rm_lu_map.assert_called_with(ssp_stor.adapter, 'vios_uuid',
                                          'mp_uuid', disk_names=['disk_name'])
