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

import mock

from nova import exception as nova_exc
from nova import objects
from nova import test
import os
from pypowervm.tests.wrappers.util import pvmhttp
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.tests.virt import powervm
from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm.disk import driver as disk_dvr
from nova_powervm.virt.powervm.disk import localdisk as ld


VOL_GRP_WITH_VIOS = 'fake_volume_group_with_vio_data.txt'
VIOS_WITH_VOL_GRP = 'fake_vios_with_volume_group_data.txt'


class TestLocalDisk(test.TestCase):
    """Unit Tests for the LocalDisk storage driver."""

    def setUp(self):
        super(TestLocalDisk, self).setUp()

        # Find directory for response file(s)
        data_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(data_dir, "..", 'data')

        def resp(file_name):
            file_path = os.path.join(data_dir, file_name)
            return pvmhttp.load_pvm_resp(file_path).get_response()

        self.vg_to_vio = resp(VOL_GRP_WITH_VIOS)
        self.vio_to_vg = resp(VIOS_WITH_VOL_GRP)
        self.pypvm = self.useFixture(fx.PyPowerVM())
        self.apt = self.pypvm.apt

        # Set up for the mocks for get_ls
        self.mock_vio_name_map_p = mock.patch('nova_powervm.virt.powervm.vios.'
                                              'get_vios_name_map')
        self.mock_vio_name_map = self.mock_vio_name_map_p.start()
        self.mock_vio_name_map.return_value = {'vio_name': 'vio_uuid'}

        self.mock_vg_uuid_p = mock.patch('nova_powervm.virt.powervm.disk.'
                                         'localdisk.LocalStorage.'
                                         '_get_vg_uuid')
        self.mock_vg_uuid = self.mock_vg_uuid_p.start()
        self.mock_vg_uuid.return_value = 'd5065c2c-ac43-3fa6-af32-ea84a3960291'

    def tearDown(self):
        test.TestCase.tearDown(self)

        # Tear down mocks
        self.mock_vg_uuid_p.stop()
        self.mock_vio_name_map_p.stop()

    def get_ls(self, adpt):
        return ld.LocalStorage({'adapter': adpt, 'host_uuid': 'host_uuid'})

    @mock.patch('pypowervm.tasks.storage.upload_new_vdisk')
    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage.'
                '_get_disk_name')
    @mock.patch('nova_powervm.virt.powervm.disk.driver.'
                'IterableToFileAdapter')
    @mock.patch('nova.image.API')
    def test_create_disk_from_image(self, mock_img_api, mock_file_adpt,
                                    mock_get_dname, mock_upload_vdisk):
        mock_img = {'id': 'fake_id', 'size': 50}
        mock_get_dname.return_value = 'fake_vol'
        mock_upload_vdisk.return_value = ('vdisk', 'f_uuid')

        vdisk = self.get_ls(self.apt).create_disk_from_image(
            None, None, mock_img, 20)
        mock_upload_vdisk.assert_called_with(mock.ANY, mock.ANY, mock.ANY,
                                             mock.ANY, 'fake_vol', 50,
                                             d_size=21474836480L)
        self.assertEqual('vdisk', vdisk)

    @mock.patch('pypowervm.wrappers.storage.VG')
    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage.'
                '_get_vg')
    def test_capacity(self, mock_get_vg, mock_vg):
        """Tests the capacity methods."""

        # Set up the mock data.  This will simulate our vg wrapper
        mock_vg_wrap = mock.MagicMock(name='vg_wrapper')
        type(mock_vg_wrap).capacity = mock.PropertyMock(return_value='5120')
        type(mock_vg_wrap).available_size = mock.PropertyMock(
            return_value='2048')

        mock_vg.wrap.return_value = mock_vg_wrap
        local = self.get_ls(self.apt)

        self.assertEqual(5120.0, local.capacity)
        self.assertEqual(3072.0, local.capacity_used)

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_vdisk_mapping')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_disconnect_image_disk(self, mock_get_vm_id, mock_remove):
        """Tests the disconnect_image_disk method."""
        # Set up the mock data.
        mock_get_vm_id.return_value = '2'

        local = self.get_ls(self.apt)
        local.disconnect_image_disk(mock.MagicMock(), mock.MagicMock(), '2')

        # Validate
        mock_remove.assert_called_once_with(mock.ANY, mock.ANY, '2',
                                            disk_prefixes=None)
        self.assertEqual(1, mock_remove.call_count)

    @mock.patch('pypowervm.tasks.scsi_mapper.remove_vdisk_mapping')
    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id')
    def test_disconnect_image_disk_disktype(self, mock_get_vm_id, mock_remove):
        """Tests the disconnect_image_disk method."""
        # Set up the mock data.
        mock_get_vm_id.return_value = '2'

        # Invoke
        local = self.get_ls(self.apt)
        local.disconnect_image_disk(mock.MagicMock(), mock.MagicMock(), '2',
                                    disk_type=[disk_dvr.BOOT_DISK])

        # Validate
        mock_remove.assert_called_once_with(mock.ANY, mock.ANY, '2',
                                            disk_prefixes=[disk_dvr.BOOT_DISK])
        self.assertEqual(1, mock_remove.call_count)

    @mock.patch('pypowervm.tasks.scsi_mapper.add_vscsi_mapping')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VSCSIMapping.'
                '_client_lpar_href')
    @mock.patch('nova_powervm.virt.powervm.vios.get_vios_wrap')
    def test_connect_disk(self, mock_vios_wrap, mock_lpar_href,
                          mock_add_mapping):
        mock_lpar_href.return_value = 'client_lpar_href'
        mock_vios_wrap.return_value = pvm_vios.VIOS.wrap(self.vio_to_vg)

        mock_vdisk = mock.MagicMock()
        mock_vdisk.name = 'vdisk'

        ls = self.get_ls(self.apt)
        ls.connect_disk(mock.MagicMock(), mock.MagicMock(),
                        mock_vdisk, 'lpar_UUID')
        self.assertEqual(1, mock_add_mapping.call_count)

    @mock.patch('pypowervm.wrappers.storage.VG.update')
    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage.'
                '_get_vg_wrap')
    def test_delete_disks(self, mock_vg, mock_update):
        # Mocks
        self.apt.side_effect = [self.vg_to_vio]

        mock_remove = mock.MagicMock()
        mock_remove.name = 'disk'

        mock_wrapper = mock.MagicMock()
        mock_wrapper.virtual_disks = [mock_remove]
        mock_vg.return_value = mock_wrapper

        # Invoke the call
        local = self.get_ls(self.apt)
        local.delete_disks(mock.MagicMock(), mock.MagicMock(),
                           [mock_remove])

        # Validate the call
        self.assertEqual(1, mock_wrapper.update.call_count)
        self.assertEqual(0, len(mock_wrapper.virtual_disks))

    @mock.patch('pypowervm.wrappers.storage.VG')
    @mock.patch('nova_powervm.virt.powervm.disk.localdisk.LocalStorage.'
                '_get_disk_name')
    def test_extend_disk(self, mock_dsk_name, mock_vg):
        local = self.get_ls(self.apt)

        inst = objects.Instance(**powervm.TEST_INSTANCE)

        vdisk = mock.Mock(name='vdisk')
        vdisk.name = 'disk_name'

        resp = mock.Mock(name='response')
        resp.virtual_disks = [vdisk]
        mock_vg.wrap.return_value = resp

        mock_dsk_name.return_value = 'NOMATCH'
        self.assertRaises(nova_exc.DiskNotFound, local.extend_disk,
                          'context', inst, dict(type='boot'), 10)

        mock_dsk_name.return_value = 'disk_name'
        local.extend_disk('context', inst, dict(type='boot'), 1000)

        # Validate the call
        self.assertEqual(1, resp.update.call_count)
        self.assertEqual(vdisk.capacity, 1000)
