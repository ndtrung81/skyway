# Copyright (c) 2019-2024 The University of Chicago.
# Part of skyway, released under the BSD 3-Clause License.

# pip install apache-libcloud cryptography paramiko

# Maintainer: Trung Nguyen, Yuxing Peng

from datetime import datetime, timezone, timedelta
import io
import os
import random
import subprocess
from tabulate import tabulate
import time

from .core import Cloud
from .. import utils
from ..utils import spinner_wait

from colorama import Fore
import pandas as pd

from libcloud.compute.types import Provider
from libcloud.compute.providers import get_driver
import libcloud.common.google


class GCP(Cloud):

    def __init__(self, account):
        account_path = self._load_account_config(account, 'gcp')

        self.keyfile = account_path + self.account['key_file'] + '.json'
        if not os.path.isfile(self.keyfile):
            raise Exception(f"The key file {self.keyfile} is not found.")

        ComputeEngine = get_driver(Provider.GCE)
        try:
            self.driver = ComputeEngine(self.account['service_account'],
                                        self.keyfile,
                                        project=self.account['project_id'])
        except Exception as e:
            print(Fore.RED + f"GCP driver error: {e}")

        pem_src = account_path + self.account['key_name'] + '.pem'
        self.my_ssh_private_key = "~/.my_gcp_ssh_key.pem"
        self._setup_ssh_key(pem_src, self.my_ssh_private_key, chmod=600)

        assert self.driver is not None

    def list_nodes(self, show_protected_nodes=False, verbose=False):
        nodes = []
        current_time = datetime.now(timezone.utc)
        df = pd.read_pickle(self.usage_history)

        for node in self.driver.list_nodes():
            if node.state == 'terminated':
                continue

            creation_time_str = node.extra.get('creationTimestamp')
            if not creation_time_str:
                continue

            creation_time = datetime.strptime(creation_time_str, '%Y-%m-%dT%H:%M:%S.%f%z')

            if node.state == "running":
                running_time = current_time - creation_time
            else:
                df_node = df.loc[df['InstanceID'] == node.id]
                if df_node.empty:
                    continue
                end_time = pd.to_datetime(df_node['End'].iloc[0],
                                          format='%Y-%m-%dT%H:%M:%S.%f%z')
                running_time = end_time - creation_time

            instance_unit_cost = self.get_unit_price_instance(node)
            running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost
            node_user_name = self.get_instance_user_name(node)
            nodes.append([node.name, node_user_name, node.state, node.size,
                           node.id, node.public_ips[0], running_time, running_cost])

        headers = ['Name', 'User', 'Status', 'Type', 'Instance ID', 'Host', 'Elapsed Time', 'Running Cost']
        output_str = ''
        if verbose:
            print(tabulate(nodes, headers=headers, maxcolwidths=None))
            print("")
        else:
            output_str = io.StringIO()
            print(tabulate(nodes, headers=headers, maxcolwidths=None), file=output_str)
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
        if count <= 0:
            raise Exception('List of node names is empty.')

        print(Fore.BLUE + f"Allocating {count} instance...")

        location_name = self.account['location']
        vpc_name = self.account.get('vpc_name', 'vpc1')
        node_cfg = self.vendor['node-types'][node_type]
        preemptible = node_cfg.get('preemptible', False)

        scopes = ['https://www.googleapis.com/auth/cloud-platform']
        networks = self.driver.ex_list_networks()
        network = next((n for n in networks if n.name == vpc_name), None)
        subnets = self.driver.ex_list_subnetworks()
        subnet = next((s for s in subnets if s.name == location_name), None)

        walltime_str = walltime if (walltime and walltime != "") else "00:30:00"
        pt = datetime.strptime(walltime_str, "%H:%M:%S")
        walltime_in_minutes = int(pt.hour * 60 + pt.minute + pt.second / 60)

        nodes = {}
        for node_name in node_names:
            gpu_type = node_cfg.get('gpu-type') if 'gpu' in node_cfg else None
            gpu_count = node_cfg.get('gpu') if 'gpu' in node_cfg else None

            try:
                vm_image = self.account['image_name']
                if image_id:
                    vm_image = image_id

                cmd = f"ssh-keygen -y -f {self.my_ssh_private_key}"
                p = subprocess.run(cmd, shell=True, text=True, capture_output=True)
                public_key_from_pem = p.stdout.split(' ')[1].strip()
                user_name_pub_key = f"{user_name}:ssh-rsa {public_key_from_pem}"

                node = self.driver.create_node(
                    node_name,
                    size=node_cfg['name'],
                    image=vm_image,
                    location=location_name,
                    ex_network=network,
                    ex_subnetwork=subnet,
                    ex_service_accounts=[{'email': self.account['service_account'],
                                          'scopes': scopes}],
                    ex_labels={'goog-ec-src': 'vm_add-gcloud', 'node_name': node_name,
                               'user': user_name},
                    ex_preemptible=preemptible,
                    ex_accelerator_type=gpu_type,
                    ex_accelerator_count=gpu_count,
                    ex_on_host_maintenance='TERMINATE',
                    ex_tags=[{'node_name': node_name, 'user': user_name}],
                    ex_metadata={'ssh-keys': user_name_pub_key},
                )
                self.driver.wait_until_running([node])

                creation_time_str = node.extra.get('creationTimestamp')
                creation_time = datetime.strptime(creation_time_str, '%Y-%m-%dT%H:%M:%S.%f%z')
                nodes[node_name] = [node_cfg['name'], creation_time_str, node.public_ips[0]]

                print(f'\nCreated instance: {node.name}')

                host = node.public_ips[0]
                running_time = timedelta(hours=pt.hour, minutes=pt.minute, seconds=pt.second)
                instance_unit_cost = self.get_unit_price_instance(node)
                projected_running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost
                usage, remaining_balance = self.get_cost_and_usage_from_db(user_name=user_name)
                end_time = creation_time + running_time

                data = [user_name, node.id, node.size,
                        creation_time_str, end_time, projected_running_cost, remaining_balance]
                self._update_usage_db(data, node.id)

                spinner_wait(30, "Preparing the instance")
                cmd = (f"ssh -t -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
                       f" {user_name}@{host} 'sudo shutdown -P +{walltime_in_minutes}'")
                subprocess.run(cmd, shell=True, text=True, capture_output=True)

                if self.post_boot_script:
                    script_file = os.environ['SKYWAYROOT'] + "/etc/accounts/" + self.post_boot_script
                    script_cmd = utils.script2cmd(script_file)
                    cmd = (f"ssh -t -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
                           f" {user_name}@{host} '{script_cmd}'")
                    subprocess.run(cmd, shell=True, text=True, capture_output=True)

                self._print_connection_hint(node.name, user_name, host,
                                            private_key=self.my_ssh_private_key)

            except libcloud.common.google.ResourceNotFoundError as e:
                print(Fore.RED + f'Resource not found: {e}')
            except libcloud.common.google.GoogleBaseError as e:
                print(Fore.RED + f'Google Cloud error: {e}')
            except Exception as e:
                print(Fore.RED + f"Failed to create {node_name}: {e}")

        return nodes

    def connect_node(self, node_id, separate_terminal=True):
        node = None
        for n in self.driver.list_nodes():
            if n.state == "running" and n.id == node_id:
                node = n
                break

        if node is None:
            print(Fore.RED + f"Node {node_id} not found or not running.")
            return {}

        public_ip = node.public_ips[0]
        username = os.environ['USER']
        print(Fore.BLUE + f"Connecting to {node_id}  IP: {public_ip}")

        port = random.randint(15000, 30000)
        cmd = (f"ssh -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
               f" -f -N -L {port}:localhost:{port} {username}@{public_ip}")
        print(f"SSH tunnel on port {port}")
        os.system(cmd)

        if separate_terminal:
            cmd = ("gnome-terminal -q --title='Connecting to the node' -- bash -c "
                   f" 'module purge; ssh -i {self.my_ssh_private_key}"
                   f" -o StrictHostKeyChecking=accept-new {username}@{public_ip}; exec bash'")
        else:
            cmd = (f"ssh -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
                   f" {username}@{public_ip}")
        os.system(cmd)

        return {
            'private_key': self.my_ssh_private_key,
            'login': f"{username}@{public_ip}",
        }

    def stop_nodes(self, IDs=[], node_names=[], need_confirmation=True):
        if isinstance(node_names, str): node_names = [node_names]
        if isinstance(IDs, str): IDs = [IDs]

        user_name = os.environ['USER']

        for node in self.driver.list_nodes():
            if node.state != "running":
                continue
            if node.name not in node_names and node.id not in IDs:
                continue

            node_user_name = self.get_instance_user_name(node)
            if node_user_name != user_name:
                print(Fore.YELLOW + f"Cannot stop {node.name}: owned by another user.")
                continue

            creation_time_str = node.extra.get('creationTimestamp')
            creation_time = datetime.strptime(creation_time_str, '%Y-%m-%dT%H:%M:%S.%f%z')
            current_time = datetime.now(timezone.utc)
            running_time = current_time - creation_time
            instance_unit_cost = self.get_unit_price_instance(node)
            running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost

            if need_confirmation:
                response = input(f"Stop {node.name} (running cost ${running_cost:.5f})? (y/n) ")
                if response != 'y':
                    continue

            self.driver.ex_stop_node(node)

            _, remaining_balance = self.get_cost_and_usage_from_db(user_name=user_name)
            data = [node_user_name, node.id, node.size,
                    creation_time, current_time, running_cost, remaining_balance]
            self._update_usage_db(data, node.id, end_time=current_time)
            print(Fore.GREEN + f"Instance {node.name} stopped.")

    def restart_nodes(self, IDs=[], node_names=[], need_confirmation=True, walltime=None):
        if isinstance(node_names, str): node_names = [node_names]
        if isinstance(IDs, str): IDs = [IDs]

        user_name = os.environ['USER']
        walltime_str = walltime if (walltime and walltime != "") else "00:30:00"
        pt = datetime.strptime(walltime_str, "%H:%M:%S")
        walltime_in_minutes = int(pt.hour * 60 + pt.minute + pt.second / 60)

        for node in self.driver.list_nodes():
            if node.state != "stopped":
                continue
            if node.name not in node_names and node.id not in IDs:
                continue

            node_user_name = self.get_instance_user_name(node)
            if node_user_name != user_name:
                print(Fore.YELLOW + f"Cannot restart {node.name}: owned by another user.")
                continue

            unit_price = self.get_unit_price_instance(node)
            if need_confirmation:
                if not self._confirm_budget(user_name, node.size, unit_price, verb="restart"):
                    return

            print(Fore.BLUE + "Starting instance...")
            self.driver.ex_start_node(node)
            self.driver.wait_until_running([node])

            spinner_wait(30, "Waiting for instance to be ready")

            creation_time_str = node.extra.get('creationTimestamp')
            creation_time = datetime.strptime(creation_time_str, '%Y-%m-%dT%H:%M:%S.%f%z')
            running_time = timedelta(hours=pt.hour, minutes=pt.minute, seconds=pt.second)
            instance_unit_cost = self.get_unit_price_instance(node)
            projected_running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost
            usage, remaining_balance = self.get_cost_and_usage_from_db(user_name=user_name)
            end_time = creation_time + running_time

            data = [user_name, node.id, node.size,
                    creation_time_str, end_time, projected_running_cost, remaining_balance]
            self._update_usage_db(data, node.id)

            if self.post_boot_script:
                host = node.public_ips[0]
                script_file = os.environ['SKYWAYROOT'] + "/etc/accounts/" + self.post_boot_script
                script_cmd = utils.script2cmd(script_file)
                cmd = (f"ssh -t -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
                       f" {user_name}@{host} '{script_cmd}'")
                subprocess.run(cmd, shell=True, text=True, capture_output=True)

            self._print_connection_hint(node.name, user_name, node.public_ips[0],
                                        private_key=self.my_ssh_private_key)

    def get_node_connection_info(self, node_id):
        node = None
        for n in self.driver.list_nodes():
            if n.state == "running" and n.id == node_id:
                node = n
                break
        if node is None:
            return {}
        public_ip = node.public_ips[0]
        username = self.get_instance_user_name(node)
        return {
            'private_key': self.get_private_key(),
            'login': f"{username}@{public_ip}",
        }

    def execute(self, node_id: str, **kwargs):
        node = next((n for n in self.driver.list_nodes()
                     if n.state == "running" and n.id == node_id), None)
        if node is None:
            print(Fore.RED + f"Node {node_id} not found or not running.")
            return
        host = node.public_ips[0]
        user_name = os.environ['USER']
        command = " ".join(kwargs.values())
        cmd = ("gnome-terminal --title='Connecting to the node' -- bash -c "
               f" 'ssh -o StrictHostKeyChecking=accept-new {user_name}@{host}' -t '{command}'")
        os.system(cmd)

    def execute_script(self, node_id: str, script_name: str):
        node = next((n for n in self.driver.list_nodes()
                     if n.state == "running" and n.id == node_id), None)
        if node is None:
            print(Fore.RED + f"Node {node_id} not found or not running.")
            return
        host = node.public_ips[0]
        user_name = os.environ['USER']
        script_cmd = utils.script2cmd(script_name)
        cmd = (f"ssh -t -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
               f" {user_name}@{host} '{script_cmd}'")
        os.system(cmd)

    def destroy_nodes(self, IDs=[], node_names=[], need_confirmation=True):
        if isinstance(node_names, str): node_names = [node_names]
        if isinstance(IDs, str): IDs = [IDs]

        user_name = os.environ['USER']

        for node in self.driver.list_nodes():
            check_nodename = node.name in node_names if node_names is not None else False
            check_id = node.id in IDs if IDs is not None else False
            if not (check_nodename or check_id):
                continue

            node_user_name = self.get_instance_user_name(node)
            if node_user_name != user_name:
                print(Fore.YELLOW + f"Cannot destroy {node.name}: owned by another user.")
                continue

            creation_time_str = node.extra.get('creationTimestamp')
            creation_time = datetime.strptime(creation_time_str, '%Y-%m-%dT%H:%M:%S.%f%z')
            current_time = datetime.now(timezone.utc)
            running_time = current_time - creation_time
            instance_unit_cost = self.get_unit_price_instance(node)
            running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost

            if need_confirmation:
                response = input(
                    f"Destroy {node.name} (running cost ${running_cost:.5f})?"
                    f" Data will be removed. (y/n) "
                )
                if response != 'y':
                    continue

            self.driver.destroy_node(node)

            _, remaining_balance = self.get_cost_and_usage_from_db(user_name=user_name)
            data = [node_user_name, node.id, node.size,
                    creation_time, current_time, running_cost, remaining_balance]
            self._update_usage_db(data, node.id, end_time=current_time)
            print(Fore.GREEN + f"Instance {node.name} destroyed.")

    def get_running_nodes(self, verbose=False):
        nodes = []
        current_time = datetime.now(timezone.utc)
        for node in self.driver.list_nodes():
            if node.state != "running":
                continue
            creation_time_str = node.extra.get('creationTimestamp')
            if creation_time_str:
                creation_time = datetime.strptime(creation_time_str, '%Y-%m-%dT%H:%M:%S.%f%z')
                running_time = current_time - creation_time
                nodes.append([node.name, node.size, node.id, node.public_ips[0], running_time])
        if verbose:
            print(tabulate(nodes, headers=['Name', 'Type', 'Instance ID', 'Host', 'Running Time']))
            print("")
        return nodes

    def get_unit_price_instance(self, node):
        for node_type in self.vendor['node-types']:
            if self.vendor['node-types'][node_type]['name'] == node.size:
                return self.vendor['node-types'][node_type]['price']
        return -1.0

    def get_host_ip(self, node_name):
        return self.driver.ex_get_node(node_name).public_ips[0]

    def get_instance_user_name(self, node):
        return node.extra.get('labels', {}).get('user', '')

    def get_instance_name(self, node):
        return node.name

    def get_instance_ID(self, instance_name: str):
        for node in self.driver.list_nodes():
            if node.state == "running" and node.name == instance_name:
                return node.id
        return ''

    def get_instances(self, filters=[]):
        return self.driver.list_nodes()

    def get_running_cost(self, verbose=True):
        current_time = datetime.now(timezone.utc)
        nodes = []
        total_cost = 0.0
        for node in self.driver.list_nodes():
            if self.get_instance_name(node) in self.account['protected_nodes']:
                continue
            if node.state != "running":
                continue
            creation_time_str = node.extra.get('creationTimestamp')
            if not creation_time_str:
                continue
            creation_time = datetime.strptime(creation_time_str, '%Y-%m-%dT%H:%M:%S.%f%z')
            running_time = current_time - creation_time
            instance_unit_cost = self.get_unit_price_instance(node)
            running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost
            total_cost += running_cost
            nodes.append([node.name, node.size, node.id, node.public_ips[0],
                           running_time, running_cost])
        if verbose:
            print(tabulate(nodes, headers=['Name', 'Type', 'Instance ID', 'Host',
                                           'Running Time', 'Running Cost']))
        return total_cost

    def get_private_key(self):
        return self.my_ssh_private_key
