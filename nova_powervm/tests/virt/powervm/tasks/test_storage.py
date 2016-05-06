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

from nova_powervm.virt.powervm import exception as npvmex
from nova_powervm.virt.powervm.tasks import storage as tf_stg


class TestStorage(test.TestCase):

    @mock.patch('pypowervm.tasks.scsi_mapper.find_maps')
    @mock.patch('nova_powervm.virt.powervm.mgmt.discover_vscsi_disk')
    @mock.patch('nova_powervm.virt.powervm.mgmt.remove_block_dev')
    def test_instance_disk_to_mgmt(self, mock_rm, mock_discover, mock_find):
        mock_discover.return_value = '/dev/disk'
        mock_instance = mock.Mock()
        mock_instance.name = 'instance_name'
        mock_stg = mock.Mock()
        mock_stg.name = 'stg_name'
        mock_vwrap = mock.Mock()
        mock_vwrap.name = 'vios_name'
        mock_vwrap.uuid = 'vios_uuid'
        mock_vwrap.scsi_mappings = ['mapping1']

        disk_dvr = mock.MagicMock()
        disk_dvr.mp_uuid = 'mp_uuid'
        disk_dvr.connect_instance_disk_to_mgmt.return_value = (mock_stg,
                                                               mock_vwrap)

        def reset_mocks():
            mock_find.reset_mock()
            mock_discover.reset_mock()
            mock_rm.reset_mock()
            disk_dvr.reset_mock()

        # Good path - find_maps returns one result
        mock_find.return_value = ['one_mapping']
        tf = tf_stg.InstanceDiskToMgmt(disk_dvr, mock_instance)
        self.assertEqual('connect_and_discover_instance_disk_to_mgmt', tf.name)
        self.assertEqual((mock_stg, mock_vwrap, '/dev/disk'), tf.execute())
        disk_dvr.connect_instance_disk_to_mgmt.assert_called_with(
            mock_instance)
        mock_find.assert_called_with(['mapping1'], client_lpar_id='mp_uuid',
                                     stg_elem=mock_stg)
        mock_discover.assert_called_with('one_mapping')
        tf.revert('result', 'failures')
        disk_dvr.disconnect_disk_from_mgmt.assert_called_with('vios_uuid',
                                                              'stg_name')
        mock_rm.assert_called_with('/dev/disk')

        # Good path - find_maps returns >1 result
        reset_mocks()
        mock_find.return_value = ['first_mapping', 'second_mapping']
        tf = tf_stg.InstanceDiskToMgmt(disk_dvr, mock_instance)
        self.assertEqual((mock_stg, mock_vwrap, '/dev/disk'), tf.execute())
        disk_dvr.connect_instance_disk_to_mgmt.assert_called_with(
            mock_instance)
        mock_find.assert_called_with(['mapping1'], client_lpar_id='mp_uuid',
                                     stg_elem=mock_stg)
        mock_discover.assert_called_with('first_mapping')
        tf.revert('result', 'failures')
        disk_dvr.disconnect_disk_from_mgmt.assert_called_with('vios_uuid',
                                                              'stg_name')
        mock_rm.assert_called_with('/dev/disk')

        # Bad path - find_maps returns no results
        reset_mocks()
        mock_find.return_value = []
        tf = tf_stg.InstanceDiskToMgmt(disk_dvr, mock_instance)
        self.assertRaises(npvmex.NewMgmtMappingNotFoundException, tf.execute)
        disk_dvr.connect_instance_disk_to_mgmt.assert_called_with(
            mock_instance)
        # find_maps was still called
        mock_find.assert_called_with(['mapping1'], client_lpar_id='mp_uuid',
                                     stg_elem=mock_stg)
        # discover_vscsi_disk didn't get called
        self.assertEqual(0, mock_discover.call_count)
        tf.revert('result', 'failures')
        # disconnect_disk_from_mgmt got called
        disk_dvr.disconnect_disk_from_mgmt.assert_called_with('vios_uuid',
                                                              'stg_name')
        # ...but remove_block_dev did not.
        self.assertEqual(0, mock_rm.call_count)

        # Bad path - connect raises
        reset_mocks()
        disk_dvr.connect_instance_disk_to_mgmt.side_effect = (
            npvmex.InstanceDiskMappingFailed(instance_name='inst_name'))
        tf = tf_stg.InstanceDiskToMgmt(disk_dvr, mock_instance)
        self.assertRaises(npvmex.InstanceDiskMappingFailed, tf.execute)
        disk_dvr.connect_instance_disk_to_mgmt.assert_called_with(
            mock_instance)
        self.assertEqual(0, mock_find.call_count)
        self.assertEqual(0, mock_discover.call_count)
        # revert shouldn't call disconnect or remove
        tf.revert('result', 'failures')
        self.assertEqual(0, disk_dvr.disconnect_disk_from_mgmt.call_count)
        self.assertEqual(0, mock_rm.call_count)

    @mock.patch('nova_powervm.virt.powervm.mgmt.remove_block_dev')
    def test_remove_instance_disk_from_mgmt(self, mock_rm):
        disk_dvr = mock.MagicMock()
        mock_instance = mock.Mock()
        mock_instance.name = 'instance_name'
        mock_stg = mock.Mock()
        mock_stg.name = 'stg_name'
        mock_vwrap = mock.Mock()
        mock_vwrap.name = 'vios_name'
        mock_vwrap.uuid = 'vios_uuid'

        tf = tf_stg.RemoveInstanceDiskFromMgmt(disk_dvr, mock_instance)
        self.assertEqual('remove_inst_disk_from_mgmt', tf.name)
        tf.execute(mock_stg, mock_vwrap, '/dev/disk')
        disk_dvr.disconnect_disk_from_mgmt.assert_called_with('vios_uuid',
                                                              'stg_name')
        mock_rm.assert_called_with('/dev/disk')

    def test_finddisk(self):
        disk_dvr = mock.Mock()
        disk_dvr.get_disk_ref.return_value = 'disk_ref'
        instance = mock.Mock()
        context = 'context'
        disk_type = 'disk_type'

        task = tf_stg.FindDisk(disk_dvr, context, instance, disk_type)
        ret_disk = task.execute()
        disk_dvr.get_disk_ref.assert_called_once_with(instance, disk_type)
        self.assertEqual('disk_ref', ret_disk)

        # Bad path for no disk found
        disk_dvr.reset_mock()
        disk_dvr.get_disk_ref.return_value = None
        ret_disk = task.execute()
        disk_dvr.get_disk_ref.assert_called_once_with(instance, disk_type)
        self.assertIsNone(ret_disk)

    def test_extenddisk(self):
        disk_dvr = mock.Mock()
        instance = mock.Mock()
        context = 'context'
        disk_info = {'type': 'disk_type'}

        task = tf_stg.ExtendDisk(disk_dvr, context, instance, disk_info, 1024)
        task.execute()
        disk_dvr.extend_disk.assert_called_once_with(context, instance,
                                                     disk_info, 1024)

    def test_connect_volume(self):
        vol_dvr = mock.Mock(connection_info={'data': {'volume_id': '1'}})

        task = tf_stg.ConnectVolume(vol_dvr, 'slot map')
        task.execute()
        vol_dvr.connect_volume.assert_called_once_with('slot map')

        task.revert('result', 'flow failures')
        vol_dvr.reset_stg_ftsk.assert_called_once_with()
        vol_dvr.disconnect_volume.assert_called_once_with('slot map')

    def test_disconnect_volume(self):
        vol_dvr = mock.Mock(connection_info={'data': {'volume_id': '1'}})

        task = tf_stg.DisconnectVolume(vol_dvr, 'slot map')
        task.execute()
        vol_dvr.disconnect_volume.assert_called_once_with('slot map')

        task.revert('result', 'flow failures')
        vol_dvr.reset_stg_ftsk.assert_called_once_with()
        vol_dvr.connect_volume.assert_called_once_with('slot map')
