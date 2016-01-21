#!/bin/bash

# devstack/powervm-functions.sh
# Functions to control the installation and configuration of the PowerVM compute services

GITREPO["pypowervm"]=${PYPOWERVM_REPO:-https://github.com/powervm/pypowervm}
GITBRANCH["pypowervm"]=${PYPOWERVM_BRANCH:-master}
GITDIR["pypowervm"]=$DEST/pypowervm

# TODO (adreznec) Uncomment when public NovaLink PPA available
# NOVALINK_PPA=${NOVALINK_PPA:-TBD}

function install_pypowervm {
    # Install the latest pypowervm from git
    echo_summary "Installing pypowervm"
    git_clone_by_name "pypowervm"
    setup_dev_lib "pypowervm"
    echo_summary "Pypowervm install complete"
}

function cleanup_pypowervm {
    echo_summary "Cleaning pypowervm"
    rm -rf ${GITDIR["pypowervm"]}
}

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
