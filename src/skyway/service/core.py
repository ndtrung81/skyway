# Copyright (c) 2019-2024 The University of Chicago.
# Part of skyway, released under the BSD 3-Clause License.

# Maintainer: Yuxing Peng, Trung Nguyen

import os
import time
import logging
import yaml
from time import sleep

from datetime import datetime
from tabulate import tabulate

from .. import cfg
from .. import utils

# base class for service under cloud/
#   cloud is a member of service
#   so is billing

class ServiceBase:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

# service_cfg is a dictionary read in from $SKYWAYROOT/etc/services
service_path = cfg['paths']['etc'] + 'services/'
# services is a list of names extracte from the *.yaml files under service_path
#   e.g. cloud-rcc-aws.yaml, cloud-rcc-gcp.yaml
services = [file[:-5] for file in os.listdir(service_path) if file.endswith('.yaml')]

def assert_config(name):
    if name not in services:
        raise Exception(f'Service {name} does not exist.')

def assert_run(service_name, strict=False):
    assert_config(service_name)
    
    if not os.path.isfile(cfg['paths']['run'] + service_name + '.yaml'):
        if strict:
            raise Exception(f'Service {service_name} has not been registered. Run --register first.')
        else:
            return False
    
    return True

def get_run(service_name):
    assert_run(service_name, strict=True)
    
    with open(cfg['paths']['run'] + service_name + '.yaml', 'r') as fp:
        return yaml.load(fp, Loader=yaml.FullLoader)

def get_status(service_name):
    pinfo = None
    
    while pinfo is None:
        pinfo = get_run(service_name)
        if pinfo is None: sleep(1)
    
    #process_name = utils.proc("ps -o cmd= " + str(pinfo['pid']), strict=False)

    if (pinfo['status'] not in ['testing', 'stopped']) and (check(pinfo['pid'], service_name) == False):
        pinfo['status'] = 'failed'
    
    return pinfo

def update_run(service_name, update=True, **kwargs):
    run_status = get_run(service_name) if update else {'pid': 0, 'status': 'stopped'}
    run_status.update(kwargs)
    run_status['update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    with open(cfg['paths']['run'] + service_name + '.yaml', 'w') as fp:
        fp.write(yaml.dump(run_status))

def register(service_name):
    if assert_run(service_name):
       raise Exception('Service {service_name} has been registered already!')
    update_run(service_name, update=False)

def config(service_name):
    assert_config(service_name)
    with open(service_path + service_name + ".yaml", 'r') as fp:
        return yaml.load(fp, Loader=yaml.FullLoader)

def start(service_name):
    assert_run(service_name)
    pinfo = get_status(service_name)

    if pinfo['status'] in ['running', 'paused']:
        print("Service is already running.")
        return

    outfile = cfg['paths']['run'] + service_name + '.run'
    #utils.proc("nohup skyway service.daemon %s > %s 2>&1 &" % (service_name, outfile))

    # when daemon starts running, it creates an instance of Service
    #   if the daemon is running, it calls its own caller (__call__)
    #   which then call check_nodetype(), which then actually call Cloud create_nodes()
    utils.proc(f"nohup python3 -m skyway.daemon {service_name} > {outfile} 2>&1 &")

def stop(service_name):
    assert_run(service_name)
    pinfo = get_status(service_name)

    if pinfo['status'] == 'running':
        utils.proc('kill -9 ' + str(pinfo['pid']))

        while check(pinfo['pid'], service_name):
            time.sleep(1)
    else:
        print("Service is not running.")

def check(pid, service_name):
    process_name = utils.proc("ps -o cmd= " + str(pid), strict=False)
    return ['python3 -m skyway.service.daemon ' + service_name] == process_name

def list_all():
    body = []        
    for service_name in services:
        if assert_run(service_name):            
            pinfo = get_status(service_name)
            body.append([service_name, pinfo['status'], '-' if pinfo['pid']==0 else pinfo['pid'], pinfo['update']])
    print("")
    print(tabulate(body, headers=['Service', 'Status', 'PID', 'Last Update']))
    print("")