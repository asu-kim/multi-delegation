#!/usr/bin/env python3
import argparse
import os
import re
import subprocess
import sys
import time
import json
import uuid
import logging
import statistics

READY_PATTERNS = [
    re.compile(r"\binitializing\.\.\.\b", re.IGNORECASE),
    re.compile(r"\bcurrent parameters\b", re.IGNORECASE),
]

RESPONSE_PATTERNS = [
    re.compile(r"\bdisconnected from auth\b", re.IGNORECASE),
]
ACCESS_RESPONSE_PATTERNS = [
    re.compile(r"\bswitching to IN_COMM\b", re.IGNORECASE),
]
NODE_RESOURCE_MAP = {
    "Node0": ["ResourceA", "ResourceB", "ResourceC", "ResourceD"],
    "Node1": ["ResourceA", "ResourceB"],
    "Node2": ["ResourceC", "ResourceD"],
    "Node3": ["ResourceA"],
    "Node4": ["ResourceB"],
    "Node5": ["ResourceC"],
    "Node6": ["ResourceD"],
}

LOG_FILE = f"scenario1_delegate2_log_{int(time.time())}.txt"


def setup_logging(log_file: str):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ],
    )


def matches_any(line: str, patterns) -> bool:
    return any(p.search(line) for p in patterns)


def run_and_measure(workdir, node_cmd, send_command, timeout_sec, pattern):
    t0 = time.perf_counter()
    workflow_id = "wf-" + uuid.uuid4().hex[:12]

    proc = subprocess.Popen(
        node_cmd,
        cwd=workdir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    sent = False
    t_deadline = t0 + timeout_sec

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            now = time.perf_counter()
            logging.info("[node] %s", line.rstrip("\n"))

            if now > t_deadline:
                raise TimeoutError(f"Timeout ({timeout_sec}s) waiting for response markers.")

            if (not sent) and ("current parameters" in line.lower() or matches_any(line, READY_PATTERNS)):
                if proc.stdin is not None:
                    proc.stdin.write(send_command + "\n")
                    proc.stdin.flush()
                    sent = True

            if matches_any(line, pattern):
                latency = now - t0
                return latency

        raise RuntimeError("Process ended before response markers were observed.")
    finally:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass

def resource_to_net_name(resource_name: str) -> str:
    """
    ResourceA -> net1.resourceA
    """
    if not resource_name.startswith("Resource"):
        raise ValueError(f"Unexpected resource name: {resource_name}")
    suffix = resource_name[len("Resource"):]   # A, B, C, D
    return f"net1.resource{suffix}"

def check_all_authorized_accesses(workdir, timeout_sec):
    results = []
    success_count = 0
    total_count = 0
    total_latency_sec = 0.0

    for node_name, resources in NODE_RESOURCE_MAP.items():
        node_config_num = node_name.replace("Node", "")   # "Node2" -> "2"
        node_cmd = f"node user.js configs/net1/node{node_config_num}.config"

        logging.info("\n" + "=" * 50)
        logging.info("Checking access for %s", node_name)
        logging.info("=" * 50)

        for resource_name in resources:
            target = resource_to_net_name(resource_name)
            send_command = f"initComm {target}"
            total_count += 1

            logging.info("\n=== Start process: %s access %s ===", node_name, resource_name)
            logging.info("Command: %s", node_cmd)
            logging.info("Send command: %s", send_command)

            try:
                access_latency_sec = run_and_measure(
                    workdir=workdir,
                    node_cmd=node_cmd.split(),
                    send_command=send_command,
                    timeout_sec=timeout_sec,
                    pattern=ACCESS_RESPONSE_PATTERNS,
                )

                success = True
                success_count += 1

                logging.info("Result: SUCCESS")
                logging.info("Latency: %.6f seconds (%.2f ms)",
                             access_latency_sec, access_latency_sec * 1000)

            except Exception as e:
                success = False

                logging.info("Result: FAIL")
                logging.info("Reason: %s", str(e))
            total_latency_sec += access_latency_sec
            results.append({
                "node": node_name,
                "resource": resource_name,
                "success": success,
                "latency_sec": access_latency_sec,
            })

    success_rate = (success_count / total_count) if total_count > 0 else 0.0

    success_latencies = [
        r["latency_sec"] for r in results
        if r["success"] and r["latency_sec"] is not None
    ]

    logging.info("\n" + "#" * 50)
    logging.info("AUTHORIZED ACCESS CHECK SUMMARY")
    logging.info("#" * 50)
    logging.info("Total attempts: %d", total_count)
    logging.info("Successful attempts: %d", success_count)
    logging.info("Failed attempts: %d", total_count - success_count)
    logging.info("Success rate: %.2f%%", success_rate * 100)

    if success_latencies:
        logging.info("Average latency: %.6f sec (%.2f ms)",
                     statistics.mean(success_latencies),
                     statistics.mean(success_latencies) * 1000)
        logging.info("Min latency: %.6f sec (%.2f ms)",
                     min(success_latencies),
                     min(success_latencies) * 1000)
        logging.info("Max latency: %.6f sec (%.2f ms)",
                     max(success_latencies),
                     max(success_latencies) * 1000)

    logging.info("\nDetailed results:")
    for r in results:
        if r["success"]:
            logging.info("  %s -> %s : SUCCESS (%.6f sec, %.2f ms)",
                         r["node"], r["resource"],
                         r["latency_sec"], r["latency_sec"] * 1000)
        else:
            logging.info("  %s -> %s : FAIL", r["node"], r["resource"])

    return results, success_rate, total_latency_sec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="../iotauth/entity/node/example_entities")
    ap.add_argument("--timeout", type=float, default=90.0)

    args = ap.parse_args()
    setup_logging(LOG_FILE)
    workdir = os.path.abspath(args.workdir)

    total_start = time.perf_counter()


    logging.info("\n=== Start process: Node0 delegateAuthority Node2 ResourceC 1*day ===")
    node_cmd = "node user.js configs/net1/node0.config"
    send_command = "delegateAuthority Node2 ResourceC 1*day"
    delegation0C_latency_sec = run_and_measure(
        workdir=workdir,
        node_cmd=node_cmd.split(),
        send_command=send_command,
        timeout_sec=args.timeout,
        pattern=RESPONSE_PATTERNS,
    )
    logging.info("\n=== LATENCY RESULT (process start -> ResourceC (Node0 -> Node2) response) ===")
    logging.info(f"{delegation0C_latency_sec:.6f} seconds")
    logging.info(f"{delegation0C_latency_sec * 1000:.2f} ms\n\n")

    logging.info("\n=== Start process: Node0 delegateAuthority Node2 ResourceD 1*day ===")
    node_cmd = "node user.js configs/net1/node0.config"
    send_command = "delegateAuthority Node2 ResourceD 1*day"
    delegation0D_latency_sec = run_and_measure(
        workdir=workdir,
        node_cmd=node_cmd.split(),
        send_command=send_command,
        timeout_sec=args.timeout,
        pattern=RESPONSE_PATTERNS,
    )
    logging.info("\n=== LATENCY RESULT (process start -> ResourceD (Node0 -> Node2) response) ===")
    logging.info(f"{delegation0D_latency_sec:.6f} seconds")
    logging.info(f"{delegation0D_latency_sec * 1000:.2f} ms\n\n")



    # logging.info("\n=== Start process: User(Alex) revoke Node1 ResourceA ===")
    # node_cmd = "node user.js configs/net1/Alex.config"
    # send_command = "revoke Node1 ResourceA"
    # revoke_latency_sec = run_and_measure(
    #     workdir=workdir,
    #     node_cmd=node_cmd.split(),
    #     send_command=send_command,
    #     timeout_sec=args.timeout,
    #     pattern=RESPONSE_PATTERNS, 
    # )
    # logging.info("\n=== LATENCY RESULT (process start -> revocation (Users -> Node1) response) ===")
    # logging.info(f"{revoke_latency_sec:.6f} seconds")
    # logging.info(f"{revoke_latency_sec * 1000:.2f} ms\n\n")

    total_end = time.perf_counter()
    total_latency_sec = total_end - total_start

    logging.info("\n=== TOTAL END-TO-END LATENCY RESULT (wall-clock time) ===")
    logging.info("From first delegation start to access checking")
    logging.info(f"{total_latency_sec:.6f} seconds")
    logging.info(f"{total_latency_sec * 1000:.2f} ms\n")
    logging.info("Total sum of all delegation: ")
    latency_sum = delegation0C_latency_sec + delegation0D_latency_sec
    logging.info(f"{latency_sum:.6f} seconds")
    logging.info(f"{latency_sum * 1000:.2f} ms\n")


if __name__ == "__main__":
    main()
