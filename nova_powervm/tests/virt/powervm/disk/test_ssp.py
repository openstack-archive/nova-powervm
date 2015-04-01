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
from oslo_config import cfg

from nova import test
import os
import pypowervm.adapter as pvm_adp
from pypowervm.tests.wrappers.util import pvmhttp
from pypowervm.wrappers import cluster as pvm_clust
from pypowervm.wrappers import storage as pvm_stg

from nova_powervm.tests.virt.powervm import fixtures as fx
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
            uuid = 'instance_uuid'

        self.instance = Instance()

        # Find directory for response file(s)
        data_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(data_dir, "..", 'data')

        def resp(file_name):
            file_path = os.path.join(data_dir, file_name)
            return pvmhttp.load_pvm_resp(file_path).get_response()

        self.ssp_resp = resp(SSP)
        self.clust_resp = resp(CLUST)
        self.pypvm = self.useFixture(fx.PyPowerVM())
        self.apt = self.pypvm.apt

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
        ssp_stor = ssp.SSPDiskAdapter({'adapter': self.apt,
                                       'host_uuid': 'host_uuid'})
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
        resp.entry = None
        resp.feed = None
        if entry_or_list is None:
            resp.feed = pvm_adp.Feed({}, [])
        else:
            if isinstance(entry_or_list, list):
                resp.feed = pvm_adp.Feed({}, entry_or_list)
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
        clust1 = pvm_clust.Cluster.bld('newclust1',
                                       pvm_stg.PV.bld('hdisk1'),
                                       pvm_clust.Node.bld('vios1'))
        clust2 = pvm_clust.Cluster.bld('newclust2',
                                       pvm_stg.PV.bld('hdisk2'),
                                       pvm_clust.Node.bld('vios2'))
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
        clust1 = pvm_clust.Cluster.bld('newclust1',
                                       pvm_stg.PV.bld('hdisk1'),
                                       pvm_clust.Node.bld('vios1'))
        clust2 = pvm_clust.Cluster.bld('newclust2',
                                       pvm_stg.PV.bld('hdisk2'),
                                       pvm_clust.Node.bld('vios2'))
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
        vios_uuids = ssp_stor._vios_uuids
        self.assertEqual(['58C9EB1D-7213-4956-A011-77D43CC4ACCC',
                          '6424120D-CA95-437D-9C18-10B06F4B3400'], vios_uuids)
        s = set()
        for i in range(1000):
            u = ssp_stor._any_vios_uuid
            # Make sure we got a good value
            self.assertIn(u, vios_uuids)
            s.add(u)
        # Make sure we hit all the values over 1000 iterations.  This isn't
        # guaranteed to work, but the odds of failure should be infinitesimal.
        self.assertEqual(set(vios_uuids), s)

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
        img = dict(id='image-id', size=b2G)

        def verify_upload_new_lu(adap, vios_uuid, ssp1, stream, lu_name,
                                 f_size):
            self.assertIn(vios_uuid, ssp_stor._vios_uuids)
            self.assertEqual(ssp_stor._ssp_wrap, ssp1)
            # 'image' + '_' + s/-/_/g(image['id']), per _get_image_name
            self.assertEqual('image_image_id', lu_name)
            self.assertEqual(b2G, f_size)
            return 'image_lu', None

        def verify_create_lu_linked_clone(adap, ssp1, clust1, imglu, lu_name,
                                          sz_gb):
            # 'boot'[:6] + '_' + 'instance_uuid'[:8], per _get_disk_name
            self.assertEqual('boot_instance', lu_name)
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
        img = dict(id='image-id', size=b2G)
        # Mock the 'existing' image LU
        img_lu = pvm_stg.LU.bld('image_image_id', 123,
                                typ=pvm_stg.LUTypeEnum.IMAGE)
        ssp_stor._ssp_wrap.logical_units.append(img_lu)

        class Instance(object):
            uuid = 'instance_uuid'

        def verify_create_lu_linked_clone(adap, ssp1, clust1, imglu, lu_name,
                                          sz_gb):
            # 'boot'[:6] + '_' + 'instance_uuid'[:8], per _get_disk_name
            self.assertEqual('boot_instance', lu_name)
            self.assertEqual(img_lu, imglu)
            return ssp1, 'new_lu'

        mock_crt_lnk_cln.side_effect = verify_create_lu_linked_clone
        lu = ssp_stor.create_disk_from_image(None, Instance(), img, 1)
        self.assertEqual('new_lu', lu)

    @mock.patch('pypowervm.wrappers.virtual_io_server.VSCSIMapping.'
                '_client_lpar_href')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VSCSIMapping.bld_to_lu')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_vscsi_mapping')
    def test_connect_disk(self, mock_add_map, mock_bld_to_lu, mock_href):
        mock_bld_to_lu.return_value = 'vscsi_mapping'
        ssp_stor = self._get_ssp_stor()
        lu = ssp_stor._ssp_wrap.logical_units[0]
        ssp_stor.connect_disk(None, self.instance, lu, 'lpar_uuid')
        self.assertEqual(1, mock_bld_to_lu.call_count)
        mock_bld_to_lu.assert_called_with(self.apt, 'host_uuid', 'lpar_uuid',
                                          lu.udid, lu.name)
        self.assertEqual(2, mock_add_map.call_count)
        for vu in ssp_stor._vios_uuids:
            mock_add_map.assert_any_call(self.apt, vu, 'vscsi_mapping')
