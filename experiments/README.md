# Delegation and Revocation Experiment

This experiment evaluates delegation, access verification, revocation, and post-revocation access verification for SST-based decentralized access control.

## Experiment Scripts

- `test_proposed_approach.py`: for Linux environments
- `test_proposed_approach_mac.py`: for macOS environments

## Setup

Download the anonymized repository by clicking the `Full repo ZIP` button on the top right. 
After extracting the ZIP file, a directory named `multi-delegation-462E` will be created.

Let `$PROJECT_ROOT` denote the root directory of the extracted repository:

```text
$PROJECT_ROOT/
├── experiment/
├── iotauth/
└── ...
```
For example, 
```
cd multi-delegation-462E
export PROJECT_ROOT=$(pwd)
```

1. Create a Python virtual environment and install `networkx`.

```
python3 -m venv venv
source venv/bin/activate
pip install networkx
```
2. Setup Auth
```
cd $PROJECT_ROOT/iotauth
find . -type f -name "*.sh" -exec chmod +x {} \;

cd $PROJECT_ROOT/iotauth/auth/auth-server 
make

cd $PROJECT_ROOT/iotauth/examples
./initConfigs.sh 
```

## Running the Experiment
After building Auth, back to the experiments folder.
```
cd $PROJECT_ROOT/experiments
```

The number of nodes, the number of resources, and the output directory name can be manually configured.

⚠️ **Important Note!** Set Auth password as `asdf`.

#### For Linux:
```
python3 test_proposed_approach.py --nodes 10 --resources 5 --output n10r5
```

#### For macOS:
```
python3 test_proposed_approach_mac.py --nodes 10 --resources 5 --output n10r5
```

This command creates the following output directory:
```
$PROJECT_ROOT/experiment/results/n10r5/
```

### Saving Terminal Logs
To save terminal output while running the experiment, use:
```
python3 test_proposed_approach.py --nodes 10 --resources 5 --output n10r5 2>&1 | tee log_name.txt
```
This creates `log_name.txt` inside the `experiment` directory where the Python script is executed.

At the beginning of the `log_name.txt` file, you can find the generated experiment topology, including:
- initial access privileges assigned to head nodes,
- the overall DAG delegation graph and hierarchy, and
- resource-specific delegation edges used to generate delegation and revocation privileges.
Example:
```
Required initial access for head node(s):
  Node1: Resource1, Resource2, Resource3, Resource4, Resource5
  ...
Overall DAG delegation graph:
  Node1 -> Node2
  Node1 -> Node5
  ...
Overall DAG hierarchy:
  Node1 -> Node2, Node5, Node9
  ...

Resource-specific DAG delegation edges:
[Resource1]
  Node7 -> Node10
  Node2 -> Node3
  ...
[Resource2]
  Node1 -> Node9
  Node2 -> Node10
  ...
```


## Output Files
The experiment generates seven output files in `results/n10r5` directory.

### 1. `n10r5.graph`
Graph file used to generate the Auth database.

It contains:
- node and resource configurations
- delegation privileges
- revocation privileges

This file is automatically referenced by the Python experiment script.

### 2. `n10r5.json`
Initial access policy file used when generating the Auth database.

It contains the initial access privileges that nodes should have before delegation. 
This file is also automatically referenced by the Python experiment script.

### 3. `n10r5_access_before_revoke.json`
Expected node-resource access pairs after delegation.

This corresponds to the set `P_d` i.e., the node-resource pairs that should be authorized after successful delegation.

### 4. `n10r5_access_before_revoke_test.json`
Actual access-checking results using `n10r5_access_before_revoke.json.`

Each pair is tested using `initComm`.

### 5. `n10r5_access_after_revoke.json`
Expected node-resource access pairs after revocation.

This corresponds to the set `P_r` i.e., the node-resource pairs that should remain authorized after revocation.

### 6. `n10r5_access_after_revoke_test.json`
Actual access-checking results using `n10r5_access_before_revoke.json.`

The script tests the original pre-revocation access pairs and records which accesses still succeed after revocation. 
Pairs that lose access after revocation produce an authorization failure, such as:
```
"success": false,
"error": "AUTH_FAILURE: Failure pattern detected: Handler: Error in secure comm"
```
Therefore, the number of successful accesses in this file should match the total number of pairs in `n10r5_access_after_revoke.json`.

### 7. `n10r5_latency.json`
Summary file containing Auth database size, latency, and access-checking results for each phase.

Example:
```
{
    "auth_db_size_bytes": {
        "before_delegation": 77824,
        "after_delegation": 77824,
        "after_access_checking1": 98304,
        "before_revocation": 98304,
        "after_revocation": 98304,
        "after_access_checking2": 106496
    },
    "delegation_latency_ms": 2057.925143977627,
    "before_revoke_access_latency_ms": 5916.511356830597,
    "revocation_latency_ms": 532.8580350615084,
    "after_revoke_access_latency_ms": 5111.449738033116,
    "access_check_before": {
        "success": 50,
        "total": 50
    },
    "access_check_after": {
        "success": 36,
        "expected_success": 36,
        "total_access_checking": 50
    }
}
```

## Add Network Latency
If you want to emulate network delay in a Linux environment, add 5 ms latency with 1 ms jitter using:
```
sudo tc qdisc add dev lo root netem delay 5ms 1ms
```

To remove the network emulation:
```
sudo tc qdisc del dev lo root
```
