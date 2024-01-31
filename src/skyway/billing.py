# Copyright (c) 2019-2024 The University of Chicago.
# Part of skyway, released under the BSD 3-Clause License.

# Maintainer: Yuxing Peng, Trung Nguyen

import os
import yaml
import pandas as pd
from . import cfg
from . import load_config
from . import utils
from .db import DBConnector
from .nodemap import NodeMap

class Billing:
    
    def __init__(self, account_name):
        self.account = account_name
        #budget_cols = 'cloud,startdate,amount,rate'
        #budget_row = DBConnector().select('budget', budget_cols, where="account='%s'" % (account_name), limit='1')[0]
        df = pd.read_csv(f"{cfg['paths']['etc']}/budget.csv")
        # select the rows where account is account_name
        #   convert the rows into a dictionary
        #   get the first (and only) row
        budget_row = (df.loc[df['account'] == account_name]).to_dict(orient="records")[0]

        self.cloud = budget_row['cloud']
        self.budget = {
            'startdate': budget_row['startdate'],
            'amount': budget_row['amount'],
            'rate': budget_row['rate']
        }

        billing_file = cfg['paths']['var'] + 'billing/' + account_name + '.yaml'

        if os.path.isfile(billing_file):
            with open(billing_file, 'r') as f:
                for k,v in yaml.load(f, Loader=yaml.FullLoader).items():
                    if k in ['status']:
                        setattr(self, k, v)
        
        self.cloud_cfg = load_config('cloud')[self.cloud]
    
    def usages(self):
        nt = self.cloud_cfg['node-types']
        #bills = [[t+":"+nt[t]['name'], h, nt[t]['price'] * h] for t,h in NodeMap.history_summary(self.account, self.budget['startdate']).items()]
        
        #rates = [[t+":"+nt[t]['name'], n, nt[t]['price'] * n] for t,n in NodeMap.running_summary(self.account).items()]
        
        #for t,n in NodeMap.running_summary(self.account).items():
        #    print(t,n)
        usage_acct = NodeMap.running_summary(self.account)

        #print(usage_acct['type'].values)
        
            
        #total = sum([bill[-1] for bill in bills])
        pct = 10 #total * 100 / self.budget['amount']

        return {
            'cloud': self.cloud,
            #'bills': bills,
            #'rates': rates,
            'total': 0, #total,
            'rate': 0, #sum([rate[-1] for rate in rates]),
            'budget': self.budget,
            #'balance': self.budget['amount'] - total,
            'pct': pct,
            'status': 'normal' if pct<90 else ('warning' if pct<100 else 'exceeded')
        }
    
    def user_usages(self, user):
        nt = self.cloud_cfg['node-types']
        usages, total = {}, 0.0
        
        for t,h in slurm.usage_nodetime(self.account, user).items():
            ex = h * nt[t]['price']
            usages[t] = [nt[t]['price'], h, ex]
            total += ex
        
        return total, usages
    
    def set(self, key, value):
        DBConnector().update_one('budget', 'account', self.account, {key:value})
        
    
