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

from nova import test
import os
from pypowervm import adapter as adpt
from pypowervm.tests.wrappers.util import pvmhttp
from pypowervm.wrappers import virtual_io_server as vios_w

from nova_powervm.virt.powervm import localdisk as ld


VOL_GRP_WITH_VIOS = 'fake_volume_group_with_vio_data.txt'
VIOS_WITH_VOL_GRP = 'fake_vios_with_volume_group_data.txt'


class TestLocalDisk(test.TestCase):
    """Unit Tests for the LocalDisk storage driver."""

    def setUp(self):
        super(TestLocalDisk, self).setUp()

        # Find directory for response file(s)
        data_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(data_dir, 'data')

        def resp(file_name):
            file_path = os.path.join(data_dir, file_name)
            return pvmhttp.load_pvm_resp(file_path).get_response()

        self.vg_to_vio = resp(VOL_GRP_WITH_VIOS)
        self.vio_to_vg = resp(VIOS_WITH_VOL_GRP)

    @mock.patch('nova_powervm.virt.powervm.localdisk.LocalStorage.'
                '_get_vg_uuid')
    @mock.patch('pypowervm.adapter.Adapter')
    def test_disconnect_image_volume(self, mock_adpt, mock_vg_uuid):
        """Tests the disconnect_image_volume method."""

        # Set up the mock data.
        class FakeQuickProp():
            @property
            def body(self):
                return '2'

        mock_vg_uuid.return_value = 'd5065c2c-ac43-3fa6-af32-ea84a3960291'

        mock_adpt.read.side_effect = [self.vio_to_vg, FakeQuickProp()]

        def validate_update(*kargs, **kwargs):
            # Make sure that the mappings are only 1 (the remaining vopt)
            self.assertEqual([vios_w.XAG_VIOS_SCSI_MAPPING], kwargs['xag'])
            vio = vios_w.VirtualIOServer(adpt.Entry({}, kargs[0]))
            self.assertEqual(1, len(vio.scsi_mappings))
        mock_adpt.update.side_effect = validate_update

        local = ld.LocalStorage({'adapter': mock_adpt,
                                 'host_uuid': 'host_uuid',
                                 'vios_name': 'vios_name',
                                 'vios_uuid': 'vios_uuid'})
        local.disconnect_image_volume(mock.MagicMock(), mock.MagicMock(), '2')
        self.assertEqual(1, mock_adpt.update.call_count)

    @mock.patch('nova_powervm.virt.powervm.localdisk.LocalStorage.'
                '_get_vg_uuid')
    @mock.patch('pypowervm.adapter.Adapter')
    def test_delete_volumes(self, mock_adpt, mock_vg_uuid):
        # Mocks
        mock_adpt.side_effect = [self.vg_to_vio]

        # Find from the data the vDisk scsi mapping
        scsi_mapping = mock.MagicMock()
        scsi_mapping.udid = '0300025d4a00007a000000014b36d9deaf.1'

        # Invoke the call
        local = ld.LocalStorage({'adapter': mock_adpt,
                                 'host_uuid': 'host_uuid',
                                 'vios_name': 'vios_name',
                                 'vios_uuid': 'vios_uuid'})
        local.delete_volumes(mock.MagicMock(), mock.MagicMock(),
                             [scsi_mapping])

        # Validate the call
        self.assertEqual(1, mock_adpt.update.call_count)
