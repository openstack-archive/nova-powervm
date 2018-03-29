# Copyright 2015, 2018 IBM Corp.
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
import six

from nova import test

from nova_powervm.virt.powervm import volume
from nova_powervm.virt.powervm.volume import gpfs
from nova_powervm.virt.powervm.volume import iscsi
from nova_powervm.virt.powervm.volume import local
from nova_powervm.virt.powervm.volume import nfs
from nova_powervm.virt.powervm.volume import npiv
from nova_powervm.virt.powervm.volume import vscsi


class TestVolumeAdapter(test.NoDBTestCase):

    def setUp(self):
        super(TestVolumeAdapter, self).setUp()

        # Enable passing through the can attach/detach checks
        self.mock_get_inst_wrap_p = mock.patch('nova_powervm.virt.powervm.vm.'
                                               'get_instance_wrapper')
        self.mock_get_inst_wrap = self.mock_get_inst_wrap_p.start()
        self.addCleanup(self.mock_get_inst_wrap_p.stop)
        self.mock_inst_wrap = mock.MagicMock()
        self.mock_inst_wrap.can_modify_io.return_value = (True, None)
        self.mock_get_inst_wrap.return_value = self.mock_inst_wrap


class TestInitMethods(test.NoDBTestCase):

    # Volume driver types to classes
    volume_drivers = {
        'iscsi': iscsi.IscsiVolumeAdapter,
        'local': local.LocalVolumeAdapter,
        'nfs': nfs.NFSVolumeAdapter,
        'gpfs': gpfs.GPFSVolumeAdapter,
    }

    def test_get_volume_class(self):
        for vol_type, class_type in six.iteritems(self.volume_drivers):
            self.assertEqual(class_type, volume.get_volume_class(vol_type))

        # Try the fibre as vscsi
        self.flags(fc_attach_strategy='vscsi', group='powervm')
        self.assertEqual(vscsi.PVVscsiFCVolumeAdapter,
                         volume.get_volume_class('fibre_channel'))

        # Try the fibre as npiv
        self.flags(fc_attach_strategy='npiv', group='powervm')
        self.assertEqual(npiv.NPIVVolumeAdapter,
                         volume.get_volume_class('fibre_channel'))

    def test_build_volume_driver(self):
        for vol_type, class_type in six.iteritems(self.volume_drivers):
            vdrv = volume.build_volume_driver(
                mock.Mock(), "abc123", mock.Mock(uuid='abc1'),
                {'driver_volume_type': vol_type})
            self.assertIsInstance(vdrv, class_type)

        # Try the fibre as vscsi
        self.flags(fc_attach_strategy='vscsi', group='powervm')
        vdrv = volume.build_volume_driver(
            mock.Mock(), "abc123", mock.Mock(uuid='abc1'),
            {'driver_volume_type': 'fibre_channel'})
        self.assertIsInstance(vdrv, vscsi.PVVscsiFCVolumeAdapter)

        # Try the fibre as npiv
        self.flags(fc_attach_strategy='npiv', group='powervm')
        vdrv = volume.build_volume_driver(
            mock.Mock(), "abc123", mock.Mock(uuid='abc1'),
            {'driver_volume_type': 'fibre_channel'})
        self.assertIsInstance(vdrv, npiv.NPIVVolumeAdapter)

    def test_hostname_for_volume(self):
        self.flags(host='test_host')
        mock_instance = mock.Mock()
        mock_instance.name = 'instance'

        # Try the fibre as vscsi
        self.flags(fc_attach_strategy='vscsi', group='powervm')
        self.assertEqual("test_host",
                         volume.get_hostname_for_volume(mock_instance))

        # Try the fibre as npiv
        self.flags(fc_attach_strategy='npiv', group='powervm')
        self.assertEqual("test_host_instance",
                         volume.get_hostname_for_volume(mock_instance))

        # NPIV with long host name
        self.flags(host='really_long_host_name_too_long')
        self.assertEqual("really_long_host_nam_instance",
                         volume.get_hostname_for_volume(mock_instance))
