#!/usr/bin/env python3
"""
run_warehouse_experiment.py

Run the generated warehouse workload against the SST/IoTAuth implementation.

Expected repository layout (matching the user's existing experiment scripts):

    <project_root>/
    ├── experiments/
    │   ├── run_warehouse_experiment.py
    │   └── generated/
    │       ├── warehouse.graph
    │       └── workload.json
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
    1. Use the workload's selected nearest robot.
    2. Supervisor executes:
           delegateAuthority <Robot> <Resource> <validity>
    3. Robot executes:
           initComm <Resource>
    4. Supervisor executes:
           revoke <Robot> <Resource>
    5. Robot retries:
           initComm <Resource>
       and the experiment records whether post-revocation access is denied.

The runner measures:
    - delegation latency
    - pre-revocation authorization latency/result
    - revocation latency
    - post-revocation authorization latency/result

All workload requests are executed, and every resource server in the graph is started.
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
            "Default: parent of the directory containing this script."
        ),
    )
    parser.add_argument(
        "--results",
        default="results/warehouse_results.json",
        help="Output result JSON",
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
        elif group.startswith("Robot"):
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

        # Existing Auth startup scripts provide a DB password through stdin.
        # Keep the same default used in the user's previous experiment code.
        if proc.stdin is not None:
            proc.stdin.write("asdf\n")
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
    robots = {req["selected_robot"] for req in requests}
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

    return {
        "num_requests": len(results),
        "delegation_latency_ms_mean": avg(("delegation", "latency_ms")),
        "authorization_before_revoke_ms_mean": avg(
            ("before_revoke_access", "latency_ms")
        ),
        "revocation_latency_ms_mean": avg(("revocation", "latency_ms")),
        "authorization_after_revoke_ms_mean": avg(
            ("after_revoke_access", "latency_ms")
        ),
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

    script_dir = Path(__file__).resolve().parent
    project_root = (
        Path(args.project_root).resolve()
        if args.project_root
        else script_dir.parent
    )

    examples_dir = project_root / "iotauth" / "examples"
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

        print("\nStarting Robots")
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
        for group in sorted(resources_by_group):
            proc, _ = start_resource_entity(
                group,
                resources_by_group[group],
                example_entities_dir,
                args.startup_timeout,
            )
            resource_procs[group] = proc

        # Let TCP listeners/Auth connections settle.
        time.sleep(1.0)

        print("\nRunning workload")
        for index, request in enumerate(requests, start=1):
            supervisor = request["supervisor"]
            robot = request["selected_robot"]
            resource = request["resource"]

            print(
                f"\n[{index}/{len(requests)}] "
                f"zone={request['resource_zone']} "
                f"{supervisor} -> {robot} -> {resource} "
                f"(distance={request['selected_robot_distance_m']} m)"
            )

            delegation_result = delegate(
                supervisor_proc=supervisor_procs[supervisor],
                supervisor_output=supervisor_outputs[supervisor],
                robot=robot,
                resource=resource,
                validity=args.validity,
                timeout=args.command_timeout,
            )

            resource_target = resources_by_group[resource]["name"]

            before_access = access_attempt(
                robot_proc=robot_procs[robot],
                robot_output=robot_outputs[robot],
                resource_target=resource_target,
                timeout=args.access_timeout,
            )

            revocation_result = revoke(
                supervisor_proc=supervisor_procs[supervisor],
                supervisor_output=supervisor_outputs[supervisor],
                robot=robot,
                resource=resource,
                timeout=args.command_timeout,
            )

            after_access = access_attempt(
                robot_proc=robot_procs[robot],
                robot_output=robot_outputs[robot],
                resource_target=resource_target,
                timeout=args.access_timeout,
            )

            record = {
                "request_id": request["request_id"],
                "resource_zone": request["resource_zone"],
                "auth_id": graph["assignments"][supervisor],
                "supervisor": supervisor,
                "robot": robot,
                "resource": resource,
                "item_id": request["item_id"],
                "storage_location_id": request["storage_location_id"],
                "selected_robot_distance_m": request[
                    "selected_robot_distance_m"
                ],
                "delegation": delegation_result,
                "before_revoke_access": before_access,
                "revocation": revocation_result,
                "after_revoke_access": after_access,
            }
            if "position_id" in request:
                record["position_id"] = request["position_id"]
            results.append(record)

            # Save incrementally so an interrupted long run still leaves data.
            partial = {
                "summary": summarize_results(results),
                "results": results,
            }
            write_json(partial, result_path)

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

            if args.inter_request_delay > 0:
                time.sleep(args.inter_request_delay)

        final_output = {
            "configuration": {
                "graph": str(graph_path),
                "workload": str(workload_path),
                "num_selected_requests": len(requests),
                "validity": args.validity,
            },
            "summary": summarize_results(results),
            "results": results,
        }
        write_json(final_output, result_path)

        print("\nSummary")
        print(json.dumps(final_output["summary"], indent=2))
        print(f"\nWrote results: {result_path}")

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
