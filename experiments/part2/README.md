# Warehouse delegation experiment

This experiment uses warehouse storage and picking data to evaluate multi-level delegation, authorization latency, cascading revocation, and Auth database size in the delegation-aware SST/IoTAuth implementation.

Each request selects a resource and a worker chain based on the resource's vertical level:

| Resource level | Delegation chain |
| --- | --- |
| `z = 0` | Supervisor → Robot |
| `z = 1` | Supervisor → Robot → Forklift |
| `z >= 2` | Supervisor → Robot → Forklift → Drone |

The runner grants access along the chain, tests each worker's access, revokes the Supervisor → Robot permission, and tests each worker again. Cascading revocation is verified only when the grants and initial access succeed, the revoke succeeds, and every worker receives an explicit denial afterward. A timeout does not count as successful denial.

## Prerequisites

- Python 3.10 or newer and `pandas` for data generation. The runner otherwise uses the Python standard library and the local `warehouse_delegation` module.
- The repository's delegation-aware `iotauth/` implementation, with its dependencies installed and Auth server built.
- Java 11 or newer, Node.js/npm, Maven, and OpenSSL 3 or newer, as described in the [IoTAuth quickstart](../../iotauth/QUICKSTART.md). See also the [example setup instructions](../../iotauth/examples/README.md).
- The built server JAR at `iotauth/auth/auth-server/target/auth-server-jar-with-dependencies.jar`.

From the repository root, build Auth if needed:

```sh
cd iotauth/auth
mvn -pl auth-server -am install -DskipTests
```

Return to the repository root after building, then enter this folder:

```sh
cd ../../experiments/part2
```

All subsequent commands run from `experiments/part2/`. The runner also requires the Node.js packages in `iotauth/entity/node/`; see the [Node.js setup instructions](../../iotauth/entity/node/README.md).

## Generate inputs

Create a Python environment and install the generator dependency:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install pandas
```

Generate a separate four-Auth configuration:

```sh
python generate_warehouse_realdata.py \
  --storage-locations dataset/Storage_Location.csv \
  --picking-wave dataset/Picking_Wave.csv \
  --num-auths 4 \
  --robots-per-zone 5 \
  --forklifts-per-zone 5 \
  --drones-per-zone 5 \
  --resources-per-auth 25 \
  --requests 100 \
  --items-per-position 4 \
  --seed 7 \
  --output-dir generated/custom-auth4
```

`--requests` counts robot-position batches; the total workload request count is `--requests * --items-per-position` (400 in this example). `--workload-order` accepts `random` (the default, seeded) or `chronological` (CSV order). An optional `--navigation-points` CSV can supply navigation positions; without it, robot positions are sampled uniformly within the real bounds.

## Generated files

Each directory under `generated/` holds a matching set of four JSON files. Although it uses a `.graph` extension, `warehouse.graph` is JSON too. Existing configurations are in `auth2/`, `auth3/`, `auth4/`, `auth5/`, and `auth8/`. Generating into an existing directory replaces its files.

| File | Contents and purpose |
| --- | --- |
| `warehouse.graph` | `authList` defines Auth servers and ports; `authTrusts` defines trust links; `assignments` maps entities to Auth IDs; `entityList` defines supervisors, workers, and resources; `privilegeList` defines delegation privileges. Used by IoTAuth configuration generation and the runner. |
| `warehouse.policy.json` | A list of initial communication policies granting the `Supervisors` group resource access, including cryptographic settings and validity fields. These establish the access from which workers receive delegated permissions. The runner passes this file to `generateAll.sh` with `-po`. |
| `warehouse_layout.json` | Warehouse bounds, spatial partition metadata, resource counts, storage and item coordinates, navigation points, and entity counts by Auth. Useful for inspecting the generated scenario; the runner does not take it as an input. Zone bounds are resource envelopes and may overlap. |
| `workload.json` | `metadata` describes generation settings and `requests` contains the actual workload. Each request identifies its resource, supervisor, selected workers, resource height, positions, and whether access crosses Auth domains. The runner executes this list in order. |

In workload records, `delegation_chain` lists workers only; result records prepend the supervisor. Forklifts and drones are selected from separately sized pools in the Robot's home zone. Use the graph, policy, and workload from the same generation run to keep entity names and assignments consistent.

## Visualize the warehouse layout

Use [`plot_warehouse_layout.py`](plot_warehouse_layout.py) to visualize a generated `warehouse_layout.json`. From `experiments/part2/`, install Pillow if you want PNG output:

```sh
python3 -m pip install pillow
```

Generate both SVG and PNG images for the saved four-Auth configuration:

```sh
python3 plot_warehouse_layout.py generated/auth4/warehouse_layout.json \
  -o warehouse-setup.svg --png warehouse-setup.png
```

The command saves two files in the current directory and prints their absolute paths:

- `warehouse-setup.svg`: a scalable vector image suitable for viewing in a browser or editing in a vector graphics tool.
- `warehouse-setup.png`: a raster image suitable for reports and slides.

For SVG output only, omit `--png warehouse-setup.png`; no third-party Python package is required. To visualize your newly generated configuration, replace the input path with `generated/custom-auth4/warehouse_layout.json`. Change the output names to preserve images from previous configurations.

The visualization plots resources at their stored x/y coordinates, colors them by Auth zone, and lists entity counts per Auth. Shaded regions show resource envelopes, which can overlap; they are not exact spatial partition boundaries. Robot positions are omitted because they are not stored in the layout file. Coordinate units are preserved from the source data.

## Run the workload

**The runner automatically invokes `iotauth/examples/cleanAll.sh` and `generateAll.sh` before starting servers. This removes and regenerates example Auth databases, properties, credentials, and entity configurations. Use an experiment checkout whose generated state can be replaced.**

Run a saved configuration, writing new results to a separate directory:

```sh
python3 run_warehouse_experiment_realdata.py \
  --graph generated/auth4/warehouse.graph \
  --workload generated/auth4/workload.json \
  --results results/local-auth4/run.json
```

To run newly generated inputs instead, replace both `generated/auth4/` paths with `generated/custom-auth4/`. Keep `warehouse.policy.json` beside `warehouse.graph`; the runner derives its path from the graph filename.

The runner locates the repository root automatically. Use `--project-root /path/to/multi-delegation` to override it. It executes every workload request and starts only resource servers referenced by the workload. Spawned processes are terminated on exit unless `--keep-processes` is specified.

Useful options include `--validity` (default `1*day`), `--startup-timeout` (10 seconds), `--command-timeout` and `--access-timeout` (5 seconds each), and `--inter-request-delay` (0 seconds). List all options with:

```sh
python3 generate_warehouse_realdata.py --help
python3 run_warehouse_experiment_realdata.py --help
```

## Read the results

For `--results results/local-auth4/run.json`, the runner writes three files and updates them after each completed request:

| File | Contents |
| --- | --- |
| `run.json` | Run configuration and aggregate summary. |
| `run_results.json` | Per-request grants, access attempts, revocation results, and latencies. |
| `run_db_size.json` | Auth database size snapshots. |

### Summary file: `<name>.json`

`configuration` records input paths, selected request count, and validity. `summary` aggregates completed requests:

| Field | Interpretation |
| --- | --- |
| `num_requests` | Number of completed request records. Compare with `configuration.num_selected_requests` to check whether the workload finished. |
| `delegation_count` | Number of recorded grant attempts across all chain edges. |
| `pre_revoke_access_count`, `post_revoke_access_count` | Access attempts across every worker, including denied or timed-out attempts. |
| `revocation_verified_count` | Requests whose grants, pre-revocation access, revocation, and explicit post-revocation denials all passed for the entire chain. |
| `cascading_revocation_request_count`, `cascading_revocation_verified_count` | Requests with more than one worker, and how many passed the complete chain verification. |
| `cross_auth_request_count`, `cross_auth_request_rate` | Requests marked as crossing Auth domains, and their fraction of completed requests. |
| `pre_revoke_access_success_count`, `pre_revoke_access_success_rate` | Robot access successes before revocation, counted per request. |
| `post_revoke_access_denied_count`, `post_revoke_denial_rate` | Robot denials after revocation, counted per request. |
| `delegation_latency_ms_mean` | Mean latency of the first grant (Supervisor → Robot), not all chain grants. |
| `authorization_before_revoke_ms_mean`, `authorization_after_revoke_ms_mean` | Mean Robot access-attempt latency before and after revocation. |
| `revocation_latency_ms_mean` | Mean latency of the Supervisor's revoke operation. |
| `total_quantity_to_pick` | Sum of picking quantities in completed request records. |

The Robot-only success and denial rates do not prove cascading revocation. Use the full-chain verification counts and per-entity records for that check. Latency means include available numeric timings without filtering by success status.

### Request details: `<name>_results.json`

The top-level `results` array contains one record per completed request. It includes source identifiers (`request_id`, `position_id`, `wave_number`, item and storage IDs), selected actors, resource coordinates, and `cross_auth`.

- `delegation_chain`: supervisor followed by the selected workers.
- `delegations`: each grant operation with delegator, delegatee, status, latency, command, and captured output.
- `before_revoke_access_by_entity` / `after_revoke_access_by_entity`: access outcomes for every worker.
- `revocation`: the single Supervisor → Robot revoke operation.
- `all_entities_accessible_before_revoke` / `all_entities_denied_after_revoke`: whole-chain access checks.
- `revocation_verified`: whether the complete request passed all checks.
- `cascading_revocation_verified`: the same check for multi-worker chains; `null` for a Robot-only chain.

Legacy fields `delegation`, `before_revoke_access`, and `after_revoke_access` retain the first grant and Robot access outcomes. Inspect statuses and `output_tail` when diagnosing failed requests; a recorded latency alone does not indicate success.

### Database measurements: `<name>_db_size.json`

The top-level `db_size` array contains snapshots before and after the workload, and after each grant, pre-revocation access attempt, revoke, and post-revocation access attempt.

| Field | Meaning |
| --- | --- |
| `phase` | Measurement stage, such as `before_workload`, `after_delegation`, or `after_workload`. |
| `request_index`, `request_id` | Link to the request; `null` for workload-level snapshots. |
| `auth_db_size_bytes` | Total measured database size across Auth servers. |
| `auth_db_size_delta_bytes` | Signed change from the preceding chronological measurement; `null` for the first or unavailable comparison. |
| `auth_db_size_bytes_by_auth` | Individual file sizes keyed by Auth ID. |
| `errors` | File measurement errors, if any. |

Some snapshots also identify the entity, resource, or delegation edge. The saved array places the before/after workload snapshots first, then the detailed measurements. Deltas still refer to chronological sampling order, not necessarily the preceding array entry.

### Timing and measurement scope

`summary.workload_total_time_ms` measures wall time from the first request start through the last completed request record. It includes intermediate logging, database sampling, saves, and inter-request delays, but excludes server setup, final output saving, and shutdown.

Database snapshots measure only each Auth's `auth.db` file, excluding journals/WAL files, keys, and credentials. Missing or unreadable files are recorded as `null`; totals require all Auth sizes to be available. Sampling occurs outside command latency timers.


## Inspect saved results

Existing result sets are under `results/auth2/`, `results/auth3/`, `results/auth4/`, and `results/auth5/`. For example, `auth4/` contains `auth4.json`, `auth4_results.json`, and `auth4_db_size.json`. Paths inside their configuration sections describe the original run environment.

To print a new run's summary:

```sh
python3 - <<'PYTHON'
import json
from pathlib import Path

report = json.loads(Path("results/local-auth4/run.json").read_text())
print(json.dumps(report["summary"], indent=2))
PYTHON
```

Use a distinct output directory for each run to preserve earlier measurements. Outputs are saved incrementally after completed requests, so an interrupted run may contain partial results. Compare completed and selected request counts before treating a file as a full run.

## Troubleshooting

- **Missing Auth JAR:** build the repository's Auth implementation using the Maven command above.
- **Missing initial policy file:** regenerate the input set and keep `warehouse.policy.json` beside `warehouse.graph`.
- **Workload references missing entities:** use graph and workload files from the same generated directory.
- **Startup or operation timeout:** inspect process output and local port availability; adjust the corresponding timeout option when needed. Timeouts are not successful revocation checks.
- **Leftover processes:** normal runs terminate spawned processes. Runs using `--keep-processes` leave them running for debugging; stop them before another run that uses the same ports.
