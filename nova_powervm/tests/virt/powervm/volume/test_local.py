# Copyright 2017 IBM Corp.
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

from nova_powervm.tests.virt.powervm.volume import test_driver as test_vol
from nova_powervm.virt.powervm.volume import local as v_drv


class TestLocalVolumeAdapter(test_vol.TestVolumeAdapter):
    """Tests the LocalVolumeAdapter.  NovaLink is a I/O host."""

    def setUp(self):
        super(TestLocalVolumeAdapter, self).setUp()

        # Needed for the volume adapter
        self.adpt = mock.Mock()
        mock_inst = mock.MagicMock(uuid='2BC123')

        # Connection Info
        mock_conn_info = {'data': {'device_path': '/local/path'}}

        self.vol_drv = v_drv.LocalVolumeAdapter(
            self.adpt, 'host_uuid', mock_inst, mock_conn_info)

    def test_get_path(self):
        self.assertEqual('/local/path', self.vol_drv._get_path())
