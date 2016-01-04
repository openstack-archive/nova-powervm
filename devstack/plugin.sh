#!/bin/bash
#
# plugin.sh - Devstack extras script to install and configure the nova compute
# driver for powervm

# This driver is enabled in override-defaults with:
#  VIRT_DRIVER=${VIRT_DRIVER:-powervm}

# The following entry points are called in this order for nova-powervm:
#
# - install_nova_powervm
# - configure_nova_powervm
# - start_nova_powervm
# - stop_nova_powervm
# - cleanup_nova_powervm

# Save trace setting
MY_XTRACE=$(set +o | grep xtrace)
set +o xtrace

# Defaults
# --------

# Set up base directories
NOVA_DIR=${NOVA_DIR:-$DEST/nova}
NOVA_CONF_DIR=${NOVA_CONF_DIR:-/etc/nova}
NOVA_CONF=${NOVA_CONF:-NOVA_CONF_DIR/nova.conf}

# nova-powervm directories
NOVA_POWERVM_DIR=${NOVA_POWERVM_DIR:-${DEST}/nova-powervm}
NOVA_POWERVM_PLUGIN_DIR=$(readlink -f $(dirname ${BASH_SOURCE[0]}))

# Support entry points installation of console scripts
if [[ -d $NOVA_DIR/bin ]]; then
    NOVA_BIN_DIR=$NOVA_DIR/bin
else
    NOVA_BIN_DIR=$(get_python_exec_prefix)
fi

# Source functions
source $NOVA_POWERVM_PLUGIN_DIR/powervm-functions.sh

# Entry Points
# ------------

# configure_nova_powervm() - Configure the system to use nova_powervm
function configure_nova_powervm {

    # Default configuration
    iniset $NOVA_CONF DEFAULT compute_driver $PVM_DRIVER
    iniset $NOVA_CONF DEFAULT instance_name_template $INSTANCE_NAME_TEMPLATE
    iniset $NOVA_CONF DEFAULT compute_available_monitors $COMPUTE_MONITORS
    iniset $NOVA_CONF DEFAULT compute_monitors ComputeDriverCPUMonitor
    iniset $NOVA_CONF DEFAULT force_config_drive $FORCE_CONFIG_DRIVE
    iniset $NOVA_CONF DEFAULT injected_network_template $INJECTED_NETWORK_TEMPLATE
    iniset $NOVA_CONF DEFAULT flat_injected $FLAT_INJECTED
    iniset $NOVA_CONF DEFAULT use_ipv6 $USE_IPV6
    iniset $NOVA_CONF DEFAULT firewall_driver $FIREWALL_DRIVER

    # PowerVM specific configuration
    iniset $NOVA_CONF powervm disk_driver $DISK_DRIVER
    if [[ -n $VOL_GRP_NAME ]]; then
        iniset $NOVA_CONF powervm volume_group_name $VOL_GRP_NAME
    fi
    if [[ -n $CLUSTER_NAME ]]; then
        iniset $NOVA_CONF powervm cluster_name $CLUSTER_NAME
    fi
}

# install_nova_powervm() - Install nova_powervm and necessary dependencies
function install_nova_powervm {
    if [[ "$INSTALL_PYPOWERVM" == "True" ]]; then
        echo_summary "Installing pypowervm"
        install_pypowervm
    fi

    # Install the nova-powervm package
    setup_develop $NOVA_POWERVM_DIR
}

# start_nova_powervm() - Start the nova_powervm process
function start_nova_powervm {
    # Check that NovaLink is installed and running
    check_novalink_install

    # This function intentionally functionless as the
    # compute service will start normally
}

# stop_nova_powervm() - Stop the nova_powervm process
function stop_nova_powervm {
    # This function intentionally left blank as the
    # compute service will stop normally
    :
}

# cleanup_nova_powervm() - Cleanup the nova_powervm process
function cleanup_nova_powervm {
    # This function intentionally left blank
    :
}

# Core Dispatch
# -------------
if is_service_enabled nova-powervm; then
    if [[ "$1" == "stack" && "$2" == "pre-install" ]]; then
        # Install NovaLink if set
        if [[ "$INSTALL_NOVALINK" = "True" ]]; then
            echo_summary "Installing NovaLink"
            install_novalink
        fi
    fi

    if [[ "$1" == "stack" && "$2" == "install" ]]; then
        # Perform installation of nova-powervm
        echo_summary "Installing nova-powervm"
        install_nova_powervm

    elif [[ "$1" == "stack" && "$2" == "post-config" ]]; then
        # Lay down configuration post install
        echo_summary "Configuring nova-powervm"
        configure_nova_powervm

    elif [[ "$1" == "stack" && "$2" == "extra" ]]; then
        # Initialize and start the nova-powervm/nova-compute service
        echo_summary "Starting nova-powervm"
        start_nova_powervm
    fi

    if [[ "$1" == "unstack" ]]; then
        # Shut down nova-powervm/nova-compute
        echo_summary "Stopping nova-powervm"
        stop_nova_powervm
    fi

    if [[ "$1" == "clean" ]]; then
        # Remove any lingering configuration data
        # clean.sh first calls unstack.sh
        echo_summary "Cleaning up nova-powervm and associated data"
        cleanup_nova_powervm
        cleanup_pypowervm
    fi
fi

# Restore xtrace
$MY_XTRACE

# Local variables:
# mode: shell-script
# End:
