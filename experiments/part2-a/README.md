# Delegation and Revocation Experiment

This experiment evaluates delegation, access verification, revocation, and post-revocation access verification for SST-based decentralized access control.

## Experiment Scripts

- `test_proposed_approach.py`: for Linux environments
- `test_proposed_approach_mac.py`: for macOS environments

## Setup

Download the anonymized repository by clicking the **Full repo ZIP** button on the top right. 
After extracting the ZIP file, a directory named `multi-delegation-462E` will be created.

Let `$PROJECT_ROOT` denote the root directory of the extracted repository:

```text
$PROJECT_ROOT/
├── experiments/
│   └── part2-a/
├── iotauth/
└── ...
```
For example, 
```text
cd multi-delegation-462E
export PROJECT_ROOT=$(pwd)
```

1. Create a Python virtual environment and install `networkx`.

```text
python3 -m venv venv
source venv/bin/activate
pip install networkx
```
2. Setup Auth
```text
cd $PROJECT_ROOT/iotauth
find . -type f -name "*.sh" -exec chmod +x {} \;

cd $PROJECT_ROOT/iotauth/auth/auth-server 
make

cd $PROJECT_ROOT/iotauth/examples
./initConfigs.sh 
```

## Running the Experiment

Run the Linux experiment from its script directory. Set the Auth password to `asdf` during setup.

```sh
cd "$PROJECT_ROOT/experiments/part2-a"
python3 test_proposed_approach.py --nodes 10 --resources 5 --output n10r5
```

This example writes seven files under `$PROJECT_ROOT/experiments/part2-a/results/n10r5/`.
The Linux runner uses `--auth-id` (default `101`) to select the Auth server and its database.

The macOS runner assumes the older layout with `test_proposed_approach_mac.py` directly
under `$PROJECT_ROOT/experiments/`. Its startup and graph-generation paths require that
layout and a working directory of `$PROJECT_ROOT/experiments`. It uses Auth 101.

### Saving Terminal Logs
To save terminal output while running the experiment, use:
```text
python3 test_proposed_approach.py --nodes 10 --resources 5 --output n10r5 2>&1 | tee log_name.txt
```
This creates `log_name.txt` in the current working directory.

At the beginning of the `log_name.txt` file, you can find the generated experiment topology, including:
- initial access privileges assigned to head nodes,
- the overall DAG delegation graph and hierarchy, and
- resource-specific delegation edges used to generate delegation and revocation privileges.
Example:
```text
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
```text
"success": false,
"error": "AUTH_FAILURE: Failure pattern detected: Handler: Error in secure comm"
```
Therefore, the number of successful accesses in this file should match the total number of pairs in `n10r5_access_after_revoke.json`.

### 7. `n10r5_latency.json`
Both runners report phase timings, operation counts and means, Auth database sizes at phase boundaries, and access-check outcomes. The Linux runner also records the following measurements.

#### Delegation chains

`chain_count_by_resource` counts root-to-leaf paths with at least one grant in each resource's generated delegation graph, before revocation. Each recipient has at most one parent for that resource. For example, if A delegates to B and B delegates to both C and D, the three grants form two chains. An isolated node contributes zero. `chain_count` sums the resource counts.

#### Database size and individual task latency

Within `auth_db_size_bytes`, `before_workload` is the size before the delegation phase and `after_workload` is the size after the final access checks. `workload_delta` is the latter minus the former. Sizes are file lengths in bytes for `iotauth/auth/databases/auth<ID>/auth.db` alone. An unavailable size or a delta with a missing endpoint is recorded as `null`.

| Task | Individual records for the example run | Identifying fields |
| --- | --- | --- |
| Delegation | `delegation_operations` in `n10r5_latency.json` | `source`, `destination`, `resource` |
| Revocation | `revocation_operations` in `n10r5_latency.json` | `source`, `destination`, `resource` |
| Access before revocation | `n10r5_access_before_revoke_test.json` | `node`, `resource`, `success`, `error` |
| Access after revocation | `n10r5_access_after_revoke_test.json` | `node`, `resource`, `success`, `error` |

Each Linux record includes `latency_ms`, `db_before_bytes`, and `db_after_bytes`. The runner samples the database immediately before and after the timed command, outside its latency interval.

### Average operation latency

Both runners also write these fields to `<name>_latency.json`, using the
per-operation averaging approach from `part2`:

| Mean latency (milliseconds) | Number of samples |
| --- | --- |
| `delegation_latency_ms_mean` | `delegation_count` |
| `authorization_before_revoke_ms_mean` | `pre_revoke_access_count` |
| `revocation_latency_ms_mean` | `revocation_count` |
| `authorization_after_revoke_ms_mean` | `post_revoke_access_count` |

Each sample starts immediately before writing/flushing the command and ends
when its response is detected. Delegation and revocation retain the existing
`disconnected from auth` completion marker. Access ends on successful communication,
authorization failure, or timeout. Queue draining, setup, node restarts, and
result-file writes are excluded from these individual timings. Access averages
include all attempts, including denials and timeouts; a latency does not imply
success. Grant/revoke timeouts still abort the run under the existing behavior.
An empty phase has a count of `0` and a mean of `null`.

Each record in the two `*_access_*_test.json` files now includes `latency_ms`.
The four existing fields without `_mean` remain whole-phase elapsed times for
compatibility; they are not per-operation averages.

## Add Network Latency
If you want to emulate network delay in a Linux environment, add 5 ms latency with 1 ms jitter using:
```text
sudo tc qdisc add dev lo root netem delay 5ms 1ms
```

To remove the network emulation:
```text
sudo tc qdisc del dev lo root
```

## Count Privileges
To count the number of `DelegationGrant` and `DelegationRevoke` privileges, use below command:
```text
python3 count_privileges.py results/n10r5/n10r5.graph
```
Example output:
```text
DelegationGrant : 33
DelegationRevoke: 10
```
