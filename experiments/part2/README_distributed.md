# Blackwell Auth 101 / A6000 workload

- Blackwell: `10.218.101.8`, SSH user `sunyoung`, only Auth 101 runs here.
- A6000: `10.218.100.95`, Auth 102 onward and **all entities/resource servers**, including net1 entities, run here.
- `run_warehouse_experiment_realdata_warmup_a6000.py` copies the original workload implementation and orchestrates both hosts.
- `run_warehouse_experiment_realdata_warmup_blackwell.py` imports startup helpers from the A6000 copy and handles Auth 101 and its cgroup measurements.
- The original `run_warehouse_experiment_realdata_warmup.py` is unchanged. Both new scripts require `warehouse_delegation.py` beside them.

## Prerequisites

Both servers need the same IoTAuth revision, built Java JAR, and the existing Java/Node/OpenSSL dependencies. Run only one experiment at a time in these checkouts. The runner invokes the existing `cleanAll.sh` and replaces generated credentials, databases and entity configurations on **both** servers.

Blackwell additionally needs Linux cgroup v2 with **writable/resettable `memory.peak`**. Older kernels with read-only `memory.peak` are not supported; the worker fails explicitly instead of reporting a warm-up-inclusive peak as a workload peak. A6000 needs SSH and SCP access to Blackwell (key authentication: the script uses `BatchMode=yes`). Establish the SSH host-key trust normally before running.

Place both scripts and `warehouse_delegation.py` in `~/multi-delegation/experiments/part2/` on both machines. From A6000, for example:

```sh
cd ~/multi-delegation/experiments/part2
scp run_warehouse_experiment_realdata_warmup_a6000.py \
    run_warehouse_experiment_realdata_warmup_blackwell.py \
    warehouse_delegation.py \
    sunyoung@10.218.101.8:~/multi-delegation/experiments/part2/
```

An administrator must provide an **empty, writable cgroup parent with the memory controller enabled for children** on Blackwell. Example for a dedicated experiment server with cgroup v2 mounted at `/sys/fs/cgroup`:

```sh
# On Blackwell, administrator setup (once per boot):
sudo mkdir -p /sys/fs/cgroup/warehouse-sunyoung
sudo sh -c 'echo +memory > /sys/fs/cgroup/cgroup.subtree_control'
sudo sh -c 'echo +memory > /sys/fs/cgroup/warehouse-sunyoung/cgroup.subtree_control'
sudo chown sunyoung /sys/fs/cgroup/warehouse-sunyoung \
    /sys/fs/cgroup/warehouse-sunyoung/cgroup.procs \
    /sys/fs/cgroup/warehouse-sunyoung/cgroup.subtree_control
```

For a systemd-managed or shared server, have the administrator supply an appropriately delegated parent instead. Do not move a shell, Python worker, or unrelated process into that empty parent. The worker creates a unique child for Auth 101 and enters it **before executing Java**, so its initial allocations belong to that group. CPU accounting uses `cpu.stat`, which exists without enabling the CPU controller. The worker remains outside the measured child cgroup.

## Run on A6000

```sh
cd ~/multi-delegation/experiments/part2
python3 run_warehouse_experiment_realdata_warmup_a6000.py \
  --graph generated/auth4/warehouse.graph \
  --workload generated/auth4/workload.json \
  --cgroup-parent /sys/fs/cgroup/warehouse-sunyoung \
  --results results/distributed-auth4/run.json
```

The default remote checkout is `/home/sunyoung/multi-delegation`; override with `--remote-project-root` if necessary. Run from an interactive terminal: the generation step uses SSH with a pseudo-terminal for the existing credential/DB password prompts. Enter the original experiment password `asdf` when prompted (the Java startup helper uses it). The worker is automatically started over SSH; do **not** separately start Auth 101.

Execution order:

1. Upload the selected graph and adjacent `.policy.json` to Blackwell's `generated/distributed-input/` directory.
2. Preflight cgroup support, then generate **all** Auth DBs, keys, certificates and entity configurations once on Blackwell. Never independently generate fresh keys on A6000 afterward.
3. Clean A6000 generated artifacts and copy matching artifacts from Blackwell using SCP. This explicitly includes the requested directory:
   `scp -r sunyoung@10.218.101.8:~/multi-delegation/iotauth/entity/credentials/keys ~/multi-delegation/iotauth/entity/credentials/`
   (The code uses the equivalent absolute remote path.) It also copies entity certificates when needed, `entity/auth_certs`, entity configurations, the CA certificate, and each local Auth's DB directory and properties. Auth 101's DB is not copied to A6000.
4. Start Auth 101 on Blackwell, then the remaining Auths and all workload entities on A6000.
5. Run the original warm-up. Then reset `memory.peak` on Blackwell and take a CPU baseline.
6. Run the measured workload, request final metrics, save outputs and stop both hosts' processes.

The relevant Auth ports from the graph must be reachable between servers. All resource-server host fields point at A6000; its resource ports must be reachable by the entities there. `--keep-processes` is intentionally rejected so the remote cgroup and server have a coordinated lifecycle.

## Measurements and outputs

A6000 summary: `summary.auth_resource_usage.by_auth["101"]`.

- `cpu_total_seconds`: final minus baseline `cpu.stat` `usage_usec`, divided by 1e6.
- `measured_seconds`: elapsed monotonic time measured on Blackwell itself, independent of cross-server clock synchronization.
- `cpu_utilization_percent`: `cpu_total_seconds / measured_seconds * 100`; 100% means one CPU core, and multithreaded Java can exceed 100%.
- `memory_peak_bytes`: kernel `memory.peak`, reset after warm-up and read using the **same open file descriptor** throughout the measurement. No periodic memory sampling is used.
- This is **cgroup memory**, including charged cache/kernel memory; it is not Java heap or RSS. Warm-up allocations still resident at the start are included, but the old warm-up peak is reset.
- The interval starts when Blackwell processes the start command and ends when it processes the stop command. It therefore includes SSH command transit at the boundaries and workload gaps, logging, intermediate result saving and DB snapshots; setup, warm-up and shutdown are excluded.

Results are saved incrementally on A6000 with the same summary/DB-size/request-result files as the original runner. Auth 101 DB-size snapshots query Blackwell's live file, not an A6000 copy. Blackwell also saves final metrics to `experiments/part2/results/auth101_blackwell.json`. SSH disconnect/EOF causes the worker to stop its Auth and attempt to save partial metrics; abrupt host failure or SIGKILL cannot guarantee cleanup. A communication failure is an explicit error, not a zero usage result.

All existing `generated/**/*.graph` files have Auth 101's `entityHost` and `authHost` set to Blackwell; other Auths use A6000. Entity/server `host` fields use A6000. Future graph regeneration may restore localhost; the A6000 runner validates addresses and rejects such inputs.

Kernel accounting reference: https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html
