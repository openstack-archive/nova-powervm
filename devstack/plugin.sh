#!/bin/bash
#
# plugin.sh - Devstack extras script to install and configure the ceilometer
# hypervisor inspector for powervm

# This driver is enabled in ceilometer.conf with:
#   hypervisor_inspector=powervm

# The following entry points are called in this order for ceilometer-powervm:
#
# - install_ceilometer_powervm
# - configure_ceilometer_powervm
# - start_ceilometer_powervm
# - stop_ceilometer_powervm
# - cleanup_ceilometer_powervm

# Save trace setting
MY_XTRACE=$(set +o | grep xtrace)
set +o xtrace

# Defaults
# --------

# Set up base directories
CEILOMETER_CONF_DIR=${CEILOMETER_CONF_DIR:-/etc/ceilometer}
CEILOMETER_CONF=${CEILOMETER_CONF:-CEILOMETER_CONF_DIR/ceilometer.conf}
NOVA_CONF=${NOVA_CONF:-/etc/nova/nova.conf}

# ceilometer-powervm directories
CEILOMETER_POWERVM_DIR=${CEILOMETER_POWERVM_DIR:-${DEST}/ceilometer-powervm}
CEILOMETER_POWERVM_PLUGIN_DIR=$(readlink -f $(dirname ${BASH_SOURCE[0]}))

# Source functions
source $CEILOMETER_POWERVM_PLUGIN_DIR/powervm-functions.sh

# Entry Points
# ------------

# configure_ceilometer_powervm() - Configure the system to use ceilometer_powervm
function configure_ceilometer_powervm {
    # Set the hypervisor inspector to be powervm
    iniset $CEILOMETER_CONF DEFAULT hypervisor_inspector $HYPERVISOR_INSPECTOR

    # Set the compute monitors
    iniset $NOVA_CONF DEFAULT compute_monitors cpu.virt_driver
}

# install_ceilometer_powervm() - Install ceilometer_powervm and necessary dependencies
function install_ceilometer_powervm {
    if [[ "$INSTALL_PYPOWERVM" == "True" ]]; then
        echo_summary "Installing pypowervm"
        install_pypowervm
    fi

    # Install the ceilometer-powervm package
    setup_develop $CEILOMETER_POWERVM_DIR
}

# start_ceilometer_powervm() - Start the ceilometer_powervm process
function start_ceilometer_powervm {
    # Check that NovaLink is installed and running
    check_novalink_install

    # Start the pvm ceilometer compute agent
    run_process pvm-ceilometer-acompute "$CEILOMETER_BIN_DIR/ceilometer-polling --polling-namespaces compute --config-file $CEILOMETER_CONF"
}

# stop_ceilometer_powervm() - Stop the ceilometer_powervm process
function stop_ceilometer_powervm {
    # Stop the pvm ceilometer compute agent
    stop_process pvm-ceilometer-acompute
}

# cleanup_ceilometer_powervm() - Cleanup the ceilometer_powervm process
function cleanup_ceilometer_powervm {
    # This function intentionally left blank
    :
}

# Core Dispatch
# -------------
if is_service_enabled pvm-ceilometer-acompute; then
    if [[ "$1" == "stack" && "$2" == "pre-install" ]]; then
        # Install NovaLink if set
        if [[ "$INSTALL_NOVALINK" = "True" ]]; then
            echo_summary "Installing NovaLink"
            install_novalink
        fi
    fi

    if [[ "$1" == "stack" && "$2" == "install" ]]; then
        # Perform installation of ceilometer-powervm
        echo_summary "Installing ceilometer-powervm"
        install_ceilometer_powervm

    elif [[ "$1" == "stack" && "$2" == "post-config" ]]; then
        # Lay down configuration post install
        echo_summary "Configuring ceilometer-powervm"
        configure_ceilometer_powervm

    elif [[ "$1" == "stack" && "$2" == "extra" ]]; then
        # Initialize and start the ceilometer compute agent for PowerVM
        echo_summary "Starting ceilometer-powervm"
        start_ceilometer_powervm
    fi

    if [[ "$1" == "unstack" ]]; then
        # Shut down the ceilometer compute agent for PowerVM
        echo_summary "Stopping ceilometer-powervm"
        stop_ceilometer_powervm
    fi

    if [[ "$1" == "clean" ]]; then
        # Remove any lingering configuration data
        # clean.sh first calls unstack.sh
        echo_summary "Cleaning up ceilometer-powervm and associated data"
        cleanup_ceilometer_powervm
        cleanup_pypowervm
    fi
fi

# Restore xtrace
$MY_XTRACE

# Local variables:
# mode: shell-script
# End:
