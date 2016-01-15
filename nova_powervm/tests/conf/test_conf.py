# Copyright 2016 IBM Corp.
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

from nova import test
import oslo_config

from nova_powervm import conf as cfg

CONF = cfg.CONF


class TestConf(test.TestCase):
    def setUp(self):
        super(TestConf, self).setUp()

    def test_conf(self):
        """Tests that powervm config values are configured."""
        # Try an option from each grouping of static options

        # Base set of options
        self.assertEqual(0.1, CONF.powervm.proc_units_factor)
        # Local disk
        self.assertEqual('', CONF.powervm.volume_group_name)
        # SSP disk
        self.assertEqual('', CONF.powervm.cluster_name)
        # Volume attach
        self.assertEqual('vscsi', CONF.powervm.fc_attach_strategy)
        # NPIV
        self.assertEqual(1, CONF.powervm.ports_per_fabric)


class TestConfDynamic(test.TestCase):
    def setUp(self):
        super(TestConfDynamic, self).setUp()
        self.conf_fx = self.useFixture(
            oslo_config.fixture.Config(oslo_config.cfg.ConfigOpts()))
        # Set the raw values in the config
        self.conf_fx.load_raw_values(group='powervm', fabrics='A,B',
                                     fabric_A_port_wwpns='WWPN1',
                                     fabric_B_port_wwpns='WWPN2')
        # Now register the NPIV options with the values
        self.conf_fx.register_opts(cfg.powervm.npiv_opts, group='powervm')
        self.conf = self.conf_fx.conf

    def test_npiv(self):
        """Tests that NPIV dynamic options are registered correctly."""
        # Register the dynamic FC values
        fabric_mapping = {}
        cfg.powervm._register_fabrics(self.conf, fabric_mapping)
        self.assertEqual('A,B', self.conf.powervm.fabrics)
        self.assertEqual('WWPN1', self.conf.powervm.fabric_A_port_wwpns)
        self.assertEqual('WWPN2', self.conf.powervm.fabric_B_port_wwpns)
        # Ensure the NPIV data was setup correctly
        self.assertEqual({'B': ['WWPN2'], 'A': ['WWPN1']}, fabric_mapping)
