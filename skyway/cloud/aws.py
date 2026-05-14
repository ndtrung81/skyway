# Copyright (c) 2019-2024 The University of Chicago.
# Part of skyway, released under the BSD 3-Clause License.

# Maintainer: Trung Nguyen, Yuxing Peng

from datetime import datetime, timezone, timedelta
import io
import os
import random
import subprocess
from tabulate import tabulate

from .core import Cloud
from .. import utils
from ..utils import spinner_wait

from colorama import Fore
import pandas as pd
import time

import boto3
from libcloud.compute.types import Provider
from libcloud.compute.providers import get_driver
from libcloud.compute.drivers.ec2 import EC2NodeDriver


class AWS(Cloud):

    def __init__(self, account):
        account_path = self._load_account_config(account, 'aws')

        self.using_trusted_agent = self.account.get('using_trusted_agent', False)
        if not self.using_trusted_agent:
            self.ec2 = boto3.resource('ec2',
                                      aws_access_key_id=self.account['access_key_id'],
                                      aws_secret_access_key=self.account['secret_access_key'],
                                      region_name=self.account['region'])
        else:
            self.client = boto3.client('sts',
                                       aws_access_key_id=self.vendor['master_access_key_id'],
                                       aws_secret_access_key=self.vendor['master_secret_access_key'])
            self.assumed_role = self.client.assume_role(
                RoleArn="arn:aws:iam::%s:role/%s" % (self.account['account_id'],
                                                      self.account['role_name']),
                RoleSessionName="RCCSkyway"
            )
            credentials = self.assumed_role['Credentials']
            self.ec2 = boto3.resource('ec2',
                                      aws_access_key_id=credentials['AccessKeyId'],
                                      aws_secret_access_key=credentials['SecretAccessKey'],
                                      aws_session_token=credentials['SessionToken'],
                                      region_name=self.account['region'])

        self.using_libcloud = False
        if self.using_libcloud:
            EC2 = get_driver(Provider.EC2)
            self.driver = EC2(self.account['access_key_id'],
                              self.account['secret_access_key'],
                              self.account['region'])

        pem_src = account_path + self.account['key_name'] + '.pem'
        self.my_ssh_private_key = "~/.my_aws_ssh_key.pem"
        self._setup_ssh_key(pem_src, self.my_ssh_private_key, chmod=600)

    def list_nodes(self, show_protected_nodes=False, verbose=False):
        instances = self.get_instances()
        nodes = []
        df = pd.read_pickle(self.usage_history)

        for instance in instances:
            node_name = self.get_instance_name(instance)
            if not show_protected_nodes and node_name in self.account['protected_nodes']:
                continue

            if instance.state['Name'] != 'terminated':
                if instance.state['Name'] == "running":
                    running_time = datetime.now(timezone.utc) - instance.launch_time
                else:
                    df_node = df.loc[df['InstanceID'] == instance.instance_id]
                    if df_node.empty:
                        continue
                    end_time = pd.to_datetime(df_node['End'].iloc[0],
                                              format='%Y-%m-%dT%H:%M:%S.%f%z')
                    running_time = end_time - instance.launch_time

                instance_unit_cost = self.get_unit_price_instance(instance)
                running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost
                instance_user_name = self.get_instance_user_name(instance)

                nodes.append([node_name, instance_user_name, instance.state['Name'],
                               instance.instance_type, instance.instance_id,
                               instance.public_ip_address, running_time, running_cost])

        headers = ['Name', 'User', 'Status', 'Type', 'Instance ID', 'Host', 'Elapsed Time', 'Running Cost']
        output_str = ''
        if verbose:
            print(tabulate(nodes, headers=headers, maxcolwidths=None))
            print("")
        else:
            output_str = io.StringIO()
            print(tabulate(nodes, headers=headers), file=output_str)
            print("", file=output_str)
        return nodes, output_str

    def create_nodes(self, node_type: str, node_names=[], interactive=False,
                     need_confirmation=True, walltime=None, image_id=""):
        if node_type not in self.vendor['node-types']:
            raise Exception(f'Node type {node_type} is not available in this account.')

        user_name = os.environ['USER']
        unit_price = self.vendor['node-types'][node_type]['price']

        if need_confirmation:
            if not self._confirm_budget(user_name, node_type, unit_price):
                return

        count = len(node_names)
        node_name = node_names[0]
        print(Fore.BLUE + f"Allocating {count} instance...")

        vm_image = self.account['ami_id']
        if image_id:
            vm_image = image_id

        instances = self.ec2.create_instances(
            ImageId=vm_image,
            KeyName=self.account['key_name'],
            SecurityGroupIds=self.account['security_group'],
            InstanceType=self.vendor['node-types'][node_type]['name'],
            MaxCount=count,
            MinCount=count,
            TagSpecifications=[{
                'ResourceType': 'instance',
                'Tags': [
                    {'Key': 'Name', 'Value': node_name},
                    {'Key': 'User', 'Value': user_name},
                ]
            }]
        )

        for instance in instances:
            instance.wait_until_running()

        nodes = {}
        username = self.vendor['username']
        region = self.account['region']

        walltime_str = walltime if (walltime and walltime != "") else "00:30:00"
        pt = datetime.strptime(walltime_str, "%H:%M:%S")
        walltime_in_minutes = int(pt.hour * 60 + pt.minute + pt.second / 60)

        for inode, instance in enumerate(instances):
            instance.load()

            instance_type = str(instance.instance_type)
            launch_time = instance.launch_time.strftime("%Y-%m-%dT%H:%M:%S.%f%z")
            ip = instance.public_ip_address
            ip_converted = ip.replace('.', '-')
            nodes[node_names[inode]] = [instance_type, launch_time, str(ip)]

            running_time = timedelta(hours=pt.hour, minutes=pt.minute, seconds=pt.second)
            instance_unit_cost = self.get_unit_price_instance(instance)
            projected_running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost
            usage, remaining_balance = self.get_cost_and_usage_from_db(user_name=user_name)
            end_time = instance.launch_time + running_time

            data = [user_name, instance.instance_id, instance.instance_type,
                    instance.launch_time, end_time, projected_running_cost, remaining_balance]
            self._update_usage_db(data, instance.instance_id)

            print(f"\nCreated instance: {node_names[inode]}")

            ssh_host = f"ec2-{ip_converted}.{region}.compute.amazonaws.com"
            cmd = (f"ssh -t -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
                   f" {username}@{ssh_host} 'sudo shutdown -P +{walltime_in_minutes}'")
            spinner_wait(10, "Preparing the instance")
            subprocess.run(cmd, shell=True, text=True, capture_output=True)

            if self.post_boot_script:
                script_file = os.environ['SKYWAYROOT'] + "/etc/accounts/" + self.post_boot_script
                script_cmd = utils.script2cmd(script_file)
                cmd = (f"ssh -t -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
                       f" {username}@{ssh_host} '{script_cmd}'")
                subprocess.run(cmd, shell=True, text=True, capture_output=True)

            io_server = "172.31.47.245"
            cmd = (f"ssh -t -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
                   f" {username}@{ssh_host}"
                   f" 'sudo mkdir -p /cloud/rcc-aws; sudo mount -t nfs {io_server}:/cloud/rcc-aws /cloud/rcc-aws'")
            subprocess.run(cmd, shell=True, text=True, capture_output=True)

            self._print_connection_hint(node_names[inode], username, ssh_host,
                                        private_key=self.my_ssh_private_key)

        return nodes

    def connect_node(self, instance_ID, separate_terminal=True):
        ip = self.get_host_ip(instance_ID)
        print(Fore.BLUE + f"Connecting to instance: {instance_ID}  IP: {ip}")

        username = self.vendor['username']
        region = self.account['region']
        ip_converted = ip.replace('.', '-')
        ssh_host = f"ec2-{ip_converted}.{region}.compute.amazonaws.com"

        port = random.randint(15000, 30000)
        cmd = (f"ssh -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
               f" -f -N -L {port}:localhost:{port} {username}@{ssh_host}")
        print(f"SSH tunnel on port {port}")
        os.system(cmd)

        if separate_terminal:
            cmd = ("gnome-terminal -q --title='Connecting to the node' -- bash -c "
                   f" 'module purge; ssh -i {self.my_ssh_private_key}"
                   f" -o StrictHostKeyChecking=accept-new {username}@{ssh_host}; exec bash'")
        else:
            cmd = (f"ssh -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
                   f" {username}@{ssh_host}")
        subprocess.run(cmd, shell=True, text=True, capture_output=True)

        return {
            'private_key': self.my_ssh_private_key,
            'login': f"{username}@{ssh_host}",
        }

    def get_node_connection_info(self, instance_ID):
        username = self.vendor['username']
        ip = self.get_host_ip(instance_ID)
        ip_converted = ip.replace('.', '-')
        region = self.account['region']
        return {
            'private_key': self.my_ssh_private_key,
            'login': f"{username}@ec2-{ip_converted}.{region}.compute.amazonaws.com",
        }

    def execute(self, instance_ID: str, **kwargs):
        ip = self.get_host_ip(instance_ID)
        username = self.vendor['username']
        region = self.account['region']
        ip_converted = ip.replace('.', '-')
        command = " ".join(kwargs.values())
        cmd = ("gnome-terminal -q --title='Connecting to the node' -- bash -c "
               f" 'ssh -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
               f" {username}@ec2-{ip_converted}.{region}.compute.amazonaws.com' -t '{command}'")
        subprocess.run(cmd, shell=True, text=True, capture_output=True)

    def execute_script(self, instance_ID: str, script_name: str):
        ip = self.get_host_ip(instance_ID)
        username = self.vendor['username']
        region = self.account['region']
        ip_converted = ip.replace('.', '-')
        script_cmd = utils.script2cmd(script_name)
        cmd = (f"ssh -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
               f" {username}@ec2-{ip_converted}.{region}.compute.amazonaws.com"
               f" -t 'eval {script_cmd}'")
        subprocess.run(cmd, shell=True, text=True, capture_output=True)

    def destroy_nodes(self, node_names=None, IDs=None, need_confirmation=True):
        user_name = os.environ['USER']
        if node_names is None and IDs is None:
            raise ValueError("node_names and IDs cannot be both empty.")

        avail_instances = self.get_instances(filters=[{
            "Name": "instance-state-name",
            "Values": ["running", "stopped"]
        }])

        for instance in avail_instances:
            instance_name = self.get_instance_name(instance)
            instance_id = instance.instance_id
            if instance_name in self.account['protected_nodes']:
                continue

            check_nodename = instance_name in node_names if node_names is not None else False
            check_id = instance_id in IDs if IDs is not None else False

            if check_nodename or check_id:
                instance_user_name = self.get_instance_user_name(instance)
                if instance_user_name != user_name:
                    print(Fore.YELLOW + f"Cannot destroy instance {instance_name}: owned by another user.")
                    continue

                running_time = datetime.now(timezone.utc) - instance.launch_time
                instance_unit_cost = self.get_unit_price_instance(instance)
                running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost

                if need_confirmation:
                    response = input(
                        f"Terminate {instance_name} ({instance_id}),"
                        f" running cost ${running_cost:.5f}? Data will be removed. (y/n) "
                    )
                    if response != 'y':
                        continue

                instance.terminate()

                end_time = datetime.now(timezone.utc)
                running_time = end_time - instance.launch_time
                running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost
                _, remaining_balance = self.get_cost_and_usage_from_db(user_name=user_name)

                data = [instance_user_name, instance.instance_id, instance.instance_type,
                        instance.launch_time, end_time, running_cost, remaining_balance]
                self._update_usage_db(data, instance.instance_id, end_time=end_time)
                print(Fore.GREEN + f"Instance {instance_name} terminated.")

    def stop_nodes(self, IDs=[], node_names=[], need_confirmation=True):
        if isinstance(node_names, str): node_names = [node_names]
        if isinstance(IDs, str): IDs = [IDs]

        user_name = os.environ['USER']
        if not node_names and not IDs:
            raise ValueError("node_names and IDs cannot be both empty.")

        avail_instances = self.get_instances(filters=[{
            "Name": "instance-state-name",
            "Values": ["running"]
        }])

        for instance in avail_instances:
            instance_name = self.get_instance_name(instance)
            instance_id = instance.instance_id
            if instance_name in self.account['protected_nodes']:
                continue

            if instance_name in node_names or instance_id in IDs:
                instance_user_name = self.get_instance_user_name(instance)
                if instance_user_name != user_name:
                    print(Fore.YELLOW + f"Cannot stop instance {instance_name}: owned by another user.")
                    continue

                running_time = datetime.now(timezone.utc) - instance.launch_time
                instance_unit_cost = self.get_unit_price_instance(instance)
                running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost

                if need_confirmation:
                    response = input(
                        f"Stop {instance_name} (running cost ${running_cost:.5f})? (y/n) "
                    )
                    if response != 'y':
                        continue

                instance.stop()

                end_time = datetime.now(timezone.utc)
                running_time = end_time - instance.launch_time
                running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost
                _, remaining_balance = self.get_cost_and_usage_from_db(user_name=user_name)

                data = [instance_user_name, instance.instance_id, instance.instance_type,
                        instance.launch_time, end_time, running_cost, remaining_balance]
                self._update_usage_db(data, instance.instance_id, end_time=end_time)
                print(Fore.GREEN + f"Instance {instance_name} stopped.")

    def restart_nodes(self, IDs=[], node_names=[], need_confirmation=True, walltime=None):
        if isinstance(node_names, str): node_names = [node_names]
        if isinstance(IDs, str): IDs = [IDs]

        user_name = os.environ['USER']
        walltime_str = walltime if (walltime and walltime != "") else "00:30:00"
        pt = datetime.strptime(walltime_str, "%H:%M:%S")
        walltime_in_minutes = int(pt.hour * 60 + pt.minute + pt.second / 60)

        if not node_names and not IDs:
            raise ValueError("node_names and IDs cannot be both empty.")

        avail_instances = self.get_instances(filters=[{
            "Name": "instance-state-name",
            "Values": ["stopped"]
        }])

        for instance in avail_instances:
            instance_name = self.get_instance_name(instance)
            instance_id = instance.instance_id

            if instance_id not in IDs and instance_name not in node_names:
                continue
            if instance_name in self.account['protected_nodes']:
                continue

            instance_user_name = self.get_instance_user_name(instance)
            if instance_user_name != user_name:
                print(Fore.YELLOW + f"Cannot restart instance {instance_name}: owned by another user.")
                continue

            unit_price = self.get_unit_price_instance(instance)
            if need_confirmation:
                if not self._confirm_budget(user_name, instance.instance_type, unit_price, verb="restart"):
                    return

            print(Fore.BLUE + "Starting instance...")
            instance.start()
            instance.wait_until_running()

            spinner_wait(30, "Waiting for instance to be ready")
            instance.load()
            print(Fore.GREEN + f"Instance {instance_name} is up.")
            self._print_connection_hint(instance_name, user_name,
                                        instance.public_ip_address,
                                        private_key=self.my_ssh_private_key)

    def get_cost_and_usage(self, start_date, end_date, verbose=True):
        client = boto3.client('ce',
                              aws_access_key_id=self.account['access_key_id'],
                              aws_secret_access_key=self.account['secret_access_key'],
                              region_name=self.account['region'])
        response = client.get_cost_and_usage_with_resources(
            TimePeriod={'Start': start_date, 'End': end_date},
            Granularity='DAILY',
            Metrics=['BlendedCost', 'UsageQuantity'],
            Filter={"Dimensions": {"Key": "SERVICE",
                                   'Values': ['Amazon Elastic Compute Cloud - Compute']}},
            GroupBy=[{'Type': 'DIMENSION', 'Key': 'RESOURCE_ID'}]
        )
        if verbose:
            print(response['ResultsByTime'])
        return response

    def get_budget_api(self):
        client = boto3.client('budgets',
                              aws_access_key_id=self.account['access_key_id'],
                              aws_secret_access_key=self.account['secret_access_key'],
                              region_name=self.account['region'])
        response = client.describe_budgets(AccountId=self.account['account_id'])
        print(response)

    def get_running_nodes(self, verbose=False):
        instances = self.get_instances(filters=[{
            "Name": "instance-state-name",
            "Values": ["running"]
        }])
        nodes = []
        for instance in instances:
            nodes.append([self.get_instance_name(instance), instance.state['Name'],
                           instance.instance_type, instance.instance_id])
        if verbose:
            print(tabulate(nodes, headers=['Name', 'Status', 'Type', 'Instance ID']))
            print("")
        return nodes

    def get_host_ip(self, instance_ID):
        if instance_ID.startswith('i-'):
            instances = self.get_instances(filters=[{"Name": "instance-id",
                                                     "Values": [instance_ID]}])
        else:
            instances = self.get_instances(filters=[{"Name": "tag:Name",
                                                     "Values": [instance_ID]}])
        return list(instances)[0].public_ip_address

    def get_all_images(self, owners=['self']):
        try:
            images = self.ec2.images.filter(Owners=owners)
            for image in images:
                print(f"Image ID: {image.id}, Name: {image.name}, Description: {image.description}")
        except Exception as e:
            print(Fore.RED + f"An error occurred: {e}")

    def get_instance_name(self, instance):
        if instance.tags is None:
            return ''
        for tag in instance.tags:
            if tag['Key'] == 'Name':
                return tag['Value']
        return ''

    def get_instance_ID(self, instance_name: str):
        running_instances = self.get_instances(filters=[{
            "Name": "instance-state-name",
            "Values": ["running"]
        }])
        for instance in running_instances:
            if instance.tags is None:
                continue
            for tag in instance.tags:
                if tag['Key'] == 'Name' and tag['Value'] == instance_name:
                    return instance.instance_id
        return ''

    def get_instance_user_name(self, instance):
        if instance.tags is None:
            return ''
        for tag in instance.tags:
            if tag['Key'] == 'User':
                return tag['Value']
        return ''

    def get_instances(self, filters=[]):
        return self.ec2.instances.filter(Filters=filters)

    def get_unit_price_instance(self, instance):
        for node_type in self.vendor['node-types']:
            if self.vendor['node-types'][node_type]['name'] == instance.instance_type:
                return self.vendor['node-types'][node_type]['price']
        return -1.0

    def get_running_cost(self, verbose=True):
        nodes = []
        total_cost = 0.0
        for instance in self.get_instances():
            if self.get_instance_name(instance) in self.account['protected_nodes']:
                continue
            if instance.state['Name'] == 'running':
                running_time = datetime.now(timezone.utc) - instance.launch_time
                instance_unit_cost = self.get_unit_price_instance(instance)
                running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost
                total_cost += running_cost
                nodes.append([self.get_instance_name(instance), instance.state['Name'],
                               instance.instance_type, instance.instance_id,
                               running_time, running_cost])
        if verbose:
            print(tabulate(nodes, headers=['Name', 'Status', 'Type', 'Instance ID',
                                           'Elapsed Time', 'Running Cost']))
        return total_cost

    def get_private_key(self):
        return self.my_ssh_private_key
