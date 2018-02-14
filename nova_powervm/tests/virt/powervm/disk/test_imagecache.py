# Copyright 2018 IBM Corp.
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

from nova import test
from pypowervm.wrappers import storage as pvm_stor
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.virt.powervm.disk import imagecache


class TestImageCache(test.NoDBTestCase):
    """Unit Tests for the LocalDisk storage driver."""

    def setUp(self):
        super(TestImageCache, self).setUp()
        self.mock_vg = mock.MagicMock(virtual_disks=[])
        # Initialize the ImageManager
        self.adpt = mock.MagicMock()
        self.vg_uuid = 'vg_uuid'
        self.vios_uuid = 'vios_uuid'
        self.img_cache = imagecache.ImageManager(self.vios_uuid, self.vg_uuid,
                                                 self.adpt)

        # Setup virtual_disks to be used later
        self.inst1 = pvm_stor.VDisk.bld(None, 'b_inst1', 10)
        self.inst2 = pvm_stor.VDisk.bld(None, 'b_inst2', 10)
        self.image = pvm_stor.VDisk.bld(None, 'i_bf8446e4_4f52', 10)

    def test_get_base(self):
        self.mock_vg_get = self.useFixture(fixtures.MockPatch(
            'pypowervm.wrappers.storage.VG.get')).mock
        self.mock_vg_get.return_value = self.mock_vg
        vg_wrap = self.img_cache._get_base()
        self.assertEqual(vg_wrap, self.mock_vg)
        self.mock_vg_get.assert_called_once_with(
            self.adpt, uuid=self.vg_uuid,
            parent_type=pvm_vios.VIOS.schema_type, parent_uuid=self.vios_uuid)

    def test_scan_base_image(self):
        # No cached images
        self.mock_vg.virtual_disks = [self.inst1, self.inst2]
        base_images = self.img_cache._scan_base_image(self.mock_vg)
        self.assertEqual([], base_images)
        # One 'cached' image
        self.mock_vg.virtual_disks.append(self.image)
        base_images = self.img_cache._scan_base_image(self.mock_vg)
        self.assertEqual([self.image], base_images)

    @mock.patch('pypowervm.tasks.storage.rm_vg_storage')
    @mock.patch('nova.virt.imagecache.ImageCacheManager.'
                '_list_running_instances')
    @mock.patch('nova_powervm.virt.powervm.disk.imagecache.ImageManager.'
                '_scan_base_image')
    def test_age_and_verify(self, mock_scan, mock_list, mock_rm):
        mock_context = mock.MagicMock()
        all_inst = mock.MagicMock()
        mock_scan.return_value = [self.image]
        # Two instances backed by image 'bf8446e4_4f52'
        # Mock dict returned from _list_running_instances
        used_images = {'': [self.inst1, self.inst2],
                       'bf8446e4_4f52': [self.inst1, self.inst2]}
        mock_list.return_value = {'used_images': used_images}

        self.mock_vg.virtual_disks = [self.inst1, self.inst2, self.image]
        self.img_cache._age_and_verify_cached_images(mock_context, all_inst,
                                                     self.mock_vg)
        mock_rm.assert_not_called()
        mock_scan.assert_called_once_with(self.mock_vg)
        mock_rm.reset_mock()

        # No instances
        mock_list.return_value = {'used_images': {}}
        self.img_cache._age_and_verify_cached_images(mock_context, all_inst,
                                                     self.mock_vg)
        mock_rm.assert_called_once_with(self.mock_vg, vdisks=[self.image])

    @mock.patch('nova_powervm.virt.powervm.disk.imagecache.ImageManager.'
                '_get_base')
    @mock.patch('nova_powervm.virt.powervm.disk.imagecache.ImageManager.'
                '_age_and_verify_cached_images')
    def test_update(self, mock_age, mock_base):
        mock_base.return_value = self.mock_vg
        mock_context = mock.MagicMock()
        mock_all_inst = mock.MagicMock()
        self.img_cache.update(mock_context, mock_all_inst)
        mock_base.assert_called_once_with()
        mock_age.assert_called_once_with(mock_context, mock_all_inst,
                                         self.mock_vg)
