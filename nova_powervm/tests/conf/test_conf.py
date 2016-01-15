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


class TestConfBounds(test.TestCase):
    def setUp(self):
        super(TestConfBounds, self).setUp()

    def _bounds_test(self, should_pass, opts, **kwargs):
        """Test the bounds of an option."""
        # Use the Oslo fixture to create a temporary conf object
        with oslo_config.fixture.Config(oslo_config.cfg.ConfigOpts()) as fx:
            # Load the raw values
            fx.load_raw_values(group='powervm', **kwargs)
            # Register the options
            fx.register_opts(opts, group='powervm')
            # For each kwarg option passed, validate it.
            for kw in kwargs:
                if not should_pass:
                    # Reference the option to cause a bounds exception
                    self.assertRaises(oslo_config.cfg.ConfigFileValueError,
                                      lambda: fx.conf.powervm[kw])
                else:
                    # It's expected to succeed
                    fx.conf.powervm[kw]

    def test_bounds(self):
        # Uncapped proc weight
        self._bounds_test(False, cfg.powervm.powervm_opts,
                          uncapped_proc_weight=0)
        self._bounds_test(False, cfg.powervm.powervm_opts,
                          uncapped_proc_weight=256)
        self._bounds_test(True, cfg.powervm.powervm_opts,
                          uncapped_proc_weight=200)
        # vopt media repo size
        self._bounds_test(False, cfg.powervm.powervm_opts,
                          vopt_media_rep_size=0)
        self._bounds_test(True, cfg.powervm.powervm_opts,
                          vopt_media_rep_size=10)
        # vscsi connections
        self._bounds_test(False, cfg.powervm.vol_adapter_opts,
                          vscsi_vios_connections_required=0)
        self._bounds_test(True, cfg.powervm.vol_adapter_opts,
                          vscsi_vios_connections_required=2)
        # ports per fabric
        self._bounds_test(False, cfg.powervm.npiv_opts,
                          ports_per_fabric=0)
        self._bounds_test(True, cfg.powervm.npiv_opts,
                          ports_per_fabric=2)


class TestConfChoices(test.TestCase):
    def setUp(self):
        super(TestConfChoices, self).setUp()

    def _choice_test(self, invalid_choice, valid_choices, opts, option,
                     ignore_case=True):
        """Test the choices of an option."""

        def _setup(fx, value):
            # Load the raw values
            fx.load_raw_values(group='powervm', **{option: value})
            # Register the options
            fx.register_opts(opts, group='powervm')

        def _build_list():
            for val in valid_choices:
                yield val
                yield val.lower()
                yield val.upper()

        if ignore_case:
            # We expect to be able to ignore upper/lower case, so build a list
            # of possibilities and ensure we do ignore them.
            valid_choices = [x for x in _build_list()]

        if invalid_choice:
            # Use the Oslo fixture to create a temporary conf object
            with oslo_config.fixture.Config(oslo_config.cfg.ConfigOpts()
                                            ) as fx:
                _setup(fx, invalid_choice)
                # Reference the option to cause an exception
                self.assertRaises(oslo_config.cfg.ConfigFileValueError,
                                  lambda: fx.conf.powervm[option])

        for choice in valid_choices:
            # Use the Oslo fixture to create a temporary conf object
            with oslo_config.fixture.Config(oslo_config.cfg.ConfigOpts()
                                            ) as fx:
                _setup(fx, choice)

                # It's expected to succeed
                fx.conf.powervm[option]

    def test_choices(self):
        # Disk driver
        self._choice_test('bad_driver', ['localdisk', 'ssp'],
                          cfg.powervm.powervm_opts, 'disk_driver')
        # FC attachment
        self._choice_test('bad_value', ['vscsi', 'npiv'],
                          cfg.powervm.vol_adapter_opts, 'fc_attach_strategy')


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
