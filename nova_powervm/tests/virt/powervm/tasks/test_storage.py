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

        def verify_connect(inst):
            self.assertEqual(mock_instance, inst)
            return mock_stg, mock_vwrap

        def verify_disconnect(vios_uuid, stg_name):
            self.assertEqual('vios_uuid', vios_uuid)
            self.assertEqual('stg_name', stg_name)

        disk_dvr = mock.MagicMock()
        disk_dvr.mp_uuid = 'mp_uuid'
        disk_dvr.connect_instance_disk_to_mgmt = verify_connect
        disk_dvr.disconnect_disk_from_mgmt = verify_disconnect

        # Good path - find_maps returns one result
        mock_find.return_value = ['one_mapping']
        tf = tf_stg.InstanceDiskToMgmt(disk_dvr, mock_instance)
        self.assertEqual('connect_and_discover_instance_disk_to_mgmt', tf.name)
        self.assertEqual((mock_stg, mock_vwrap, '/dev/disk'), tf.execute())
        mock_find.assert_called_with(['mapping1'], 'mp_uuid',
                                     stg_elem=mock_stg)
        mock_discover.assert_called_with('one_mapping')
        tf.revert('result', 'failures')
        mock_rm.assert_called_with('/dev/disk')

        # Good path - find_maps returns >1 result
        mock_find.reset_mock()
        mock_discover.reset_mock()
        mock_rm.reset_mock()
        mock_find.return_value = ['first_mapping', 'second_mapping']
        tf = tf_stg.InstanceDiskToMgmt(disk_dvr, mock_instance)
        self.assertEqual((mock_stg, mock_vwrap, '/dev/disk'), tf.execute())
        mock_find.assert_called_with(['mapping1'], 'mp_uuid',
                                     stg_elem=mock_stg)
        mock_discover.assert_called_with('first_mapping')
        tf.revert('result', 'failures')
        mock_rm.assert_called_with('/dev/disk')

        # Bad path - find_maps returns no results
        mock_find.reset_mock()
        mock_discover.reset_mock()
        mock_rm.reset_mock()
        mock_find.return_value = []
        tf = tf_stg.InstanceDiskToMgmt(disk_dvr, mock_instance)
        self.assertRaises(npvmex.NewMgmtMappingNotFoundException, tf.execute)
        # find_maps was still called
        mock_find.assert_called_with(['mapping1'], 'mp_uuid',
                                     stg_elem=mock_stg)
        # discover_vscsi_disk didn't get called
        self.assertEqual(0, mock_discover.call_count)
        tf.revert('result', 'failures')
        # disconnect_disk_from_mgmt got called (still checked by
        # verify_disconnect above), but remove_block_dev did not.
        self.assertEqual(0, mock_rm.call_count)

        # Bad path - connect raises
        mock_find.reset_mock()
        mock_discover.reset_mock()
        mock_rm.reset_mock()
        disk_dvr.connect_instance_disk_to_mgmt = mock.Mock(
            side_effect=npvmex.InstanceDiskMappingFailed(
                instance_name='inst_name'))
        tf = tf_stg.InstanceDiskToMgmt(disk_dvr, mock_instance)
        self.assertRaises(npvmex.InstanceDiskMappingFailed, tf.execute)
        self.assertEqual(0, mock_find.call_count)
        self.assertEqual(0, mock_discover.call_count)
        # revert shouldn't call disconnect or remove
        disk_dvr.disconnect_disk_from_mgmt = mock.Mock(side_effect=self.fail)
        tf.revert('result', 'failures')
        self.assertEqual(0, mock_rm.call_count)
