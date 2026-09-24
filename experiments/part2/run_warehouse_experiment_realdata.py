#!/usr/bin/env python3
"""
run_warehouse_experiment_realdata.py

Run the real-data warehouse workload against the SST/IoTAuth implementation.

Expected repository layout (matching the user's existing experiment scripts):

    <project_root>/
    ├── experiments/
    │   └── part2/
    │       ├── run_warehouse_experiment_realdata.py
    │       └── generated/
    │           ├── warehouse.graph
    │           └── workload.json
    └── iotauth/
        ├── examples/
        │   ├── cleanAll.sh
        │   └── generateAll.sh
        ├── auth/
        │   ├── auth-server/target/auth-server-jar-with-dependencies.jar
        │   └── properties/
        └── entity/node/example_entities/
            ├── user.js
            ├── server.js
            └── configs/

Workflow per request:
    1. Supervisor delegates resource access to the selected Robot.
    2. For z=1, Robot delegates to Forklift; for z>=2, Forklift also delegates to Drone.
    3. Every chain member attempts resource access.
    4. Supervisor revokes only Robot's resource access.
    5. Every chain member retries access to verify cascading revocation.

summary.workload_total_time_ms measures elapsed wall time from the first request
start through the last completed request record. It includes intermediate logging,
DB sampling, saves and inter-request delays, but excludes server setup, final
output saving and shutdown. Incremental summaries contain elapsed time so far.

The runner measures:
    - delegation latency
    - pre-revocation authorization latency/result
    - revocation latency
    - post-revocation authorization latency/result
    - each Auth's auth.db file size and total size before the workload and
      after each delegation, access attempt, and revocation

[DB_SIZE] logs show bytes and signed changes since the preceding snapshot.
The first change is N/A. File-size sampling runs outside command latency timers.
Three JSON files are saved, also after each completed request:
    --results <name>.json: configuration and summary
    <name>_db_size.json: db_size (the measurement snapshots)
    <name>_results.json: results (the per-request records)

Forklifts and drones are selected from independently sized pools in the robot home Auth. z=1 delegates through
Forklift; z>=2 delegates through Forklift then Drone. Each chain member is tested
before and after a single Supervisor -> Robot revocation. Verification requires
successful grants, successful access before revocation, a successful revoke,
and explicit access denial for every member afterward (timeouts do not pass).
Legacy singular access/latency fields still describe the Robot/first grant;
delegations and *_access_by_entity contain the full chain results.
Summary delegation_latency_ms_mean averages all recorded delegation operations.
Summary authorization_before_revoke_ms_mean and authorization_after_revoke_ms_mean
average all recorded worker access attempts (Robot, Forklift, and Drone), including
denied/timeout attempts with numeric latencies; legacy records use the Robot result.
Summary delegation_latency_ms_total and revocation_latency_ms_total sum recorded
operation latencies in milliseconds, excluding access attempts and other workload
overhead. Legacy records without delegations contribute their singular grant.

All workload requests are executed; only resource servers referenced by the workload are started.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from warehouse_delegation import request_chain


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run SST warehouse delegation/revocation workload."
    )
    parser.add_argument("--graph", required=True, help="Generated warehouse.graph")
    parser.add_argument("--workload", required=True, help="Generated workload.json")
    parser.add_argument(
        "--project-root",
        default=None,
        help=(
            "Root containing iotauth/. "
            "Default: repository root, two levels above this script directory."
        ),
    )
    parser.add_argument(
        "--results",
        default="results/warehouse_results.json",
        help="Configuration/summary JSON path; also writes <stem>_db_size.json and <stem>_results.json",
    )
    parser.add_argument(
        "--validity",
        default="1*day",
        help="Validity passed to delegateAuthority",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for each Auth/entity startup marker",
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        default=5.0,
        help="Seconds to wait for delegation/revocation command completion",
    )
    parser.add_argument(
        "--access-timeout",
        type=float,
        default=5.0,
        help="Seconds to wait for initComm output",
    )
    parser.add_argument(
        "--inter-request-delay",
        type=float,
        default=0.0,
        help="Optional sleep between requests",
    )
    parser.add_argument(
        "--keep-processes",
        action="store_true",
        help="Do not terminate spawned processes at the end (debugging only)",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class AuthDatabaseSizeLogger:
    """Log auth.db file sizes and changes since the previous measurement.

    Sizes include only auth.db, not WAL/journal files, credentials, or keys.
    A missing/unreadable file yields null, not zero; totals require all Auths.
    """

    def __init__(self, project_root: Path, graph: dict[str, Any]) -> None:
        self.paths = {
            str(auth["id"]): project_root / "iotauth" / "auth" / "databases"
            / f"auth{auth['id']}" / "auth.db"
            for auth in graph["authList"]
        }
        self.snapshots: list[dict[str, Any]] = []

    def record(self, phase: str, request_index: int | None = None,
               request_id: Any = None) -> dict[str, Any]:
        sizes: dict[str, int | None] = {}
        errors = {}
        for auth_id, path in self.paths.items():
            try:
                sizes[auth_id] = path.stat().st_size
            except OSError as exc:
                sizes[auth_id] = None
                errors[auth_id] = f"{type(exc).__name__}: {exc}"

        previous = self.snapshots[-1] if self.snapshots else None
        # previous_sizes = previous["auth_db_size_bytes_by_auth"] if previous else {}
        # deltas = {
        #     auth_id: size - previous_sizes[auth_id]
        #     if size is not None and previous_sizes.get(auth_id) is not None else None
        #     for auth_id, size in sizes.items()
        # }
        total = sum(sizes.values()) if all(size is not None for size in sizes.values()) else None
        previous_total = previous["auth_db_size_bytes"] if previous else None
        total_delta = total - previous_total if total is not None and previous_total is not None else None
        snapshot = {
            "phase": phase,
            "request_index": request_index,
            "request_id": request_id,
            "auth_db_size_bytes": total,
            "auth_db_size_delta_bytes": total_delta,
            "auth_db_size_bytes_by_auth": sizes,
            "errors": errors,
        }
        self.snapshots.append(snapshot)

        def size_text(value: int | None) -> str:
            return "unavailable" if value is None else f"{value} bytes"

        def delta_text(value: int | None) -> str:
            return "N/A" if value is None else f"{value:+d} bytes"

        context = f" request={request_index} request_id={request_id}" if request_index is not None else ""
        lines = [f"[DB_SIZE] phase={phase}{context} "
                 f"total={size_text(total)} delta={delta_text(total_delta)}"]
        for auth_id, size in sizes.items():
            lines.append(f"[DB_SIZE]   Auth{auth_id}: size={size_text(size)} ")
                         # f"delta={delta_text(deltas[auth_id])}")
            if auth_id in errors:
                lines.append(f"[DB_SIZE]   Auth{auth_id}: {errors[auth_id]}")
        print("\n".join(lines), flush=True)
        return snapshot


def start_output_reader(
    proc: subprocess.Popen[str],
    label: str,
) -> queue.Queue[str]:
    output_q: queue.Queue[str] = queue.Queue()

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            print(f"[{label}] {line}")
            output_q.put(line)

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    return output_q


def drain_queue(output_q: queue.Queue[str]) -> list[str]:
    lines = []
    while True:
        try:
            lines.append(output_q.get_nowait())
        except queue.Empty:
            return lines


def wait_for_any_output(
    output_q: queue.Queue[str],
    patterns: list[str],
    timeout: float,
    failure_patterns: list[str] | None = None,
    completion_pattern: str | None = None,
) -> tuple[str | None, list[str]]:
    """
    Wait for a result marker, optionally followed by a completion marker.
    Failure takes precedence over success. With completion_pattern, return only
    after that marker follows a result; a disconnect alone is not a result.
    Returns (result_marker, consumed_lines), or (None, lines) on timeout.
    """
    deadline = time.monotonic() + timeout
    consumed: list[str] = []
    matched: str | None = None
    failures = failure_patterns or []

    while time.monotonic() < deadline:
        remaining = max(0.01, deadline - time.monotonic())
        try:
            line = output_q.get(timeout=min(0.1, remaining))
            consumed.append(line)
            failure = next((p for p in failures if p in line), None)
            if failure is not None:
                matched = failure
            elif matched is None:
                matched = next((p for p in patterns if p in line), None)

            if matched is not None and (
                completion_pattern is None or completion_pattern in line
            ):
                return matched, consumed
        except queue.Empty:
            pass

    return None, consumed


def wait_for_access_result(
    output_q: queue.Queue[str],
    timeout: float,
) -> tuple[str, list[str]]:
    """
    Match the markers used by the user's existing access test:
      success: "switching to IN_COMM"
      failure: "Handler: Error in secure comm"

    Auth disconnection is normal before the resource handshake completes.
    Only IN_COMM confirms that the connection is ready.
    A timeout is kept separate from an explicit authorization/secure-comm denial.
    """
    SUCCESS_PATTERNS = [
        "switching to IN_COMM",
    ]
    DENY_PATTERNS = [
        "Handler: Error in secure comm",
    ]

    deadline = time.monotonic() + timeout
    consumed: list[str] = []

    while time.monotonic() < deadline:
        remaining = max(0.01, deadline - time.monotonic())
        try:
            line = output_q.get(timeout=min(0.1, remaining))
            consumed.append(line)

            if any(pattern in line for pattern in DENY_PATTERNS):
                return "denied", consumed

            if any(pattern in line for pattern in SUCCESS_PATTERNS):
                return "success", consumed
        except queue.Empty:
            pass

    return "timeout", consumed


def send_command(
    proc: subprocess.Popen[str],
    command: str,
) -> None:
    if proc.poll() is not None:
        raise RuntimeError(
            f"Cannot send command; process already exited with code {proc.returncode}"
        )
    if proc.stdin is None:
        raise RuntimeError("Process stdin is unavailable")

    proc.stdin.write(command)
    proc.stdin.flush()


def entity_config_path(
    example_entities_dir: Path,
    entity: dict[str, Any],
) -> Path:
    """
    Convert:
        net1.supervisor -> configs/net1/supervisor.config
        net1.robotA1  -> configs/net1/robotA1.config
        net1.item_x   -> configs/net1/item_x.config
    """
    name = entity["name"]
    if "." not in name:
        raise ValueError(f"Unexpected entity name without net prefix: {name}")

    net, short_name = name.split(".", 1)
    return example_entities_dir / "configs" / net / f"{short_name}.config"


def auth_properties_path(
    auth_dir: Path,
    auth_id: int,
) -> Path:
    """
    Existing experiments use ../properties/exampleAuth101.properties.
    generateAll.sh normally creates exampleAuth<ID>.properties for each Auth.
    """
    return auth_dir.parent / "properties" / f"exampleAuth{auth_id}.properties"


def run_generate_all(
    graph_path: Path,
    project_root: Path,
) -> None:
    examples_dir = project_root / "iotauth" / "examples"

    if not examples_dir.exists():
        raise FileNotFoundError(f"Missing examples directory: {examples_dir}")

    clean_script = examples_dir / "cleanAll.sh"
    generate_script = examples_dir / "generateAll.sh"

    if not clean_script.exists():
        raise FileNotFoundError(clean_script)
    if not generate_script.exists():
        raise FileNotFoundError(generate_script)

    # generateAll.sh in the user's existing scripts is invoked from iotauth/examples
    # with a graph path relative to that directory. Use os.path.relpath rather than
    # assuming experiment/../../ paths.
    policy_path = graph_path.with_suffix(".policy.json")
    if not policy_path.is_file():
        raise FileNotFoundError(
            f"Initial access policy file not found: {policy_path}\n"
            "Run generate_warehouse.py first to generate the graph and policies."
        )
    graph_arg = os.path.relpath(graph_path.resolve(), examples_dir.resolve())
    policy_arg = os.path.relpath(policy_path.resolve(), examples_dir.resolve())
    cmd = ["./generateAll.sh", "-g", str(graph_arg), "-po", str(policy_arg)]

    print("\nGenerating IoTAuth configs/credentials:")
    print(f"  cd {examples_dir}")
    print("  ./cleanAll.sh")
    print(f"  {' '.join(cmd)}")
    print(f"  Initial Supervisor access policies: {policy_path}")

    subprocess.run(
        ["./cleanAll.sh"],
        cwd=examples_dir,
        check=True,
    )
    subprocess.run(
        cmd,
        cwd=examples_dir,
        check=True,
    )


def classify_entities(
    graph: dict[str, Any],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    supervisors = {}
    robots = {}
    resources = {}

    for entity in graph.get("entityList", []):
        group = str(entity.get("group", ""))

        if group.startswith("Supervisor"):
            supervisors[entity["name"]] = entity
        elif group.startswith(("Robot", "Forklift", "Drone")):
            robots[group] = entity
        elif group.startswith("Item_"):
            resources[group] = entity

    return supervisors, robots, resources


def start_auths(
    graph: dict[str, Any],
    auth_dir: Path,
    startup_timeout: float,
) -> tuple[
    dict[int, subprocess.Popen[str]],
    dict[int, queue.Queue[str]],
]:
    jar = auth_dir / "target" / "auth-server-jar-with-dependencies.jar"
    if not jar.exists():
        raise FileNotFoundError(
            f"Auth server jar not found: {jar}\n"
            f"Build the auth server first from {auth_dir.parent}:\n"
            "  mvn -pl auth-server -am install -DskipTests"
        )

    auth_procs: dict[int, subprocess.Popen[str]] = {}
    auth_outputs: dict[int, queue.Queue[str]] = {}

    for auth in graph["authList"]:
        auth_id = int(auth["id"])
        properties = auth_properties_path(auth_dir, auth_id)

        if not properties.exists():
            raise FileNotFoundError(
                f"Missing Auth properties: {properties}\n"
                "Run this script with --generate, or run generateAll.sh first."
            )

        # Preserve the same relative-property style used by the existing scripts.
        properties_arg = os.path.relpath(properties, auth_dir)

        print(f"\nStarting Auth {auth_id}")
        proc = subprocess.Popen(
            [
                "java",
                "-Djava.awt.headless=true",
                "-jar",
                str(jar),
                "-p",
                properties_arg,
            ],
            cwd=auth_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        # Piped stdin has no Java console. In headless mode Auth first asks
        # for y/n confirmation, then the existing experiment DB password.
        if proc.stdin is not None:
            proc.stdin.write("y\nasdf\n")
            proc.stdin.flush()

        output_q = start_output_reader(proc, f"Auth{auth_id}")
        matched, _ = wait_for_any_output(
            output_q,
            ["Started Server@"],
            timeout=startup_timeout,
        )
        if matched is None:
            raise TimeoutError(f"Auth {auth_id} did not become ready in time")

        auth_procs[auth_id] = proc
        auth_outputs[auth_id] = output_q

    return auth_procs, auth_outputs


def start_user_entity(
    group: str,
    entity: dict[str, Any],
    example_entities_dir: Path,
    startup_timeout: float,
) -> tuple[subprocess.Popen[str], queue.Queue[str]]:
    config = entity_config_path(example_entities_dir, entity)
    if not config.exists():
        raise FileNotFoundError(
            f"Missing config for {group}: {config}\n"
            "Run with --generate or inspect generateAll.sh output."
        )

    config_arg = os.path.relpath(config, example_entities_dir)

    proc = subprocess.Popen(
        ["node", "user.js", config_arg],
        cwd=example_entities_dir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    output_q = start_output_reader(proc, group)

    # The user's existing Node clients print "current parameters:" and a prompt.
    matched, lines = wait_for_any_output(
        output_q,
        ["current parameters:", " prompt>"],
        timeout=startup_timeout,
    )
    if matched is None:
        raise TimeoutError(
            f"{group} did not become ready in time. Last output: {lines[-5:]}"
        )

    return proc, output_q


def start_resource_entity(
    group: str,
    entity: dict[str, Any],
    example_entities_dir: Path,
    startup_timeout: float,
) -> tuple[subprocess.Popen[str], queue.Queue[str]]:
    config = entity_config_path(example_entities_dir, entity)
    if not config.exists():
        raise FileNotFoundError(
            f"Missing config for {group}: {config}\n"
            "Run with --generate or inspect generateAll.sh output."
        )

    config_arg = os.path.relpath(config, example_entities_dir)

    proc = subprocess.Popen(
        ["node", "server.js", config_arg],
        cwd=example_entities_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    output_q = start_output_reader(proc, group)
    matched, lines = wait_for_any_output(
        output_q,
        ["Handler: listening on port"],
        timeout=startup_timeout,
    )
    if matched is None:
        raise TimeoutError(
            f"{group} did not become ready in time. Last output: {lines[-5:]}"
        )

    return proc, output_q


def required_entities_from_requests(
    requests: list[dict[str, Any]],
) -> tuple[set[str], set[str], set[str]]:
    supervisors = {req["supervisor"] for req in requests}
    robots = {group for req in requests for group in request_chain(req)}
    resources = {req["resource"] for req in requests}
    return supervisors, robots, resources


def access_attempt(
    robot_proc: subprocess.Popen[str],
    robot_output: queue.Queue[str],
    resource_target: str,
    timeout: float,
) -> dict[str, Any]:
    drain_queue(robot_output)

    start = time.perf_counter()
    send_command(robot_proc, f"initComm {resource_target}\n")
    status, lines = wait_for_access_result(robot_output, timeout=timeout)
    end = time.perf_counter()

    return {
        "status": status,
        "latency_ms": (end - start) * 1000.0,
        "output_tail": lines[-20:],
    }


def delegate(
    supervisor_proc: subprocess.Popen[str],
    supervisor_output: queue.Queue[str],
    robot: str,
    resource: str,
    validity: str,
    timeout: float,
) -> dict[str, Any]:
    drain_queue(supervisor_output)

    command = f"delegateAuthority {robot} {resource} {validity} 1*day 1*hour\n"
    start = time.perf_counter()
    send_command(supervisor_proc, command)

    matched, lines = wait_for_any_output(
        supervisor_output,
        [
            "Finished privilege request",
        ],
        timeout=timeout,
        completion_pattern="disconnected from auth",
        failure_patterns=[
            "Handler: Error in secure comm",
        ],
    )
    end = time.perf_counter()

    return {
        "command": command.strip(),
        "completion_marker": matched,
        "completed": matched == "Finished privilege request",
        "status": (
            "success" if matched == "Finished privilege request"
            else "failed" if matched is not None
            else "timeout"
        ),
        "latency_ms": (end - start) * 1000.0,
        "output_tail": lines[-20:],
    }


def revoke(
    supervisor_proc: subprocess.Popen[str],
    supervisor_output: queue.Queue[str],
    robot: str,
    resource: str,
    timeout: float,
) -> dict[str, Any]:
    drain_queue(supervisor_output)

    command = f"revoke {robot} {resource}\n"
    start = time.perf_counter()
    send_command(supervisor_proc, command)

    matched, lines = wait_for_any_output(
        supervisor_output,
        [
            "Finished privilege request",
        ],
        timeout=timeout,
        completion_pattern="disconnected from auth",
        failure_patterns=[
            "Handler: Error in secure comm",
        ],
    )
    end = time.perf_counter()

    return {
        "command": command.strip(),
        "completion_marker": matched,
        "completed": matched == "Finished privilege request",
        "status": (
            "success" if matched == "Finished privilege request"
            else "failed" if matched is not None
            else "timeout"
        ),
        "latency_ms": (end - start) * 1000.0,
        "output_tail": lines[-20:],
    }


def terminate_process(
    proc: subprocess.Popen[str],
    label: str,
) -> None:
    if proc.poll() is not None:
        return

    print(f"Stopping {label}")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    def avg(field_path: tuple[str, ...]) -> float | None:
        values = []
        for result in results:
            value: Any = result
            for part in field_path:
                if not isinstance(value, dict) or part not in value:
                    value = None
                    break
                value = value[part]
            if isinstance(value, (int, float)):
                values.append(float(value))
        if not values:
            return None
        return sum(values) / len(values)

    # Count actual attempts across every worker, including denied/timeout results.
    # Legacy records contain only the singular Robot access result.
    def access_count(phase: str) -> int:
        return sum(
            len(r[f"{phase}_access_by_entity"])
            if f"{phase}_access_by_entity" in r
            else int(isinstance(r.get(f"{phase}_access"), dict))
            for r in results
        )

    def access_latency_mean(phase: str) -> float | None:
        # Average individual attempts across all workers, not per-chain means.
        # Fall back to the Robot result only when legacy records lack the map.
        values = []
        for result in results:
            if f"{phase}_access_by_entity" in result:
                attempts = result[f"{phase}_access_by_entity"].values()
            else:
                attempts = [result.get(f"{phase}_access", {})]
            for attempt in attempts:
                latency = attempt.get("latency_ms")
                if isinstance(latency, (int, float)):
                    values.append(float(latency))
        return sum(values) / len(values) if values else None

    # Prefer full chains; use the singular grant only for legacy records.
    delegations = [
        operation
        for result in results
        for operation in (
            result["delegations"]
            if "delegations" in result
            else [result["delegation"]] if isinstance(result.get("delegation"), dict) else []
        )
    ]
    delegation_latencies = [
        float(operation["latency_ms"])
        for operation in delegations
        if isinstance(operation.get("latency_ms"), (int, float))
    ]
    revocation_latencies = [
        float(result["revocation"]["latency_ms"])
        for result in results
        if isinstance(result.get("revocation"), dict)
        and isinstance(result["revocation"].get("latency_ms"), (int, float))
    ]

    before_count = access_count("before_revoke")
    after_count = access_count("after_revoke")

    before_success = sum(
        1
        for result in results
        if result["before_revoke_access"]["status"] == "success"
    )
    after_denied = sum(
        1
        for result in results
        if result["after_revoke_access"]["status"] == "denied"
    )
    cross_auth_count = sum(1 for result in results if result.get("cross_auth") is True)
    total_quantity = sum(
        int(result.get("quantity_to_pick", 0) or 0) for result in results
    )

    return {
        "num_requests": len(results),
        "pre_revoke_access_count": before_count,
        "post_revoke_access_count": after_count,
        "delegation_count": len(delegations),
        "cascading_revocation_request_count": sum(r.get("cascading_revocation_verified") is not None for r in results),
        "cascading_revocation_verified_count": sum(r.get("cascading_revocation_verified") is True for r in results),
        "revocation_verified_count": sum(r.get("revocation_verified") is True for r in results),
        "cross_auth_request_count": cross_auth_count,
        "cross_auth_request_rate": (cross_auth_count / len(results) if results else None),
        "total_quantity_to_pick": total_quantity,
        "delegation_latency_ms_mean": (
            sum(delegation_latencies) / len(delegation_latencies)
            if delegation_latencies else None
        ),
        "delegation_latency_ms_total": sum(delegation_latencies, 0.0),
        "revocation_latency_ms_total": sum(revocation_latencies, 0.0),
        "authorization_before_revoke_ms_mean": access_latency_mean("before_revoke"),
        "revocation_latency_ms_mean": avg(("revocation", "latency_ms")),
        "authorization_after_revoke_ms_mean": access_latency_mean("after_revoke"),
        "pre_revoke_access_success_count": before_success,
        "post_revoke_access_denied_count": after_denied,
        "pre_revoke_access_success_rate": (
            before_success / len(results) if results else None
        ),
        "post_revoke_denial_rate": (
            after_denied / len(results) if results else None
        ),
    }


def main() -> None:
    args = parse_args()

    graph_path = Path(args.graph).resolve()
    workload_path = Path(args.workload).resolve()
    result_path = Path(args.results).resolve()
    db_size_path = result_path.with_name(f"{result_path.stem}_db_size.json")
    details_path = result_path.with_name(f"{result_path.stem}_results.json")

    script_dir = Path(__file__).resolve().parent
    project_root = (
        Path(args.project_root).resolve()
        if args.project_root
        else script_dir.parent.parent
    )

    auth_dir = project_root / "iotauth" / "auth" / "auth-server"
    example_entities_dir = (
        project_root / "iotauth" / "entity" / "node" / "example_entities"
    )

    graph = load_json(graph_path)
    workload = load_json(workload_path)
    requests = list(workload["requests"])

    if not requests:
        raise ValueError("No workload requests selected")

    supervisors_by_name, robots_by_group, resources_by_group = classify_entities(graph)

    required_supervisors, required_robots, required_resources = (
        required_entities_from_requests(requests)
    )

    missing_supervisors = required_supervisors - supervisors_by_name.keys()
    missing_robots = required_robots - robots_by_group.keys()
    missing_resources = required_resources - resources_by_group.keys()

    if missing_supervisors or missing_robots or missing_resources:
        raise ValueError(
            "Workload references entities absent from graph:\n"
            f"  supervisors={sorted(missing_supervisors)}\n"
            f"  robots={sorted(missing_robots)}\n"
            f"  resources={sorted(missing_resources)}"
        )

    run_generate_all(graph_path, project_root)

    auth_procs: dict[int, subprocess.Popen[str]] = {}
    supervisor_procs: dict[str, subprocess.Popen[str]] = {}
    supervisor_outputs: dict[str, queue.Queue[str]] = {}
    robot_procs: dict[str, subprocess.Popen[str]] = {}
    robot_outputs: dict[str, queue.Queue[str]] = {}
    resource_procs: dict[str, subprocess.Popen[str]] = {}

    results: list[dict[str, Any]] = []
    db_size_logger = AuthDatabaseSizeLogger(project_root, graph)
    configuration = {
        "graph": str(graph_path),
        "workload": str(workload_path),
        "num_selected_requests": len(requests),
        "validity": args.validity,
    }

    workload_total_time_ms: float | None = None

    def save_outputs() -> dict[str, Any]:
        summary = summarize_results(results)
        summary["workload_total_time_ms"] = workload_total_time_ms
        # Display the final snapshot beside the baseline without changing the
        # chronological recording order used to calculate deltas.
        snapshots = db_size_logger.snapshots
        phases = ("before_workload", "after_workload")
        overview = [s for s in snapshots if s["phase"] in phases]
        details = [s for s in snapshots if s["phase"] not in phases]
        write_json({"db_size": overview + details}, db_size_path)
        write_json({"results": results}, details_path)
        write_json({"configuration": configuration, "summary": summary}, result_path)
        return summary

    try:
        auth_procs, _ = start_auths(
            graph,
            auth_dir=auth_dir,
            startup_timeout=args.startup_timeout,
        )

        print("\nStarting Supervisors")
        for group in sorted(required_supervisors):
            proc, output_q = start_user_entity(
                group,
                supervisors_by_name[group],
                example_entities_dir,
                args.startup_timeout,
            )
            supervisor_procs[group] = proc
            supervisor_outputs[group] = output_q

        print("\nStarting Robots, Forklifts and Drones")
        for group in sorted(required_robots):
            proc, output_q = start_user_entity(
                group,
                robots_by_group[group],
                example_entities_dir,
                args.startup_timeout,
            )
            robot_procs[group] = proc
            robot_outputs[group] = output_q

        print("\nStarting Resources")
        for group in sorted(required_resources):
            proc, _ = start_resource_entity(
                group,
                resources_by_group[group],
                example_entities_dir,
                args.startup_timeout,
            )
            resource_procs[group] = proc

        # Let TCP listeners/Auth connections settle.
        time.sleep(1.0)

        db_size_logger.record("before_workload")

        print("\nRunning workload")
        workload_start = time.perf_counter()
        for index, request in enumerate(requests, start=1):
            supervisor = request["supervisor"]
            robot = request["selected_robot"]
            resource = request["resource"]

            print(
                f"\n[{index}/{len(requests)}] "
                f"zone={request['resource_zone']} "
                f"{supervisor} -> {' -> '.join(request_chain(request))} (resource={resource}) "
                f"(distance={request['selected_robot_distance_m']} m)"
            )

            chain = request_chain(request)
            resource_target = resources_by_group[resource]["name"]
            delegations = []
            parent = supervisor
            for child in chain:
                proc = supervisor_procs[parent] if parent == supervisor else robot_procs[parent]
                output = supervisor_outputs[parent] if parent == supervisor else robot_outputs[parent]
                outcome = delegate(proc, output, child, resource, args.validity, args.command_timeout)
                delegations.append({"delegator": parent, "delegatee": child,
                                    "resource": resource, **outcome})
                snapshot = db_size_logger.record("after_delegation", index, request["request_id"])
                snapshot.update(delegator=parent, delegatee=child, resource=resource)
                parent = child

            before_by_entity = {}
            for group in chain:
                before_by_entity[group] = access_attempt(
                    robot_procs[group], robot_outputs[group], resource_target, args.access_timeout)
                snapshot = db_size_logger.record("after_access_before_revoke", index, request["request_id"])
                snapshot.update(entity=group, resource=resource)

            # Only revoke the first edge. Descendants must lose access through cascading.
            revocation_result = revoke(
                supervisor_proc=supervisor_procs[supervisor],
                supervisor_output=supervisor_outputs[supervisor],
                robot=robot, resource=resource, timeout=args.command_timeout)
            db_size_logger.record("after_revocation", index, request["request_id"])

            after_by_entity = {}
            for group in chain:
                after_by_entity[group] = access_attempt(
                    robot_procs[group], robot_outputs[group], resource_target, args.access_timeout)
                snapshot = db_size_logger.record("after_access_after_revoke", index, request["request_id"])
                snapshot.update(entity=group, resource=resource)

            delegation_result = delegations[0]
            before_access = before_by_entity[robot]
            after_access = after_by_entity[robot]
            all_before = all(v["status"] == "success" for v in before_by_entity.values())
            all_after = all(v["status"] == "denied" for v in after_by_entity.values())
            verified = (all_before and all_after
                        and all(d.get("status") == "success" for d in delegations)
                        and revocation_result.get("status") == "success")

            record = {
                "request_id": request["request_id"],
                "position_id": request.get("position_id"),
                "wave_number": request.get("wave_number"),
                # "operator": request.get("operator"),
                "item_id": request["item_id"],
                "reference": request.get("reference", request["item_id"]),
                "size_us": request.get("size_us"),
                "quantity_to_pick": request.get("quantity_to_pick"),
                "storage_location_id": request["storage_location_id"],
                "resource_zone": request["resource_zone"],
                "selected_robot_home_zone": request.get("selected_robot_home_zone"),
                "cross_auth": request.get("cross_auth"),
                "auth_id": graph["assignments"][supervisor],
                "supervisor": supervisor,
                "robot": robot,
                "resource": resource,
                "selected_robot_distance_m": request[
                    "selected_robot_distance_m"
                ],
                "resource_position": request.get("resource_position"),
                "selected_forklift": request.get("selected_forklift"),
                "selected_drone": request.get("selected_drone"),
                "delegation_chain": [supervisor] + chain,
                "delegations": delegations,
                "before_revoke_access_by_entity": before_by_entity,
                "after_revoke_access_by_entity": after_by_entity,
                "all_entities_accessible_before_revoke": all_before,
                "all_entities_denied_after_revoke": all_after,
                "revocation_verified": verified,
                "cascading_revocation_verified": verified if len(chain) > 1 else None,
                "delegation": delegation_result,
                # "before_revoke_access": before_access,
                "revocation": revocation_result,
                # "after_revoke_access": after_access,
            }
            results.append(record)
            workload_total_time_ms = (time.perf_counter() - workload_start) * 1000.0

            # Save incrementally so an interrupted long run still leaves data.
            save_outputs()

            print(
                "  delegation={:.2f} ms, access-before={} ({:.2f} ms), "
                "revocation={:.2f} ms, access-after={} ({:.2f} ms)".format(
                    delegation_result["latency_ms"],
                    before_access["status"],
                    before_access["latency_ms"],
                    revocation_result["latency_ms"],
                    after_access["status"],
                    after_access["latency_ms"],
                )
            )

            if index < len(requests) and args.inter_request_delay > 0:
                time.sleep(args.inter_request_delay)

        db_size_logger.record("after_workload")
        summary = save_outputs()
        print("\nSummary")
        print(json.dumps(summary, indent=2))
        print(f"\nWrote configuration/summary: {result_path}")
        print(f"Wrote database sizes: {db_size_path}")
        print(f"Wrote request results: {details_path}")

    finally:
        if not args.keep_processes:
            for group, proc in resource_procs.items():
                terminate_process(proc, group)
            for group, proc in robot_procs.items():
                terminate_process(proc, group)
            for group, proc in supervisor_procs.items():
                terminate_process(proc, group)
            for auth_id, proc in auth_procs.items():
                terminate_process(proc, f"Auth{auth_id}")


if __name__ == "__main__":
    main()
