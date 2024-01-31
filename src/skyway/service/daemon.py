# Copyright (c) 2019-2024 The University of Chicago.
# Part of skyway, released under the BSD 3-Clause License.

# Maintainer: Yuxing Peng, Trung Nguyen

import sys, os, signal
from importlib import import_module
from time import time, sleep

from .. import cfg
from .core import *

service_name = sys.argv[1]
status = 'testing' if '--test' in sys.argv else 'running'

import logging
logging.basicConfig(filename=cfg['paths']['log'] + service_name + '.log', format='%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S', level=logging.INFO)
logging.info('Service daemon started.')

# service_cfg is a dictionary read in from $SKYWAYROOT/etc/services
service_cfg = config(service_name)
service_cfg['kwargs']['name'] = service_name

# service_cfg['module'] would be "cloud"
# service_module would be service.py under skyway.cloud.service
#service_module = import_module('skyway.' + service_cfg['module'] + '.service')
service_module = import_module('skyway.' + 'service')
# get the Service class in this module (which is derived from ServiceBse)
service_class = getattr(service_module, 'CloudService')
# create an instance of the Service class using the kwargs params
service_instance = service_class(**(service_cfg['kwargs']))
update_run(service_name, pid=os.getpid(), status=status)
ts_now, ts_next, running = 1, 0, True

def on_exit(sid, frame):
    global running
    running = False

signal.signal(signal.SIGTERM, on_exit)

while running:
    ts_now = time()

    if ts_now > ts_next:
        ts_next = ts_now + service_cfg['every']

        # this is where the Service caller __call__() is invoked
        if get_status(service_name)['status'] in ['running', 'testing']:
            service_instance()
        # update the .run file of this service instance
        update_run(service_name)
    else:
        sleep(1)

del service_instance
update_run(service_name, pid=0, status='stopped')
logging.info('Service daemon stopped.')