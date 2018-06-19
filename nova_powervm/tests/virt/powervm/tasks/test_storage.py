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

from nova import test

from nova_powervm.virt.powervm import exception as npvmex
from nova_powervm.virt.powervm.tasks import storage as tf_stg


class TestStorage(test.NoDBTestCase):

    def setUp(self):
        super(TestStorage, self).setUp()

        self.adapter = mock.Mock()
        self.disk_dvr = mock.MagicMock()
        self.mock_cfg_drv = self.useFixture(fixtures.MockPatch(
            'nova_powervm.virt.powervm.media.ConfigDrivePowerVM')).mock
        self.mock_mb = self.mock_cfg_drv.return_value
        self.instance = mock.MagicMock()
        self.context = 'context'

    def test_create_and_connect_cfg_drive(self):
        lpar_w = mock.Mock()

        # Test with no FeedTask
        task = tf_stg.CreateAndConnectCfgDrive(
            self.adapter, self.instance, 'injected_files',
            'network_info', 'admin_pass')
        task.execute(lpar_w, 'mgmt_cna')
        self.mock_cfg_drv.assert_called_once_with(self.adapter)
        self.mock_mb.create_cfg_drv_vopt.assert_called_once_with(
            self.instance, 'injected_files', 'network_info', lpar_w.uuid,
            admin_pass='admin_pass', mgmt_cna='mgmt_cna', stg_ftsk=None)

        self.mock_cfg_drv.reset_mock()
        self.mock_mb.reset_mock()

        # Normal revert
        task.revert(lpar_w, 'mgmt_cna', 'result', 'flow_failures')
        self.mock_mb.dlt_vopt.assert_called_once_with(lpar_w.uuid)

        self.mock_mb.reset_mock()

        # Revert when dlt_vopt fails
        self.mock_mb.dlt_vopt.side_effect = Exception('fake-exc')
        task.revert(lpar_w, 'mgmt_cna', 'result', 'flow_failures')
        self.mock_mb.dlt_vopt.assert_called_once_with(lpar_w.uuid)

        self.mock_mb.reset_mock()

        # With a specified FeedTask
        task = tf_stg.CreateAndConnectCfgDrive(
            self.adapter, self.instance, 'injected_files',
            'network_info', 'admin_pass', stg_ftsk='stg_ftsk')
        task.execute(lpar_w, 'mgmt_cna')
        self.mock_cfg_drv.assert_called_once_with(self.adapter)
        self.mock_mb.create_cfg_drv_vopt.assert_called_once_with(
            self.instance, 'injected_files', 'network_info', lpar_w.uuid,
            admin_pass='admin_pass', mgmt_cna='mgmt_cna', stg_ftsk='stg_ftsk')

        # Revert when media builder not created
        task.mb = None
        task.revert(lpar_w, 'mgmt_cna', 'result', 'flow_failures')
        self.mock_mb.assert_not_called()

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_stg.CreateAndConnectCfgDrive(
                self.adapter, self.instance, 'injected_files', 'network_info',
                'admin_pass')
        tf.assert_called_once_with(name='cfg_drive', requires=['lpar_wrap',
                                   'mgmt_cna'])

    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid', autospec=True)
    def test_delete_vopt(self, mock_pvm_uuid):
        # Test with no FeedTask
        mock_pvm_uuid.return_value = 'pvm_uuid'
        task = tf_stg.DeleteVOpt(self.adapter, self.instance)
        task.execute()
        self.mock_cfg_drv.assert_called_once_with(self.adapter)
        self.mock_mb.dlt_vopt.assert_called_once_with(
            'pvm_uuid', stg_ftsk=None)

        self.mock_cfg_drv.reset_mock()
        self.mock_mb.reset_mock()

        # With a specified FeedTask
        task = tf_stg.DeleteVOpt(self.adapter, self.instance,
                                 stg_ftsk='ftsk')
        task.execute()
        self.mock_cfg_drv.assert_called_once_with(self.adapter)
        self.mock_mb.dlt_vopt.assert_called_once_with(
            'pvm_uuid', stg_ftsk='ftsk')

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_stg.DeleteVOpt(self.adapter, self.instance)
        tf.assert_called_once_with(name='vopt_delete')

    def test_delete_disk(self):
        stor_adpt_mappings = mock.Mock()

        task = tf_stg.DeleteDisk(self.disk_dvr, self.instance)
        task.execute(stor_adpt_mappings)
        self.disk_dvr.delete_disks.assert_called_once_with(stor_adpt_mappings)

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_stg.DeleteDisk(self.disk_dvr, self.instance)
        tf.assert_called_once_with(
            name='dlt_storage', requires=['stor_adpt_mappings'])

    def test_detach_disk(self):
        disk_type = 'disk_type'
        stg_ftsk = mock.Mock()

        task = tf_stg.DetachDisk(
            self.disk_dvr, self.instance, stg_ftsk=stg_ftsk,
            disk_type=disk_type)
        task.execute()
        self.disk_dvr.disconnect_disk.assert_called_once_with(
            self.instance, stg_ftsk=stg_ftsk, disk_type=disk_type)

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_stg.DetachDisk(self.disk_dvr, self.instance)
        tf.assert_called_once_with(
            name='detach_storage', provides='stor_adpt_mappings')

    def test_connect_disk(self):
        stg_ftsk = mock.Mock()
        disk_dev_info = mock.Mock()

        task = tf_stg.ConnectDisk(
            self.disk_dvr, self.instance, stg_ftsk=stg_ftsk)
        task.execute(disk_dev_info)
        self.disk_dvr.connect_disk.assert_called_once_with(
            self.instance, disk_dev_info, stg_ftsk=stg_ftsk)

        task.revert(disk_dev_info, 'result', 'flow failures')
        self.disk_dvr.disconnect_disk.assert_called_once_with(self.instance)

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_stg.ConnectDisk(self.disk_dvr, self.instance)
        tf.assert_called_once_with(
            name='connect_disk', requires=['disk_dev_info'])

    def test_create_disk_for_img(self):
        image_meta = mock.Mock()
        image_type = mock.Mock()

        task = tf_stg.CreateDiskForImg(
            self.disk_dvr, self.context, self.instance, image_meta,
            image_type=image_type)
        task.execute()
        self.disk_dvr.create_disk_from_image.assert_called_once_with(
            self.context, self.instance, image_meta, image_type=image_type)

        task.revert('result', 'flow failures')
        self.disk_dvr.delete_disks.assert_called_once_with(['result'])

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_stg.CreateDiskForImg(
                self.disk_dvr, self.context, self.instance, image_meta)
        tf.assert_called_once_with(
            name='crt_disk_from_img', provides='disk_dev_info')

    @mock.patch('pypowervm.tasks.scsi_mapper.find_maps', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.mgmt.discover_vscsi_disk',
                autospec=True)
    @mock.patch('nova_powervm.virt.powervm.mgmt.remove_block_dev',
                autospec=True)
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
        self.assertEqual('instance_disk_to_mgmt', tf.name)
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

        # Management Partition is VIOS and NovaLink hosted storage
        reset_mocks()
        disk_dvr.vios_uuids = ['mp_uuid']
        dev_name = '/dev/vg/fake_name'
        disk_dvr.get_bootdisk_path.return_value = dev_name
        tf = tf_stg.InstanceDiskToMgmt(disk_dvr, mock_instance)
        self.assertEqual((None, None, dev_name), tf.execute())

        # Management Partition is VIOS and not NovaLink hosted storage
        reset_mocks()
        disk_dvr.vios_uuids = ['mp_uuid']
        disk_dvr.get_bootdisk_path.return_value = None
        tf = tf_stg.InstanceDiskToMgmt(disk_dvr, mock_instance)
        tf.execute()
        disk_dvr.connect_instance_disk_to_mgmt.assert_called_with(
            mock_instance)

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

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_stg.InstanceDiskToMgmt(disk_dvr, mock_instance)
        tf.assert_called_once_with(
            name='instance_disk_to_mgmt',
            provides=['stg_elem', 'vios_wrap', 'disk_path'])

    @mock.patch('nova_powervm.virt.powervm.mgmt.remove_block_dev',
                autospec=True)
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

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_stg.RemoveInstanceDiskFromMgmt(disk_dvr, mock_instance)
        tf.assert_called_once_with(
            name='remove_inst_disk_from_mgmt',
            requires=['stg_elem', 'vios_wrap', 'disk_path'])

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

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_stg.FindDisk(disk_dvr, context, instance, disk_type)
        tf.assert_called_once_with(name='find_disk', provides='disk_dev_info')

    def test_save_bdm(self):
        mock_bdm = mock.Mock(volume_id=1)
        save_bdm = tf_stg.SaveBDM(mock_bdm, 'instance')
        save_bdm.execute()
        mock_bdm.save.assert_called_once_with()

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_stg.SaveBDM(mock_bdm, 'instance')
        tf.assert_called_once_with(name='save_bdm_1')

    def test_extend_disk(self):
        disk_dvr = mock.Mock()
        instance = mock.Mock()
        disk_info = {'type': 'disk_type'}

        task = tf_stg.ExtendDisk(disk_dvr, instance, disk_info, 1024)
        task.execute()
        disk_dvr.extend_disk.assert_called_once_with(instance, disk_info, 1024)

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_stg.ExtendDisk(disk_dvr, instance, disk_info, 1024)
        tf.assert_called_once_with(name='extend_disk_disk_type')

    def test_connect_volume(self):
        vol_dvr = mock.Mock(connection_info={'data': {'volume_id': '1'}})

        task = tf_stg.ConnectVolume(vol_dvr, 'slot map')
        task.execute()
        vol_dvr.connect_volume.assert_called_once_with('slot map')

        task.revert('result', 'flow failures')
        vol_dvr.reset_stg_ftsk.assert_called_once_with()
        vol_dvr.disconnect_volume.assert_called_once_with('slot map')

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_stg.ConnectVolume(vol_dvr, 'slot map')
        tf.assert_called_once_with(name='connect_vol_1')

    def test_disconnect_volume(self):
        vol_dvr = mock.Mock(connection_info={'data': {'volume_id': '1'}})

        task = tf_stg.DisconnectVolume(vol_dvr, 'slot map')
        task.execute()
        vol_dvr.disconnect_volume.assert_called_once_with('slot map')

        task.revert('result', 'flow failures')
        vol_dvr.reset_stg_ftsk.assert_called_once_with()
        vol_dvr.connect_volume.assert_called_once_with('slot map')

        # Validate args on taskflow.task.Task instantiation
        with mock.patch('taskflow.task.Task.__init__') as tf:
            tf_stg.DisconnectVolume(vol_dvr, 'slot map')
        tf.assert_called_once_with(name='disconnect_vol_1')
