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

from nova.compute import power_state
from nova.compute import task_states
from nova.compute import vm_states
from nova.objects import flavor
from nova.objects import image_meta
from nova.objects import instance
import os
import sys


TEST_FLAVOR = flavor.Flavor(memory_mb=2048,
                            swap=0,
                            vcpu_weight=None,
                            root_gb=10,
                            id=2,
                            name=u'm1.small',
                            ephemeral_gb=0,
                            rxtx_factor=1.0,
                            flavorid=u'1',
                            vcpus=1)

TEST_INSTANCE = {
    'id': 1,
    'uuid': '49629a5c-f4c4-4721-9511-9725786ff2e5',
    'display_name': 'Fake Instance',
    'root_gb': 10,
    'ephemeral_gb': 0,
    'instance_type_id': '5',
    'system_metadata': {'image_os_distro': 'rhel'},
    'host': 'host1',
    'flavor': TEST_FLAVOR,
    'task_state': None,
    'vm_state': vm_states.ACTIVE,
    'power_state': power_state.SHUTDOWN,
}

TEST_INST_SPAWNING = dict(TEST_INSTANCE, task_state=task_states.SPAWNING,
                          uuid='b3c04455-a435-499d-ac81-371d2a2d334f')

TEST_INST1 = instance.Instance(**TEST_INSTANCE)
TEST_INST2 = instance.Instance(**TEST_INST_SPAWNING)

TEST_MIGRATION = {
    'id': 1,
    'source_compute': 'host1',
    'dest_compute': 'host2',
    'migration_type': 'resize',
    'old_instance_type_id': 1,
    'new_instance_type_id': 2,
}
TEST_MIGRATION_SAME_HOST = dict(TEST_MIGRATION, dest_compute='host1')

IMAGE1 = {
    'id': '3e865d14-8c1e-4615-b73f-f78eaecabfbd',
    'name': 'image1',
    'size': 300,
    'container_format': 'bare',
    'disk_format': 'raw',
    'checksum': 'b518a8ba2b152b5607aceb5703fac072',
}
TEST_IMAGE1 = image_meta.ImageMeta.from_dict(IMAGE1)
EMPTY_IMAGE = image_meta.ImageMeta.from_dict({})

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
