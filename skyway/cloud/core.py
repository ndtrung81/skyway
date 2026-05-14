# Copyright (c) 2019-2024 The University of Chicago.
# Part of skyway, released under the BSD 3-Clause License.

# Maintainer: Yuxing Peng, Trung Nguyen

import os
import subprocess

from colorama import Fore
import pandas as pd
from tabulate import tabulate

from .. import utils


class Cloud():

    def __init__(self, vendor_cfg, kwargs):
        self.vendor = vendor_cfg
        self.onpremises = False

        for k, v in kwargs.items():
            setattr(self, k.replace('-', '_'), v)

    # ── shared init helpers ───────────────────────────────────────────────────

    def _load_account_config(self, account: str, cloud_vendor: str) -> str:
        """Load account + cloud vendor configs, set common attrs. Returns account_path."""
        account_path = os.environ['SKYWAYROOT'] + '/etc/accounts/'
        account_cfg = utils.load_config(account, account_path)
        if account_cfg['cloud'] != cloud_vendor:
            raise Exception(f'Service provider {cloud_vendor} is not associated with this account.')

        for k, v in account_cfg.items():
            setattr(self, k.replace('-', '_'), v)

        self.usage_history = f"{account_path}usage-{account}.pkl"

        vendor_cfg = utils.load_config('cloud')
        if cloud_vendor not in vendor_cfg:
            raise Exception(f'Cloud vendor {cloud_vendor} is undefined.')

        self.vendor = vendor_cfg[cloud_vendor]
        self.account_name = account
        self.onpremises = False
        self.post_boot_script = self.account.get('post_boot_script', '')
        return account_path

    def _setup_ssh_key(self, src_path: str, dest_path: str, chmod: int = 600):
        """Copy an SSH key to dest_path and set its permissions."""
        cmd = f"cp {src_path} {dest_path}; chmod {chmod} {dest_path}"
        subprocess.run(cmd, shell=True, text=True, capture_output=True)

    # ── account info ──────────────────────────────────────────────────────────

    def check_valid_user(self, user_name, verbose=False):
        if user_name not in self.users:
            if verbose:
                print(Fore.YELLOW + f"{user_name} is not listed in the user group of this account.")
            return False
        if verbose:
            print(tabulate([[user_name, self.users[user_name]['budget']]], headers=['User', 'Budget']))
            print("")
        return True

    def get_node_types(self):
        node_info = []
        for node_type, nt in self.vendor['node-types'].items():
            if 'gpu' in nt:
                node_info.append([node_type, nt['name'], nt['cores'], nt['memgb'],
                                   nt['gpu'], nt['gpu-type'], nt['price']])
            else:
                node_info.append([node_type, nt['name'], nt['cores'], nt['memgb'],
                                   "0", "--", nt['price']])
        print(tabulate(node_info,
                       headers=['Name', 'Instance Type', 'CPU Cores', 'Memory (GB)',
                                 'GPU', 'GPU Type', 'Per-hour Cost ($)']))
        print("")

    def get_group_members(self):
        user_info = [[user, self.users[user]['budget']] for user in self.users]
        print(tabulate(user_info, headers=['User', 'Budget']))
        print("")

    # ── billing ───────────────────────────────────────────────────────────────

    def get_budget(self, user_name=None, verbose=True):
        if user_name is not None:
            if user_name not in self.users:
                print(Fore.YELLOW + f"{user_name} is not listed in the user group of this account.")
                return -1
            if verbose:
                print(tabulate([[user_name, self.users[user_name]['budget']]], headers=['User', 'Budget']))
                print("")
            return self.users[user_name]['budget']
        else:
            user_info = []
            total_budget = 0.0
            for name in self.users:
                total_budget += float(self.users[name]['budget'])
                if verbose:
                    user_info.append([name, self.users[name]['budget']])
            if verbose:
                print(tabulate(user_info, headers=['User', 'Budget']))
                print(f"Total: ${total_budget}")
            return total_budget

    def get_budget_api(self):
        pass

    def get_cost_and_usage_from_db(self, user_name):
        if user_name not in self.users:
            raise Exception(f"{user_name} is not listed in the user group of this account.")

        user_budget = self.get_budget(user_name=user_name, verbose=False)

        if not os.path.isfile(self.usage_history):
            data = [user_name, "--", "--", "00:00:00", "00:00:00", 0.0, user_budget]
            df = pd.DataFrame([], columns=['User', 'InstanceID', 'InstanceType', 'Start', 'End', 'Cost', 'Balance'])
            df = pd.concat([pd.DataFrame([data], columns=df.columns), df], ignore_index=True)
            df.to_pickle(self.usage_history)
            return 0, user_budget

        df = pd.read_pickle(self.usage_history)
        df_user = df.loc[df['User'] == user_name].astype({'Cost': float})
        accumulating_cost = df_user['Cost'].sum()
        remaining_balance = float(user_budget) - float(accumulating_cost)
        return accumulating_cost, remaining_balance

    def get_usage_history_from_db(self, user_name):
        if user_name not in self.users:
            raise Exception(f"{user_name} is not listed in the user group of this account.")

        user_budget = self.users[user_name]['budget']

        if not os.path.isfile(self.usage_history):
            data = [user_name, "--", "--", "00:00:00", "00:00:00", 0.0, user_budget]
            df = pd.DataFrame([], columns=['User', 'InstanceID', 'InstanceType', 'Start', 'End', 'Cost', 'Balance'])
            df = pd.concat([pd.DataFrame([data], columns=df.columns), df], ignore_index=True)
            df.to_pickle(self.usage_history)
            return df[['User', 'InstanceID', 'InstanceType', 'Start', 'End']]

        df = pd.read_pickle(self.usage_history)
        df_user = df.loc[df['User'] == user_name]
        return df_user[['User', 'InstanceID', 'InstanceType', 'Start', 'End']]

    def get_unit_price(self, node_type: str):
        if node_type in self.vendor['node-types']:
            return self.vendor['node-types'][node_type]['price']
        return -1.0

    # ── shared operation helpers ──────────────────────────────────────────────

    def _confirm_budget(self, user_name: str, node_type: str, unit_price: float,
                        verb: str = "create") -> bool:
        """Print budget summary, prompt for confirmation. Returns False if cancelled."""
        user_budget = self.get_budget(user_name=user_name, verbose=False)
        usage, _ = self.get_cost_and_usage_from_db(user_name=user_name)
        running_cost = self.get_running_cost(verbose=False)
        usage += running_cost
        remaining_balance = user_budget - usage

        print(f"User budget: ${user_budget:.3f}")
        print(f"+ Usage    : ${usage:.3f}")
        print(f"+ Available: ${remaining_balance:.3f}")

        if remaining_balance <= 0:
            print(Fore.RED + "The current budget is not sufficient for this request.")
            return False

        response = input(f"Do you want to {verb} an instance of type {node_type} (${unit_price}/hr)? (y/n) ")
        return response == 'y'

    def _update_usage_db(self, data: list, instance_id: str, end_time=None):
        """Insert a new usage record or update the End time for an existing one."""
        cols = ['User', 'InstanceID', 'InstanceType', 'Start', 'End', 'Cost', 'Balance']
        if os.path.isfile(self.usage_history):
            df = pd.read_pickle(self.usage_history)
        else:
            df = pd.DataFrame([], columns=cols)

        if end_time is None or instance_id not in df['InstanceID'].values:
            df = pd.concat([pd.DataFrame([data], columns=cols), df], ignore_index=True)
        else:
            df.loc[df['InstanceID'] == instance_id, 'End'] = end_time
        df.to_pickle(self.usage_history)

    def _print_connection_hint(self, node_name: str, username: str, host: str,
                                private_key: str = ""):
        """Print a consistent SSH connection hint after instance creation."""
        key_opt = f"-i {private_key} " if private_key else ""
        print(Fore.GREEN + f"\nInstance '{node_name}' is ready.")
        print(f"  ssh {key_opt}-o StrictHostKeyChecking=accept-new {username}@{host}")
        print(f"  skyway_connect --account={self.account_name} -J {node_name}")

    # ── stubs for subclasses ──────────────────────────────────────────────────

    def list_nodes(self, show_protected_nodes=False, verbose=False):
        pass

    def create_nodes(self, node_type: str, node_names=[], need_confirmation=True,
                     walltime=None, image_id=""):
        pass

    def connect_node(self, node_name, separate_terminal=True):
        pass

    def destroy_nodes(self, node_names, ids, need_confirmation=True):
        pass

    def stop_nodes(self, node_names, ids, need_confirmation=True):
        pass

    def restart_nodes(self, node_names, need_confirmation=True):
        pass

    def get_running_nodes(self, verbose=False):
        pass

    def execute(self, node_name: str, **kwargs):
        pass

    def execute_script(self, node_id: str, script_name: str):
        pass

    def get_host_ip(self, node_name):
        pass

    def get_node_connection_info(self, node_name):
        pass

    def get_instance_name(self, node):
        pass

    def get_instance_user_name(self, node):
        pass

    def get_instances(self, filters=[]):
        pass

    def get_running_cost(self, verbose=True):
        pass

    def get_private_key(self):
        return ""

    @staticmethod
    def create(vendor: str, kwargs):
        vendor = vendor.lower()
        vendor_cfg = utils.load_config('cloud')
        if vendor not in vendor_cfg:
            raise Exception(f'Cloud vendor {vendor} is undefined.')

        from importlib import import_module
        module = import_module('skyway.cloud.' + vendor)
        cloud_class = getattr(module, vendor.upper())
        return cloud_class(vendor_cfg[vendor], kwargs)
