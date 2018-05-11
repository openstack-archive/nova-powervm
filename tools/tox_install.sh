#!/usr/bin/env bash

# Base code from https://github.com/openstack/glance_store/blob/master/tools/tox_install.sh
#
# Library constraint file contains version pin that is in conflict with
# installing the library from source. We should replace the version pin in
# the constraints file before applying it for from-source installation.

ZUUL_CLONER=/usr/zuul-env/bin/zuul-cloner
BRANCH_NAME=master
LIB_NAME=pypowervm
LIB_BRANCH=develop
LIB_LOCATION=git+https://github.com/powervm/$LIB_NAME@$LIB_BRANCH#egg=$LIB_NAME
requirements_installed=$(echo "import openstack_requirements" | python 2>/dev/null ; echo $?)

set -e -x

CONSTRAINTS_FILE=$1
shift

install_cmd="pip install"
mydir=$(mktemp -dt "$LIB_NAME-tox_install-XXXXXXX")
trap "rm -rf $mydir" EXIT
localfile=$mydir/upper-constraints.txt
if [[ $CONSTRAINTS_FILE != http* ]]; then
    CONSTRAINTS_FILE=file://$CONSTRAINTS_FILE
fi
curl $CONSTRAINTS_FILE -k -o $localfile
install_cmd="$install_cmd -c$localfile"

if [ $requirements_installed -eq 0 ]; then
    echo "ALREADY INSTALLED" > /tmp/tox_install.txt
    echo "Requirements already installed; using existing package"
elif [ -x "$ZUUL_CLONER" ]; then
    echo "ZUUL CLONER" > /tmp/tox_install.txt
    pushd $mydir
    $ZUUL_CLONER --cache-dir \
        /opt/git \
        --branch $BRANCH_NAME \
        git://git.openstack.org \
        openstack/requirements
    cd openstack/requirements
    $install_cmd -e .
    popd
else
    echo "PIP HARDCODE" > /tmp/tox_install.txt
    if [ -z "$REQUIREMENTS_PIP_LOCATION" ]; then
        REQUIREMENTS_PIP_LOCATION="git+https://git.openstack.org/openstack/requirements@$BRANCH_NAME#egg=requirements"
    fi
    $install_cmd -U -e ${REQUIREMENTS_PIP_LOCATION}
fi

# This is the main purpose of the script: Allow local installation of
# the current repo. It is listed in constraints file and thus any
# install will be constrained and we need to unconstrain it.
edit-constraints $localfile -- $LIB_NAME "-e $LIB_LOCATION"

if [ -z "$@" ]; then
    echo "No packages to be installed."
    exit 0
fi

$install_cmd -U $*
exit $?
