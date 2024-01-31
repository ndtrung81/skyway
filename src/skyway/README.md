
`Skyway` is a software package that allows HPC users to access to their cloud accounts, and run their workflows in interactive and batch modes as they do on premises.

From the user perspective, one should be able to run the following commands on the Skyway login node:

### Querying available node types 
```
sinfo-node-types
```

### Submitting interative jobs
```
sinteractive -A rcc-aws -p rcc-aws --constraint=c1 -t 02:00:00
```

### Submitting batch jobs
```
sbatch job.script
```

### Monitoring submitted jobs
```
squeue -u $USER
```

### Cancelling submitted jobs
```
scancel [jobid]
```

From the developer/admin perspectives, one should be able to run the following commands on the Skyway management node.

### Configuring cloud accounts

```
skyway cloud
skyway cloud rcc-aws --test
skyway cloud rcc-aws --connect rcc-aws-t1-001
skyway cloud rcc-aws --connect rcc-io
skyway cloud rcc-aws --ls
skyway cloud rcc-aws --rm i-0ecb224c29fdcb688
```

### Configuring billing for a cloud account

```
skyway billing
skyway billing rcc-aws --set amount=10
skyway billing rcc-aws --set rate=6.0
skyway billing rcc-aws --summary
```

### Managing services

```
skyway service
skyway service --status
skyway service --regist billing
skyway service --restart billing
skyway service --restart cloud-rcc-aws
skyway service --start cloud-rcc-aws
skyway service --stop cloud-rcc-aws
```

After a user is added to a cloud account `.yaml` file

```
skyway slurm --update-conf
```

to update SLURM association.


### Inspecting database and nodes

```
skyway misc.db_test
skyway misc.nodes
skyway misc.nodes --update
skyway misc.sendmail
```

To achieve these, we need to have a database that monitor

* a data structure that maps a cloud account (rcc-aws) with the PI/account holder ID (rcc), cloud vendor id (aws or gcp or azure), the users that belong to, and other metadata (account id, project id, or subscription id)
* a data structure that maps usernames (CNetIDs) with a cloud account (rcc-aws.yaml)

Skyway v1 maintains a database `skyway` with 4 tables:
* `budget`: listing cloud accounts with corresponding start date, budget and max rate ($ per hour) allowed (static)
* `node_map`: listing unique host names for cloud nodes, type, cloud account, runtime instance, ip and startime (dynamic)
* `node_journal` (dynamic): listing the jobs that were running with the corresponding host names (cloud nodes), type, cloud account, runtime instance, startime and endtime stamps
* `user` (seemingly deprecated): listing cloud accounts with the corresponding users