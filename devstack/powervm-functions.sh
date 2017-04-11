#!/bin/bash

# devstack/powervm-functions.sh
# Functions to control the installation and configuration of the PowerVM compute services

# TODO (adreznec) Uncomment when public NovaLink PPA available
# NOVALINK_PPA=${NOVALINK_PPA:-TBD}

function check_novalink_install {
    echo_summary "Checking NovaLink installation"
    if ! ( is_package_installed pvm-novalink ); then
        echo "WARNING: You are using the NovaLink drivers, but NovaLink is not installed on this system."
    fi

    # The user that nova runs as should be a member of **pvm_admin** group
    if ! getent group $PVM_ADMIN_GROUP >/dev/null; then
        sudo groupadd $PVM_ADMIN_GROUP
    fi
    add_user_to_group $STACK_USER $PVM_ADMIN_GROUP
}

function install_novalink {
    echo_summary "Installing NovaLink"
    if is_ubuntu; then
        # Set up the NovaLink PPA
        # TODO (adreznec) Uncomment when public NovaLink PPA available
        # echo "deb ${NOVALINK_PPA} ${DISTRO} main" | sudo tee /etc/apt/sources.list.d/novalink-${DISTRO}.list
        # echo "deb-src ${NOVALINK_PPA} ${DISTRO} main" | sudo tee --append /etc/apt/sources.list.d/novalink-${DISTRO}.list

        NO_UPDATE_REPOS=FALSE
        REPOS_UPDATED=FALSE
    else
        die $LINENO "NovaLink is currently supported only on Ubuntu platforms"
    fi

    install_package pvm-novalink
    echo_summary "NovaLink install complete"
}
