# Copyright (c) 2019-2024 The University of Chicago.
# Part of skyway, released under the BSD 3-Clause License.

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

import oci


class OCI(Cloud):

    def __init__(self, account):
        account_path = self._load_account_config(account, 'oci')
        self.account_path = account_path

        api_key_pem_full_path = account_path + self.account['api_key_name']
        if ".pem" not in api_key_pem_full_path:
            api_key_pem_full_path += ".pem"

        self.config = {
            "user": self.account['user'],
            "key_file": api_key_pem_full_path,
            "fingerprint": self.account['fingerprint'],
            "tenancy": self.account['tenancy'],
            "region": self.account['region'],
        }

        self.identity_client = oci.identity.IdentityClient(self.config)
        self.compute_client = oci.core.ComputeClient(self.config)
        self.compute_client_composite_operations = oci.core.ComputeClientCompositeOperations(
            self.compute_client)

        pem_src = account_path + self.account['private_key']
        self.my_ssh_private_key = "~/.my_oci_ssh_key.pem"
        self._setup_ssh_key(pem_src, self.my_ssh_private_key, chmod=400)

    def list_nodes(self, show_protected_nodes=False, verbose=False):
        instances = self.get_instances()
        nodes = []
        df = pd.read_pickle(self.usage_history)

        for instance in instances:
            node_name = self.get_instance_name(instance)
            if not show_protected_nodes and node_name in self.account['protected_nodes']:
                continue

            if instance.lifecycle_state == 'TERMINATED':
                continue

            if instance.lifecycle_state == 'RUNNING':
                running_time = datetime.now(timezone.utc) - instance.time_created
            else:
                df_node = df.loc[df['InstanceID'] == instance.id]
                if df_node.empty:
                    continue
                end_time = pd.to_datetime(df_node['End'].iloc[0],
                                          format='%Y-%m-%dT%H:%M:%S.%f%z')
                running_time = end_time - instance.time_created

            instance_unit_cost = self.get_unit_price_instance(instance)
            running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost
            public_ip_address = self.get_host_ip(instance.id)
            instance_type = instance.shape
            user_name = self.get_instance_user_name(instance)

            nodes.append([instance.display_name, user_name, instance.lifecycle_state,
                           instance_type, instance.id, public_ip_address,
                           running_time, running_cost])

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
        node_name = node_names[0]
        print(Fore.BLUE + f"Allocating {count} instance...")

        vnic_details = oci.core.models.CreateVnicDetails(
            subnet_id=self.account['subnet_id'],
            assign_public_ip=True,
            display_name='my_instance_vnic',
            hostname_label=node_name,
        )

        public_key_file = self.account_path + "/" + self.account['public_key']
        ssh_pub_key = open(public_key_file).read()

        list_availability_domains_response = oci.pagination.list_call_get_all_results(
            self.identity_client.list_availability_domains,
            self.account['compartment_id']
        )
        availability_domain = list_availability_domains_response.data[0]

        vm_image = self.account['image_id']
        if image_id:
            vm_image = image_id

        instance_details = oci.core.models.LaunchInstanceDetails(
            compartment_id=self.account['compartment_id'],
            availability_domain=availability_domain.name,
            shape=self.vendor['node-types'][node_type]['name'],
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=1, memory_in_gbs=8),
            display_name=node_name,
            create_vnic_details=vnic_details,
            image_id=vm_image,
            metadata={
                'ssh_authorized_keys': ssh_pub_key,
                'Name': node_name,
                'User': user_name,
                'node_type': self.vendor['node-types'][node_type]['name'],
            }
        )

        launch_response = self.compute_client_composite_operations.launch_instance_and_wait_for_state(
            instance_details,
            wait_for_states=[oci.core.models.Instance.LIFECYCLE_STATE_RUNNING]
        )
        instance = launch_response.data

        nodes = {}
        username = self.vendor['username']
        public_ip = self.get_host_ip(instance.id)

        walltime_str = walltime if (walltime and walltime != "") else "00:30:00"
        pt = datetime.strptime(walltime_str, "%H:%M:%S")
        walltime_in_minutes = int(pt.hour * 60 + pt.minute + pt.second / 60)

        instance_type = str(self.vendor['node-types'][node_type]['name'])
        launch_time = instance.time_created.strftime("%Y-%m-%dT%H:%M:%S.%f%z")
        nodes[node_names[0]] = [instance_type, launch_time, str(public_ip)]

        print(f"\nCreated instance: {instance.display_name}")

        running_time = timedelta(hours=pt.hour, minutes=pt.minute, seconds=pt.second)
        instance_unit_cost = self.get_unit_price_instance(instance)
        projected_running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost
        usage, remaining_balance = self.get_cost_and_usage_from_db(user_name=user_name)
        end_time = instance.time_created + running_time

        data = [user_name, instance.id, instance_type,
                launch_time, end_time, projected_running_cost, remaining_balance]
        self._update_usage_db(data, instance.id)

        cmd = (f"ssh -t -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
               f" {username}@{public_ip} 'sudo shutdown -P +{walltime_in_minutes}'")
        spinner_wait(30, "Preparing the instance")
        subprocess.run(cmd, shell=True, text=True, capture_output=True)

        if self.post_boot_script:
            script_file = os.environ['SKYWAYROOT'] + "/etc/accounts/" + self.post_boot_script
            script_cmd = utils.script2cmd(script_file)
            cmd = (f"ssh -t -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
                   f" {user_name}@{public_ip} '{script_cmd}'")
            subprocess.run(cmd, shell=True, text=True, capture_output=True)

        self._print_connection_hint(instance.display_name, username, public_ip,
                                    private_key=self.my_ssh_private_key)
        return nodes

    def connect_node(self, instance_ID, separate_terminal=True):
        print(Fore.BLUE + f"Connecting to instance: {instance_ID}")
        public_ip = ""

        for instance in self.get_instances():
            if instance.id == instance_ID:
                public_ip = self.get_host_ip(instance.id)
                break

        if not public_ip:
            print(Fore.RED + "Could not find a public IP for this instance.")
            return {}

        username = "opc"
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

    def get_node_connection_info(self, instance_ID):
        username = self.vendor['username']
        ip = self.get_host_ip(instance_ID)
        return {
            'private_key': self.my_ssh_private_key,
            'login': f"{username}@{ip}",
        }

    def execute(self, instance_ID: str, **kwargs):
        ip = self.get_host_ip(instance_ID)
        username = self.vendor['username']
        command = " ".join(kwargs.values())
        cmd = (f"ssh -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
               f" {username}@{ip} -t '{command}'")
        subprocess.run(cmd, shell=True, text=True, capture_output=True)

    def execute_script(self, instance_ID: str, script_name: str):
        ip = self.get_host_ip(instance_ID)
        username = self.vendor['username']
        script_cmd = utils.script2cmd(script_name)
        cmd = (f"ssh -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
               f" {username}@{ip} -t 'eval {script_cmd}'")
        subprocess.run(cmd, shell=True, text=True, capture_output=True)

    def destroy_nodes(self, node_names=None, IDs=None, need_confirmation=True):
        user_name = os.environ['USER']
        if node_names is None and IDs is None:
            raise ValueError("node_names and IDs cannot be both empty.")

        instance_list = oci.pagination.list_call_get_all_results(
            self.compute_client.list_instances,
            self.account['compartment_id']
        ).data
        avail_instances = [i for i in instance_list
                           if i.lifecycle_state in ('RUNNING', 'STOPPED')]

        for instance in avail_instances:
            check_nodename = instance.display_name in node_names if node_names else False
            check_id = instance.id in IDs if IDs else False
            if not (check_nodename or check_id):
                continue

            instance_user_name = self.get_instance_user_name(instance)
            if instance_user_name != user_name:
                print(Fore.YELLOW + f"Cannot destroy {instance.display_name}: owned by another user.")
                continue

            running_time = datetime.now(timezone.utc) - instance.time_created
            instance_unit_cost = self.get_unit_price_instance(instance)
            running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost

            if need_confirmation:
                response = input(
                    f"Terminate {instance.display_name} (running cost ${running_cost:.5f})?"
                    f" Data will be removed. (y/n) "
                )
                if response != 'y':
                    continue

            self.compute_client_composite_operations.terminate_instance_and_wait_for_state(
                instance.id,
                operation_kwargs={'preserve_boot_volume': False,
                                  'preserve_data_volumes_created_at_launch': False},
                wait_for_states=[oci.core.models.Instance.LIFECYCLE_STATE_TERMINATED]
            )

            end_time = datetime.now(timezone.utc)
            running_time = end_time - instance.time_created
            running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost
            _, remaining_balance = self.get_cost_and_usage_from_db(user_name=user_name)

            data = [instance_user_name, instance.id, instance.shape,
                    instance.time_created, end_time, running_cost, remaining_balance]
            self._update_usage_db(data, instance.id, end_time=end_time)
            print(Fore.GREEN + f"Instance {instance.display_name} terminated.")

    def stop_nodes(self, IDs=[], node_names=[], need_confirmation=True):
        if isinstance(node_names, str): node_names = [node_names]
        if isinstance(IDs, str): IDs = [IDs]

        user_name = os.environ['USER']

        instance_list = oci.pagination.list_call_get_all_results(
            self.compute_client.list_instances,
            self.account['compartment_id']
        ).data
        running_instances = [i for i in instance_list if i.lifecycle_state == 'RUNNING']

        for instance in running_instances:
            if instance.id not in IDs and instance.display_name not in node_names:
                continue

            instance_user_name = self.get_instance_user_name(instance)
            if instance_user_name != user_name:
                print(Fore.YELLOW + f"Cannot stop {instance.display_name}: owned by another user.")
                continue

            running_time = datetime.now(timezone.utc) - instance.time_created
            instance_unit_cost = self.get_unit_price_instance(instance)
            running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost

            if need_confirmation:
                response = input(f"Stop {instance.display_name} (running cost ${running_cost:.5f})? (y/n) ")
                if response != 'y':
                    continue

            self.compute_client_composite_operations.stop_instance_and_wait_for_state(
                instance.id,
                operation_kwargs={'preserve_boot_volume': True,
                                  'preserve_data_volumes_created_at_launch': True},
                wait_for_states=[oci.core.models.Instance.LIFECYCLE_STATE_STOPPED]
            )

            end_time = datetime.now(timezone.utc)
            running_time = end_time - instance.time_created
            running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost
            _, remaining_balance = self.get_cost_and_usage_from_db(user_name=user_name)

            data = [instance_user_name, instance.id, instance.shape,
                    instance.time_created, end_time, running_cost, remaining_balance]
            self._update_usage_db(data, instance.id, end_time=end_time)
            print(Fore.GREEN + f"Instance {instance.display_name} stopped.")

    def restart_nodes(self, IDs=[], node_names=[], need_confirmation=True, walltime=None):
        if isinstance(node_names, str): node_names = [node_names]
        if isinstance(IDs, str): IDs = [IDs]

        user_name = os.environ['USER']
        walltime_str = walltime if (walltime and walltime != "") else "00:30:00"
        pt = datetime.strptime(walltime_str, "%H:%M:%S")
        walltime_in_minutes = int(pt.hour * 60 + pt.minute + pt.second / 60)

        instance_list = oci.pagination.list_call_get_all_results(
            self.compute_client.list_instances,
            self.account['compartment_id']
        ).data
        stopped_instances = [i for i in instance_list if i.lifecycle_state == 'STOPPED']

        for instance in stopped_instances:
            if instance.display_name not in node_names and instance.id not in IDs:
                continue

            node_user_name = self.get_instance_user_name(instance)
            if node_user_name != user_name:
                print(Fore.YELLOW + f"Cannot restart {instance.display_name}: owned by another user.")
                continue

            unit_price = self.get_unit_price_instance(instance)
            if need_confirmation:
                if not self._confirm_budget(user_name, instance.shape, unit_price, verb="restart"):
                    return

            print(Fore.BLUE + "Starting instance...")
            self.compute_client_composite_operations.start_instance_and_wait_for_state(
                instance.id,
                operation_kwargs={'preserve_boot_volume': True,
                                  'preserve_data_volumes_created_at_launch': True},
                wait_for_states=[oci.core.models.Instance.LIFECYCLE_STATE_RUNNING]
            )

            spinner_wait(30, "Waiting for instance to be ready")
            public_ip_address = self.get_host_ip(instance.id)

            end_time = datetime.now(timezone.utc)
            running_time = end_time - instance.time_created
            running_cost = running_time.total_seconds() / 3600.0 * unit_price
            _, remaining_balance = self.get_cost_and_usage_from_db(user_name=user_name)

            data = [user_name, instance.id, instance.shape,
                    instance.time_created, end_time, running_cost, remaining_balance]
            self._update_usage_db(data, instance.id, end_time=end_time)

            self._print_connection_hint(instance.display_name, user_name, public_ip_address,
                                        private_key=self.my_ssh_private_key)

    def get_running_nodes(self, verbose=False):
        instances = self.get_instances()
        nodes = []
        for instance in instances:
            nodes.append([self.get_instance_name(instance), instance.lifecycle_state,
                           instance.shape, instance.id])
        if verbose:
            print(tabulate(nodes, headers=['Name', 'Status', 'Type', 'Instance ID']))
            print("")
        return nodes

    def get_host_ip(self, instanceID):
        public_ip = ""
        vn_client = oci.core.VirtualNetworkClient(self.config)
        vnic_attachments = self.compute_client.list_vnic_attachments(
            compartment_id=self.account['compartment_id'],
            instance_id=instanceID
        ).data
        if vnic_attachments:
            vnic_id = vnic_attachments[0].vnic_id
            vnic = vn_client.get_vnic(vnic_id).data
            public_ip = vnic.public_ip
        return public_ip

    def get_all_images(self, owners=['self']):
        try:
            list_images_response = oci.pagination.list_call_get_all_results(
                self.compute_client.list_images,
                self.account['compartment_id'],
            )
            for image in list_images_response.data:
                print(f"Image ID: {image.id}, Name: {image.display_name}")
        except Exception as e:
            print(Fore.RED + f"An error occurred: {e}")

    def get_instance_name(self, instance):
        return instance.display_name

    def get_instance_ID(self, instance_name: str):
        for instance in self.get_instances():
            if instance.display_name == instance_name:
                return instance.id
        return ''

    def get_instance_user_name(self, instance):
        if instance.metadata is None:
            return ''
        return instance.metadata.get('User', '')

    def get_instances(self, filters=[]):
        instance_list = oci.pagination.list_call_get_all_results(
            self.compute_client.list_instances, self.account['compartment_id']
        ).data
        return [i for i in instance_list if i.lifecycle_state == 'RUNNING']

    def get_unit_price_instance(self, instance):
        for node_type in self.vendor['node-types']:
            if self.vendor['node-types'][node_type]['name'] == instance.shape:
                return self.vendor['node-types'][node_type]['price']
        return -1.0

    def get_running_cost(self, verbose=True):
        nodes = []
        total_cost = 0.0
        for instance in self.get_instances():
            if self.get_instance_name(instance) in self.account['protected_nodes']:
                continue
            if instance.lifecycle_state == 'RUNNING':
                running_time = datetime.now(timezone.utc) - instance.time_created
                instance_unit_cost = self.get_unit_price_instance(instance)
                running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost
                total_cost += running_cost
                nodes.append([self.get_instance_name(instance), instance.lifecycle_state,
                               instance.shape, instance.id, running_time, running_cost])
        if verbose:
            print(tabulate(nodes, headers=['Name', 'Status', 'Type', 'Instance ID',
                                           'Elapsed Time', 'Running Cost']))
        return total_cost

    @staticmethod
    def get_availability_domain(identity, compartment_id):
        result = oci.pagination.list_call_get_all_results(
            identity.list_availability_domains, compartment_id)
        return result.data[0].name

    def get_private_key(self):
        return self.my_ssh_private_key
