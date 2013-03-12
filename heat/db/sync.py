#!/usr/bin/env python
# vim: tabstop=4 shiftwidth=4 softtabstop=4

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

import gettext

import sys

gettext.install('heat', unicode=1)

from oslo.config import cfg
from heat.openstack.common import log as logging
import heat.db
from heat.db import migration


LOG = logging.getLogger(__name__)


if __name__ == '__main__':
    cfg.CONF(project='heat', prog='heat-engine')

    heat.db.configure()

    try:
        migration.db_sync()
    except Exception as exc:
        print >>sys.stderr, str(exc)
        sys.exit(1)
