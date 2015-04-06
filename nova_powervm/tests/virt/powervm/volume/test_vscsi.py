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

from pypowervm.tasks import hdisk


class TestVSCSIAdapter(test.TestCase):
    """Tests the vSCSI Volume Connector Adapter."""

    def setUp(self):
        super(TestVSCSIAdapter, self).setUp()

    @mock.patch('pypowervm.tasks.hdisk.build_itls')
    @mock.patch('pypowervm.tasks.hdisk.discover_hdisk')
    @mock.patch('pypowervm.tasks.scsi_mapper.add_vscsi_mapping')
    @mock.patch('nova_powervm.virt.powervm.vios.get_vios_name_map')
    def test_connect_volume(self, mock_vio_name_map, mock_add_vscsi_mapping,
                            mock_discover_hdisk, mock_build_itls):
        con_info = {'data': {'initiator_target_map': {'i1': ['t1'],
                                                      'i2': ['t2', 't3']},
                    'target_lun': '1', 'volume_id': 'id'}}
        mock_discover_hdisk.return_value = (hdisk.LUA_STATUS_DEVICE_AVAILABLE,
                                            'devname', 'udid')
        mock_vio_name_map.return_value = {'vio_name': 'vio_uuid',
                                          'vio_name1': 'vio_uuid1'}
        vscsi.VscsiVolumeAdapter().connect_volume(None, 'host_uuid',
                                                  'vm_uuid', None, con_info)
        # Confirm mapping called twice for two defined VIOS
        self.assertEqual(2, mock_add_vscsi_mapping.call_count)

    @mock.patch('nova_powervm.virt.powervm.vios.get_physical_wwpns')
    def test_wwpns(self, mock_vio_wwpns):
        mock_vio_wwpns.return_value = ['aa', 'bb']

        vol_drv = vscsi.VscsiVolumeAdapter()
        wwpns = vol_drv.wwpns(mock.ANY, 'host_uuid', mock.ANY)

        self.assertListEqual(['aa', 'bb'], wwpns)
