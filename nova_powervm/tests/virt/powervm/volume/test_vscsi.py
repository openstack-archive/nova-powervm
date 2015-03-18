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

from nova_powervm.virt.powervm.volume import vscsi


class TestVSCSIAdapter(test.TestCase):
    """Tests the vSCSI Volume Connector Adapter."""

    def setUp(self):
        super(TestVSCSIAdapter, self).setUp()

    @mock.patch('pypowervm.tasks.hdisk.build_itls')
    @mock.patch('pypowervm.tasks.hdisk.discover_hdisk')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VSCSIMapping.bld_to_pv')
    @mock.patch('nova_powervm.virt.powervm.vios.add_vscsi_mapping')
    def test_connect_volume(self, mock_add_vscsi_mapping, mock_bld_to_pv,
                            mock_discover_hdisk, mock_build_itls):
        con_info = {'data': {'initiator_target_map': {'i': ['t'],
                                                      'i': ['t']}},
                    'target_lun': '1'}
        mock_discover_hdisk.return_value = '1' '2' '3'
        vscsi.VscsiVolumeAdapter().connect_volume(None, 'host_uuid',
                                                  'vios_uuid', 'vm_uuid',
                                                  'vios_name', None, con_info)
        self.assertEqual(1, mock_add_vscsi_mapping.call_count)

    @mock.patch('nova_powervm.virt.powervm.vios.get_physical_wwpns')
    def test_wwpns(self, mock_vio_wwpns):
        mock_vio_wwpns.return_value = ['aa', 'bb']

        vol_drv = vscsi.VscsiVolumeAdapter()
        wwpns = vol_drv.wwpns(mock.ANY, 'host_uuid', mock.ANY)

        self.assertListEqual(['aa', 'bb'], wwpns)
