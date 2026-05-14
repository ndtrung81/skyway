# Copyright (c) 2019-2024 The University of Chicago.
# Part of skyway, released under the BSD 3-Clause License.

# Maintainer: Trung Nguyen

from datetime import datetime, timezone
import io
import logging
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

from azure.identity import ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.network.models import (PublicIPAddress, PublicIPAddressSku,
                                        NetworkSecurityGroup, SecurityRule)
from azure.core.exceptions import HttpResponseError
from azure.mgmt.compute.models import (HardwareProfile, StorageProfile, OSProfile,
                                        NetworkProfile, LinuxConfiguration,
                                        SshConfiguration, SshPublicKey, VirtualMachine)


class AZURE(Cloud):

    def __init__(self, account):
        account_path = self._load_account_config(account, 'azure')

        public_key_file = account_path + self.account['public_key']
        with open(public_key_file, "r") as f:
            self.public_key = f.read().strip()

        self.private_key_file = account_path + self.account['private_key']
        self.my_ssh_private_key = "~/.my_azure_ssh_key.pem"
        self._setup_ssh_key(self.private_key_file, self.my_ssh_private_key, chmod=600)

        self.credentials = ClientSecretCredential(
            client_id=self.account['client_id'],
            client_secret=self.account['client_secret'],
            tenant_id=self.account['tenant_id'],
        )

    def list_nodes(self, show_protected_nodes=False, verbose=False):
        nodes = []
        current_time = datetime.now(timezone.utc)
        compute_client = ComputeManagementClient(self.credentials, self.account['subscription_id'])

        for node in compute_client.virtual_machines.list_all():
            creation_time = node.time_created
            running_time = current_time - creation_time
            node_type = node.hardware_profile.vm_size
            instance_unit_cost = self.get_unit_price_instance(node)
            running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost
            state = self.get_instance_status(node)
            public_ip = self.get_host_ip(node.name)
            nodes.append([node.name, node.tags.get('user'), state, node_type,
                           node.vm_id, public_ip, running_time, running_cost])

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
        print(Fore.BLUE + f"Allocating {count} instance...")

        node_cfg = self.vendor['node-types'][node_type]
        size_name = node_cfg['name']

        walltime_str = walltime if (walltime and walltime != "") else "00:30:00"
        pt = datetime.strptime(walltime_str, "%H:%M:%S")
        walltime_in_minutes = int(pt.hour * 60 + pt.minute + pt.second / 60)

        location_name = 'Central US'
        subscription_id = self.account['subscription_id']
        resource_client = ResourceManagementClient(self.credentials, subscription_id)
        network_client = NetworkManagementClient(self.credentials, subscription_id)
        compute_client = ComputeManagementClient(self.credentials, subscription_id)

        resource_group_name = self.account['resource_group']
        resource_client.resource_groups.create_or_update(resource_group_name,
                                                          {"location": location_name})

        nodes = {}
        for node_name in node_names:
            vnet_name = f"vnet-{user_name}-{node_name}"
            subnet_name = f"subnet-{user_name}-{node_name}"

            network_client.virtual_networks.begin_create_or_update(
                resource_group_name, vnet_name,
                {"location": location_name,
                 "address_space": {"address_prefixes": ["10.0.0.0/16"]}}
            ).result()

            network_client.subnets.begin_create_or_update(
                resource_group_name, vnet_name, subnet_name,
                {"address_prefix": "10.0.0.0/24"}
            ).result()

            subnet = network_client.subnets.get(resource_group_name, vnet_name, subnet_name)

            nsg_name = f"nsg-{user_name}-{node_name}"
            nsg = network_client.network_security_groups.begin_create_or_update(
                resource_group_name, nsg_name,
                NetworkSecurityGroup(location=location_name)
            ).result()

            network_client.security_rules.begin_create_or_update(
                resource_group_name, nsg.name, "AllowSSH",
                SecurityRule(protocol="Tcp", direction="Inbound",
                             source_address_prefix="*", destination_address_prefix="*",
                             access="Allow", priority=1000, source_port_range="*",
                             destination_port_range="22", name="AllowSSH")
            ).result()

            public_ip_name = f'my_public_ip-{user_name}-{node_name}'
            public_ip = network_client.public_ip_addresses.begin_create_or_update(
                resource_group_name, public_ip_name,
                PublicIPAddress(location=location_name,
                                sku=PublicIPAddressSku(name="Standard"),
                                public_ip_allocation_method="Static")
            ).result()

            nic_name = f"my-nic-{user_name}-{node_name}"
            network_interface = network_client.network_interfaces.begin_create_or_update(
                resource_group_name, nic_name,
                {"location": location_name,
                 "ip_configurations": [{"name": "ipconfig1",
                                         "subnet": {"id": subnet.id},
                                         "public_ip_address": {"id": public_ip.id}}],
                 "network_security_group": NetworkSecurityGroup(id=nsg.id)}
            ).result()

            node = None
            try:
                vm_params = VirtualMachine(
                    location=location_name,
                    hardware_profile=HardwareProfile(vm_size=size_name),
                    storage_profile=StorageProfile(image_reference={
                        "publisher": "Canonical",
                        "offer": "UbuntuServer",
                        "sku": "18.04-LTS",
                        "version": "latest",
                    }),
                    os_profile=OSProfile(
                        computer_name=node_name,
                        admin_username=user_name,
                        linux_configuration=LinuxConfiguration(
                            disable_password_authentication=True,
                            ssh=SshConfiguration(
                                public_keys=[SshPublicKey(
                                    path=f"/home/{user_name}/.ssh/authorized_keys",
                                    key_data=self.public_key)]
                            )
                        )
                    ),
                    network_profile=NetworkProfile(
                        network_interfaces=[{"id": network_interface.id}]),
                    tags={'node_name': node_name, 'user': user_name},
                )
                node = compute_client.virtual_machines.begin_create_or_update(
                    resource_group_name, node_name, vm_params).result()
            except HttpResponseError as e:
                print(Fore.RED + f"VM creation failed: {e.message}")
            except Exception as ex:
                logging.info("Failed to create %s. Reason: %s" % (node_name, str(ex)))

            if node is not None:
                creation_time_str = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f%z')
                nodes[node_name] = [str(node.id), size_name, creation_time_str]

                print(f"\nCreated instance: {node_name}")

                public_ip_address = network_client.public_ip_addresses.get(
                    resource_group_name, public_ip_name)
                public_ip_str = public_ip_address.ip_address

                cmd = (f"ssh -t -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
                       f" {user_name}@{public_ip_str} 'sudo shutdown -P +{walltime_in_minutes}'")
                spinner_wait(30, "Preparing the instance")
                subprocess.run(cmd, shell=True, text=True, capture_output=True)

                if self.post_boot_script:
                    script_file = os.environ['SKYWAYROOT'] + "/etc/accounts/" + self.post_boot_script
                    script_cmd = utils.script2cmd(script_file)
                    cmd = (f"ssh -t -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
                           f" {user_name}@{public_ip_str} '{script_cmd}'")
                    subprocess.run(cmd, shell=True, text=True, capture_output=True)

                self._print_connection_hint(node_name, user_name, public_ip_str,
                                            private_key=self.my_ssh_private_key)

        return nodes

    def execute(self, instance_ID: str, **kwargs):
        compute_client = ComputeManagementClient(self.credentials, self.account['subscription_id'])
        vm = next((i for i in compute_client.virtual_machines.list_all()
                   if i.vm_id == instance_ID), None)
        if vm is None:
            print(Fore.RED + f"No VM found for {instance_ID}.")
            return

        host = self.get_host_ip(vm.name)
        user_name = os.environ['USER']
        command = " ".join(kwargs.values())
        cmd = ("gnome-terminal -q --title='Connecting to the node' -- bash -c "
               f" 'ssh -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
               f" {user_name}@{host}' -t '{command}'")
        subprocess.run(cmd, shell=True, text=True, capture_output=True)

    def connect_node(self, instance_ID, separate_terminal=True):
        compute_client = ComputeManagementClient(self.credentials, self.account['subscription_id'])
        vm = next((i for i in compute_client.virtual_machines.list_all()
                   if i.vm_id == instance_ID), None)
        if vm is None:
            print(Fore.RED + f"No VM found for {instance_ID}.")
            return {}

        host = self.get_host_ip(vm.name)
        user_name = os.environ['USER']
        print(Fore.BLUE + f"Connecting to {instance_ID}  IP: {host}")

        node_info = {
            'private_key': self.my_ssh_private_key,
            'login': f"{user_name}@{host}",
        }

        port = random.randint(15000, 30000)
        cmd = (f"ssh -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
               f" -f -N -L {port}:localhost:{port} {user_name}@{host}")
        print(f"SSH tunnel on port {port}")
        os.system(cmd)

        if separate_terminal:
            cmd = ("gnome-terminal --title='Connecting to the node' -- bash -c "
                   f" 'ssh -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
                   f" {user_name}@{host}; exec bash'")
        else:
            cmd = (f"ssh -i {self.my_ssh_private_key} -o StrictHostKeyChecking=accept-new"
                   f" {user_name}@{host}")
        os.system(cmd)
        return node_info

    def get_node_connection_info(self, instance_ID):
        compute_client = ComputeManagementClient(self.credentials, self.account['subscription_id'])
        vm = next((i for i in compute_client.virtual_machines.list_all()
                   if i.vm_id == instance_ID), None)
        if vm is None:
            print(Fore.RED + f"No VM found for {instance_ID}.")
            return {}
        host = self.get_host_ip(vm.name)
        user_name = os.environ['USER']
        return {
            'private_key': self.my_ssh_private_key,
            'login': f"{user_name}@{host}",
        }

    def _parse_resource_id(self, resource_id):
        parts = resource_id.split('/')
        return parts[4], parts[-1]  # (resource_group, resource_name)

    def destroy_nodes(self, node_names=[], IDs=[], need_confirmation=True):
        if node_names is None:
            node_names = []
        if not node_names:
            raise Exception("No node names provided to destroy.")
        if isinstance(node_names, str):
            node_names = [node_names]

        user_name = os.environ['USER']
        subscription_id = self.account['subscription_id']
        compute_client = ComputeManagementClient(self.credentials, subscription_id)
        network_client = NetworkManagementClient(self.credentials, subscription_id)
        resource_group_name = self.account['resource_group']

        for vm_name in node_names:
            node = compute_client.virtual_machines.get(resource_group_name, vm_name)
            if node is None:
                raise ValueError(f"Node {vm_name} not found.")

            node_user_name = self.get_instance_user_name(node)
            if node_user_name != user_name:
                print(Fore.YELLOW + f"Cannot destroy {vm_name}: owned by another user.")
                continue

            creation_time_str = node.time_created.strftime('%Y-%m-%dT%H:%M:%S.%f%z')
            idx = creation_time_str.find('+')
            creation_time_str = creation_time_str[:idx - 1] + creation_time_str[idx:]
            creation_time = datetime.strptime(creation_time_str, '%Y-%m-%dT%H:%M:%S.%f%z')
            running_time = datetime.now(timezone.utc) - creation_time
            node_type = node.hardware_profile.vm_size
            instance_unit_cost = self.get_unit_price_instance(node)
            running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost

            if need_confirmation:
                response = input(
                    f"Destroy {node.name} (running cost ${running_cost:.5f})?"
                    f" Data will be removed. (y/n) "
                )
                if response != 'y':
                    continue

            end_time = datetime.now(timezone.utc)
            running_cost = (end_time - creation_time).total_seconds() / 3600.0 * instance_unit_cost
            _, remaining_balance = self.get_cost_and_usage_from_db(user_name=user_name)
            data = [node_user_name, node.id, node_type,
                    creation_time, end_time, running_cost, remaining_balance]
            self._update_usage_db(data, node.id)

            try:
                nic_ids = ([nic.id for nic in node.network_profile.network_interfaces]
                           if node.network_profile else [])
                os_disk_name = (node.storage_profile.os_disk.name
                                if node.storage_profile and node.storage_profile.os_disk else None)
                data_disk_names = ([disk.name for disk in node.storage_profile.data_disks]
                                   if node.storage_profile else [])

                print(f"  Deleting VM: {vm_name}")
                compute_client.virtual_machines.begin_delete(resource_group_name, vm_name).result()
                print(Fore.GREEN + f"  VM deleted")

                public_ip_ids, nsg_ids, subnet_ids = [], [], []
                for nic_id in nic_ids:
                    rg, nic_name = self._parse_resource_id(nic_id)
                    try:
                        nic = network_client.network_interfaces.get(rg, nic_name)
                        for ip_config in nic.ip_configurations:
                            if ip_config.public_ip_address:
                                public_ip_ids.append(ip_config.public_ip_address.id)
                            if ip_config.subnet:
                                subnet_ids.append(ip_config.subnet.id)
                        if nic.network_security_group:
                            nsg_ids.append(nic.network_security_group.id)
                        print(f"  Deleting NIC: {nic_name}")
                        network_client.network_interfaces.begin_delete(rg, nic_name).result()
                        print(Fore.GREEN + f"  NIC deleted")
                    except Exception as e:
                        print(Fore.YELLOW + f"  Error deleting NIC {nic_name}: {e}")

                for public_ip_id in public_ip_ids:
                    rg, public_ip_name = self._parse_resource_id(public_ip_id)
                    try:
                        print(f"  Deleting Public IP: {public_ip_name}")
                        network_client.public_ip_addresses.begin_delete(rg, public_ip_name).result()
                        print(Fore.GREEN + f"  Public IP deleted")
                    except Exception as e:
                        print(Fore.YELLOW + f"  Error deleting Public IP {public_ip_name}: {e}")

                for nsg_id in set(nsg_ids):
                    rg, nsg_name = self._parse_resource_id(nsg_id)
                    try:
                        print(f"  Deleting NSG: {nsg_name}")
                        network_client.network_security_groups.begin_delete(rg, nsg_name).result()
                        print(Fore.GREEN + f"  NSG deleted")
                    except Exception as e:
                        print(Fore.YELLOW + f"  Error deleting NSG {nsg_name}: {e}")

                if os_disk_name:
                    try:
                        print(f"  Deleting OS Disk: {os_disk_name}")
                        compute_client.disks.begin_delete(resource_group_name, os_disk_name).result()
                        print(Fore.GREEN + f"  OS Disk deleted")
                    except Exception as e:
                        print(Fore.YELLOW + f"  Error deleting OS Disk {os_disk_name}: {e}")

                for data_disk_name in data_disk_names:
                    try:
                        print(f"  Deleting Data Disk: {data_disk_name}")
                        compute_client.disks.begin_delete(resource_group_name, data_disk_name).result()
                        print(Fore.GREEN + f"  Data Disk deleted")
                    except Exception as e:
                        print(Fore.YELLOW + f"  Error deleting Data Disk {data_disk_name}: {e}")

                vnet_names = set()
                for subnet_id in subnet_ids:
                    parts = subnet_id.split('/')
                    vnet_names.add(parts[-3])

                for vnet_name in vnet_names:
                    try:
                        print(f"  Deleting VNet: {vnet_name}")
                        network_client.virtual_networks.begin_delete(
                            resource_group_name, vnet_name).result()
                        print(Fore.GREEN + f"  VNet deleted")
                    except Exception as e:
                        print(Fore.YELLOW + f"  Error deleting VNet {vnet_name}: {e}")

                print(Fore.GREEN + f"Successfully deleted VM and associated resources.")

            except Exception as e:
                print(Fore.RED + f"Error during deletion: {e}")
                raise

    def get_running_nodes(self, verbose=False):
        nodes = []
        current_time = datetime.now(timezone.utc)
        compute_client = ComputeManagementClient(self.credentials, self.account['subscription_id'])
        for node in compute_client.virtual_machines.list_all():
            state = self.get_instance_status(node)
            if state == "running":
                creation_time = node.time_created
                running_time = current_time - creation_time
                node_type = node.hardware_profile.vm_size
                nodes.append([node.name, state, node_type, node.vm_id, running_time])
        if verbose:
            print(tabulate(nodes, headers=['Name', 'Status', 'Type', 'Instance ID', 'Running Time']))
            print("")
        return nodes

    def get_unit_price_instance(self, node):
        vmtype = node.hardware_profile.vm_size
        for node_type in self.vendor['node-types']:
            if self.vendor['node-types'][node_type]['name'] == vmtype:
                return self.vendor['node-types'][node_type]['price']
        return -1.0

    def get_host_ip(self, node_name):
        try:
            compute_client = ComputeManagementClient(self.credentials, self.account['subscription_id'])
            network_client = NetworkManagementClient(self.credentials, self.account['subscription_id'])
            vm = next((i for i in compute_client.virtual_machines.list_all()
                       if i.name == node_name), None)
            if vm is None:
                return None
            nic_id = vm.network_profile.network_interfaces[0].id
            rg, nic_name = self._parse_resource_id(nic_id)
            nic = network_client.network_interfaces.get(rg, nic_name)
            public_ip_id = nic.ip_configurations[0].public_ip_address.id
            rg, public_ip_name = self._parse_resource_id(public_ip_id)
            public_ip = network_client.public_ip_addresses.get(rg, public_ip_name)
            return public_ip.ip_address
        except (AttributeError, IndexError, TypeError):
            return None

    def get_instance_name(self, node):
        return node.name

    def get_instance_user_name(self, node):
        return node.tags.get('user') if node.tags else 'N/A'

    def get_instance_ID(self, instance_name: str):
        compute_client = ComputeManagementClient(self.credentials, self.account['subscription_id'])
        for node in compute_client.virtual_machines.list_all():
            if node.name == instance_name:
                return node.vm_id
        return ''

    def get_instance_status(self, node):
        resource_group_name = self.account['resource_group']
        compute_client = ComputeManagementClient(self.credentials, self.account['subscription_id'])
        instance_view = compute_client.virtual_machines.instance_view(
            resource_group_name, node.name)
        for status in instance_view.statuses:
            if status.code.startswith('PowerState/'):
                return status.code.split('/')[-1].lower()
        return 'unknown'

    def get_running_cost(self, verbose=True):
        current_time = datetime.now(timezone.utc)
        nodes = []
        total_cost = 0.0
        compute_client = ComputeManagementClient(self.credentials, self.account['subscription_id'])

        for node in compute_client.virtual_machines.list_all():
            if self.get_instance_name(node) in self.account['protected_nodes']:
                continue
            state = self.get_instance_status(node)
            if state != "running":
                continue
            creation_time = node.time_created
            running_time = current_time - creation_time
            node_type = node.hardware_profile.vm_size
            instance_unit_cost = self.get_unit_price_instance(node)
            running_cost = running_time.total_seconds() / 3600.0 * instance_unit_cost
            total_cost += running_cost
            nodes.append([node.name, node_type, running_time, running_cost])

        if verbose:
            print(tabulate(nodes, headers=['Name', 'Type', 'Running Time', 'Running Cost']))
        return total_cost

    def get_private_key(self):
        return self.my_ssh_private_key
