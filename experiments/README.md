# Delegation experiments

This directory contains experiments for the delegation-aware Secure Swarm Toolkit (SST/IoTAuth): a recorded workflow comparing baseline and proposed authorization, and a warehouse workload that exercises multi-level delegation and cascading revocation across Auth servers.

## Directory guide

| Path | Contents |
| --- | --- |
| [`part1/`](part1/README.md) | Recorded delegation, access, and revocation workflow. |
| `part1/logs/baseline/` | Baseline Auth and node logs. |
| `part1/logs/proposed_approach/` | Delegation-aware Auth and node logs. |
| `part2/dataset/` | Input warehouse CSVs: `Storage_Location.csv` and `Picking_Wave.csv`. |
| `part2/generate_warehouse_realdata.py` | Converts warehouse data into an SST graph, initial policies, layout, and workload. |
| `part2/run_warehouse_experiment_realdata.py` | Runs the workload against local Auth servers and Node.js entities. |
| `part2/warehouse_delegation.py` | Shared delegation-chain naming and height rules. |
| `part2/generated/` | Saved inputs for 2, 3, 4, 5, and 8 Auth configurations. |
| `part2/results/` | Collected JSON results for 2, 3, 4, and 5 Auth configurations. |

## Part 1: recorded workflow

Logs are grouped by approach, then by `Auth_logs/` (server-side policy and privilege processing) and `Node_logs/` (entity-side operations and outcomes). The `t_<k>` filename prefix identifies a workflow stage:

| Stage | Operation |
| --- | --- |
| `t_1`–`t_4` | Grant delegated access. |
| `t_5` | Check access after delegation. |
| `t_6`–`t_9` | Revoke delegated permissions. |
| `t_10` | Check access after revocation. |

The baseline has no built-in revocation operation; its communication policies were removed manually using SQLPro Studio, so no baseline revocation logs are included. 
This part is a log archive; no executable experiment script is included in `part1/`. 
See the [Part 1 README](part1/README.md) for more detail.

## Part 2: warehouse experiment

This experiment uses warehouse storage and picking data to evaluate multi-level delegation, authorization latency, cascading revocation, and Auth database size in the delegation-aware SST/IoTAuth implementation.


To run the experiment, See the [Part 2 README](part2/README.md) for more detail.