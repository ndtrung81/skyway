# Copyright (c) 2019-2024 The University of Chicago.
# Part of skyway, released under the BSD 3-Clause License.

# Maintainer: Yuxing Peng, Trung Nguyen

"""@package docstring
Documentation for AWS Class
"""

from .core import Cloud
import boto3

class AWS(Cloud):
    """Documentation for AWS Class
    This Class is used as the driver to operate Cloud resource for [Demo]
    """
    
    def __init__(self, vendor_cfg, kwargs):
        """Constructor:
        The construct initialize the connection to the cloud platform, by using
        setting informations passed by [cfg], such as the credentials.        
        """
        super().__init__(vendor_cfg, kwargs)

        self.client = boto3.client('sts',
            aws_access_key_id = vendor_cfg['master_access_key_id'],
            aws_secret_access_key = vendor_cfg['master_secret_access_key'])

        self.assumed_role = self.client.assume_role(
            RoleArn = "arn:aws:iam::%s:role/%s" % (self.account_id, self.role_name), 
            RoleSessionName = "RCCSkyway"
        )
        
        credentials = self.assumed_role['Credentials']
        
        self.ec2 = boto3.resource('ec2',
            aws_access_key_id = credentials['AccessKeyId'],
            aws_secret_access_key = credentials['SecretAccessKey'],
            aws_session_token= credentials['SessionToken'],
            region_name = self.region)

        return
    
    
    
    def running_nodes(self):
        """Member function: running_nodes
        Return identifiers of all running instances
        """
        
        instances = self.get_instances(filters = [{
            "Name" : "instance-state-name",
            "Values" : ["running"]
        }])
        
        nodes = []
        
        for instance in instances:
            name = self.get_instance_name(instance)
            if name == '': nodes.append(instance.instance_id)
            else: nodes.append(name)
            
        return nodes
    
    
        
    def list_nodes(self):
        """Member function: list_nodes
        Get a list of all existed instances
        
        Return: a list of multiple turple. Each turple has four elements:
                (1) instance name (2) state (3) type (4) identifier
        """
        
        instances = self.get_instances()
        nodes = []
        
        for instance in instances:
            if instance.state['Name'] != 'terminated':
                nodes.append([self.get_instance_name(instance),
                              instance.state['Name'], 
                              instance.instance_type, 
                              instance.instance_id])
        
        return nodes
    
    
        
    def create_nodes(self, node_type, names = []):
        """Member function: create_compute
        Create a group of compute instances(nodes, servers, virtual-machines 
        ...) with the given type.
        
         - node_type: instance type information from the Skyway definitions
         - names: a group of names for the nodes (optional)
        
        Return: a group of identifiers (i.e., names) for created instances.
        """
        
        count = len(names)
        
        instances = self.ec2.create_instances(
            ImageId          = self.vendor['ami-id'],
            KeyName          = self.vendor['key-name'],
            SecurityGroupIds = self.security_group,
            InstanceType     = self.vendor['node-types'][node_type]['name'],
            MaxCount         = count,
            MinCount         = count)
                
        for instance in instances:
            instance.wait_until_running()
        
        nodes = {}
        
        for inode, instance in enumerate(instances):
            instance.load()
            nodes[names[inode]] = [str(instance.id), str(instance.public_ip_address)]
                
        return nodes
            
        
        
    def destroy_nodes(self, IDs = []):
        """Member function: destroy nodes
        Destroy a group of compute instances

         - IDs: a group of identifiers of instances to be destroyed
        """
        
        instances = []
        
        for ID in IDs:
        
            instance = self.ec2.Instance(ID)
            
            if self.get_instance_name(instance) in self.protected_nodes:
                continue
            
            instance.terminate()
            instances.append(instance)
        
        for instance in instances:
            instance.wait_until_terminated()
        
        return
    
    
        
    def get_host_ip(self, ID):
        """Member function: SSH to a remote instance
        This function prepare the IP address and SSH to the node with the
        given identifier.
        
         - ID: instance identifier
        """
        
        if ID[0:2] == 'i-':
            instances = self.get_instances(filters = [{
                "Name" : "instance-id",
                "Values" : [ID]
            }])
        else:
            instances = self.get_instances(filters = [{
                "Name" : "tag:Name",
                "Values" : [ID]
            }])
        
        return list(instances)[0].public_ip_address
    
    
    def get_instance_name(self, instance):
        """Member function: get_instance_name
        Get the name information from the instance with given ID.
        Note: AWS doesn't use unique name for instances, instead, name is an
        attribute stored in the tags.
        
         - ID: instance identifier
        """
        
        if instance.tags is None: return ''
        
        for tag in instance.tags:
            if tag['Key'] == 'Name':
                return tag['Value']
        
        return ''
    
    
    def get_instances(self, filters = []):
        """Member function: get_instances
        Get a list of instance objects with give filters
        """
        
        return self.ec2.instances.filter(Filters = filters)
    
    
    def get_cost(self, start, end):
        
        pass

