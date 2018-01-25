# Copyright 2016, 2017 IBM Corp.
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

from nova.virt import imagecache

from nova_powervm.virt.powervm.disk import driver

from oslo_log import log as logging
from pypowervm.tasks import storage as tsk_stg
from pypowervm.wrappers import storage as pvm_stg
from pypowervm.wrappers import virtual_io_server as pvm_vios


LOG = logging.getLogger(__name__)


class ImageManager(imagecache.ImageCacheManager):

    def __init__(self, vios_uuid, vg_uuid, adapter):
        super(ImageManager, self).__init__()
        self.vios_uuid = vios_uuid
        self.vg_uuid = vg_uuid
        self.adapter = adapter

    def _get_base(self):
        """Returns the base directory of the cached images.

        :return: Volume Group containing all images/instances
        """
        # Return VG for instances
        return pvm_stg.VG.get(
            self.adapter, uuid=self.vg_uuid,
            parent_type=pvm_vios.VIOS.schema_type, parent_uuid=self.vios_uuid)

    def _scan_base_image(self, base_dir):
        """Scan base images present in base_dir.

        :param base_dir: Volume group containing all images/instances
        :return: List of all virtual disks containing a bootable image that
                 were created for caching purposes.
        """
        # Find LVs in the _get_base VG with i_<partial id>
        prefix = '%s_' % driver.DiskType.IMAGE[0]
        return [image for image in base_dir.virtual_disks
                if image.name.startswith(prefix)]

    def _age_and_verify_cached_images(self, context, all_instances, base_dir):
        """Finds and removes unused images from the cache.

        :param context: nova context
        :param all_instances: List of all instances on the node
        :param base_dir: Volume group of cached images
        """
        # Use the 'used_images' key from nova imagecache to get a dict that
        # uses image_ids as keys.
        cache = self._scan_base_image(base_dir)
        running_inst = self._list_running_instances(context, all_instances)
        adjusted_ids = []
        for img_id in running_inst.get('used_images'):
            if img_id:
                adjusted_ids.append(
                    driver.DiskAdapter.get_name_by_uuid(driver.DiskType.IMAGE,
                                                        img_id, short=True))
        # Compare base images with running instances remove unused
        unused = [image for image in cache if image.name not in adjusted_ids]
        # Remove unused
        if unused:
            for image in unused:
                LOG.info("Removing unused cache image: '%s'", image.name)
            tsk_stg.rm_vg_storage(base_dir, vdisks=unused)

    def update(self, context, all_instances):
        """Remove cached images not being used by any instance.

        :param context: nova context
        :param all_instances: List of all instances on the node
        """

        base_dir = self._get_base()
        self._age_and_verify_cached_images(context, all_instances, base_dir)
