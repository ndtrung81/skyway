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
from .. import account
from .core import ServiceBase

# base class for service under cloud/
#   cloud is a member of service
#   so is billing

class CloudService(ServiceBase):
    
    def __init__(self, **kwargs):
        logging.info('Cloud service started.')
        
        super().__init__(**kwargs)
        self.cfg = account.load_cfg(self.account)
        
    def __call__(self, **kwargs):
        self.cloud = cloud.Cloud.create(self.cfg['cloud'], self.cfg['account'])
        try:
            self.billing = Billing(self.account).usages()
        except:
            logging.info("unkown error when checking the billing status")
            time.sleep(60)
            return
            
        """ if self.billing['status'] == 'exceeded':
            slurm.cancel_jobs(slurm.partition_jobs(self.account))
            
            while slurm.partition_jobs(self.account) != []:
                time.sleep(1) """
        
        for nodetype in self.cfg['nodes']:
            self.check_nodetype(nodetype)
    
    def __del__(self):
        logging.info('Cloud service stopped.')
    
    def check_nodetype(self, nodetype):
        node_status = slurm.node_status(self.account, nodetype)
        
        ndrain = len(node_status['drain'])
        nidle = len(node_status['idle'])
        ndown = len(node_status['down'])
        #npending = slurm.pending_jobs_count(self.account, nodetype)
        
        # First, remove down nodes (due to premmetive reasons)
        if ndown > 0:
            nmap = NodeMap()
            instances = []
            nodes = []
            ips = []
            
            for nodename in node_status['down']:
                info = nmap.nodes[nodename]
                #slurm.update_node_state(nodename, 'drain')
                nodes.append(nodename)
                ips.append(info['ip'])
                instances.append(nmap.power_off(nodename))
            
            if len(instances)>0:
                logging.info("Found %d down compute nodes: %s" % (ndown, ','.join(instances)))
                self.cloud.destroy_nodes(instances)
                logging.info("Terminated %d down compute nodes: %s" % (ndown, ','.join(instances)))
        
        # Then, release nodes if idle
        elif nidle > 0:
            nmap = NodeMap()
            instances = []
            nodes = []
            ips = []
            
            for nodename in node_status['idle']:
                info = nmap.nodes[nodename]

                if self.cloud.vendor['grace_sec'] > 0:

                    elapsed = int(float(NodeMap.tsnow()) - float(info['start'])) % 3600
                    if elapsed < self.c.vendor['grace_sec']: continue

                #slurm.update_node_state(nodename, 'drain')
                nodes.append(nodename)
                ips.append(info['ip'])
                instances.append(nmap.power_off(nodename))
            
            if len(instances)>0:
                logging.info("Found %d idle compute nodes: %s" % (nidle, ','.join(instances)))
                self.cloud.destroy_nodes(instances)
                logging.info("Terminated %d compute nodes: %s" % (nidle, ','.join(instances)))
        
        # Finally, create nodes if needed
        elif npending > 0 and nidle == 0 and ndrain > 0:
            self.create_nodes(nodetype, node_status['drain'][:min(ndrain, npending)])
        
        return
    
    def create_nodes(self, nodetype, nodenames):
        cfg = self.cloud.vendor['node-types'][nodetype]
        
        price = cfg['price']
        rate_quota = self.billing['budget']['rate'] - self.billing['rate']
        
        # Get number of possible nodes from budget plan
        n = int(math.floor(rate_quota / price))
        
        # Only 1 node at a time for preemptive node
        
        if 'preemptible' in cfg:
            n = 1
        
        if n>0:
            nodenames = nodenames[:min(len(nodenames), n)]
        else:
            return
                        
        ncreate = len(nodenames)
        logging.info("Create compute nodes in type %s for %d pending jobs" % (nodetype, ncreate))
        
        nodes = self.cloud.create_nodes(nodetype, nodenames)
        ncreated = len(nodes)
        logging.info("%d of %d nodes successfully created" % (ncreated, ncreate))
            
        nmap = NodeMap()
        
        for nodename, nodeinfo in nodes.items():
            if nmap.has_node(nodename):
                raise Exception("Fatal error: incorrect drained node " + nodename)
            
            nmap.power_on(nodename, nodeinfo[0], nodeinfo[1])
            logging.info("New compute node: %s %s %s" % (nodename, nodeinfo[1], nodeinfo[0]))
        
        #slurm.reconfigure()
        utils.proc(['exportfs', '-a'], strict=False)

        for nodename, nodeinfo in nodes.items():
            sshcmd = 'ssh ' + self.cloud.vendor['username'] + '@' + nodeinfo[1]
            failed = 0
            
            while failed < 90:
                check = utils.proc([sshcmd, "whoami"], strict = False)
                
                if check != [] and check[0] == self.cloud.vendor['username']:
                    failed = 0
                    break                    
                else:
                    failed += 1
                    time.sleep(1)
            
            if failed > 0:
                logging.info("Failed to configure node " + nodename)
                self.c.destroy_nodes([nodeinfo[0]])
                continue
            
            logging.info("Post-provision for node %s." % (nodename))
            utils.proc(['/opt/system/post.sh %s %s' % (self.c.vendor['username'], nodename)], strict = False)
            logging.info("Node %s is ready to use." % (nodename))
            #slurm.update_node_state(nodename, 'resume')