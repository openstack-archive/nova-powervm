# Copyright 2014 IBM Corp.
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

# TODO(mikal): move eventlet imports to nova.__init__ once we move to PBR
import os
import sys

SYS_META = {
    'instance_type_memory_mb': 2048,
    'instance_type_swap': 0,
    'instance_type_vcpu_weight': None,
    'instance_type_root_gb': 1,
    'instance_type_id': 2,
    'instance_type_name': u'm1.small',
    'instance_type_ephemeral_gb': 0,
    'instance_type_rxtx_factor': 1.0,
    'instance_type_flavorid': u'1',
    'instance_type_vcpus': 1
}

TEST_INSTANCE = {
    'id': 1,
    'uuid': '49629a5c-f4c4-4721-9511-9725786ff2e5',
    'display_name': 'Fake Instance',
    'instance_type_id': '5',
    'system_metadata': SYS_META
}

# NOTE(mikal): All of this is because if dnspython is present in your
# environment then eventlet monkeypatches socket.getaddrinfo() with an
# implementation which doesn't work for IPv6. What we're checking here is
# that the magic environment variable was set when the import happened.
if ('eventlet' in sys.modules and
        os.environ.get('EVENTLET_NO_GREENDNS', '').lower() != 'yes'):
    raise ImportError('eventlet imported before nova/cmd/__init__ '
                      '(env var set to %s)'
                      % os.environ.get('EVENTLET_NO_GREENDNS'))

os.environ['EVENTLET_NO_GREENDNS'] = 'yes'

import eventlet

eventlet.monkey_patch(os=False)
