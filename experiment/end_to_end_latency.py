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

READY_PATTERNS = [
    re.compile(r"\binitializing\.\.\.\b", re.IGNORECASE),
    re.compile(r"\bcurrent parameters\b", re.IGNORECASE),
]

RESPONSE_PATTERNS = [
    re.compile(r"\bdisconnected from auth\b", re.IGNORECASE),
]

LOG_FILE = f"end_to_end_log_{int(time.time())}.txt"


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


def run_and_measure(workdir, node_cmd, send_command, timeout_sec):
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

            if matches_any(line, RESPONSE_PATTERNS):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="../iotauth/entity/node/example_entities")
    ap.add_argument("--timeout", type=float, default=90.0)

    args = ap.parse_args()
    setup_logging(LOG_FILE)
    workdir = os.path.abspath(args.workdir)

    total_start = time.perf_counter()

    logging.info("\n=== Start process: User(Alex) delegateAuthority MyAgents ResourceA 1*day ===")
    node_cmd = "node user.js configs/net1/Alex.config"
    send_command = "delegateAuthority MyAgents ResourceA 1*day"
    delegation1_latency_sec = run_and_measure(
        workdir=workdir,
        node_cmd=node_cmd.split(),
        send_command=send_command,
        timeout_sec=args.timeout,
    )
    logging.info("\n=== LATENCY RESULT (process start -> first delegation (Users -> MyAgents) response) ===")
    logging.info(f"{delegation1_latency_sec:.6f} seconds")
    logging.info(f"{delegation1_latency_sec * 1000:.2f} ms\n\n")

    logging.info("\n=== Start process: MyAgents(alexAgent) delegateAuthority ExternalAgents ResourceA 1*day ===")
    node_cmd = "node user.js configs/net1/alexAgent.config"
    send_command = "delegateAuthority ExternalAgents ResourceA 1*day"
    delegation2_latency_sec = run_and_measure(
        workdir=workdir,
        node_cmd=node_cmd.split(),
        send_command=send_command,
        timeout_sec=args.timeout,
    )
    logging.info("\n=== LATENCY RESULT (process start -> second delegation (MyAgents -> ExternalAgents) response) ===")
    logging.info(f"{delegation2_latency_sec:.6f} seconds")
    logging.info(f"{delegation2_latency_sec * 1000:.2f} ms\n\n")

    logging.info("\n=== Start process: ExternalAgents(highTrustAgent) delegateAuthority NodeA ResourceA 1*day ===")
    node_cmd = "node user.js configs/net1/highTrustAgent.config"
    send_command = "delegateAuthority NodeA ResourceA 1*day"
    delegation3_latency_sec = run_and_measure(
        workdir=workdir,
        node_cmd=node_cmd.split(),
        send_command=send_command,
        timeout_sec=args.timeout,
    )
    logging.info("\n=== LATENCY RESULT (process start -> third delegation (ExternalAgents -> NodeA) response) ===")
    logging.info(f"{delegation3_latency_sec:.6f} seconds")
    logging.info(f"{delegation3_latency_sec * 1000:.2f} ms\n\n")

    logging.info("\n=== Start process: NodeA(nodeA) delegateAuthority NodeB ResourceA 1*day ===")
    node_cmd = "node user.js configs/net1/nodeA.config"
    send_command = "delegateAuthority NodeB ResourceA 1*day"
    delegation4_latency_sec = run_and_measure(
        workdir=workdir,
        node_cmd=node_cmd.split(),
        send_command=send_command,
        timeout_sec=args.timeout,
    )
    logging.info("\n=== LATENCY RESULT (process start -> forth delegation (NodeA -> NodeB) response) ===")
    logging.info(f"{delegation4_latency_sec:.6f} seconds")
    logging.info(f"{delegation4_latency_sec * 1000:.2f} ms\n\n")

    logging.info("\n=== Start process: User(Alex) revoke MyAgents ResourceA ===")
    node_cmd = "node user.js configs/net1/Alex.config"
    send_command = "revoke MyAgents ResourceA"
    revoke_latency_sec = run_and_measure(
        workdir=workdir,
        node_cmd=node_cmd.split(),
        send_command=send_command,
        timeout_sec=args.timeout,
    )
    logging.info("\n=== LATENCY RESULT (process start -> revocation (Users -> MyAgents) response) ===")
    logging.info(f"{revoke_latency_sec:.6f} seconds")
    logging.info(f"{revoke_latency_sec * 1000:.2f} ms\n\n")

    total_end = time.perf_counter()
    total_latency_sec = total_end - total_start

    logging.info("\n=== TOTAL END-TO-END LATENCY RESULT (wall-clock time) ===")
    logging.info("From first delegation start to final revocation response")
    logging.info(f"{total_latency_sec:.6f} seconds")
    logging.info(f"{total_latency_sec * 1000:.2f} ms\n")
    logging.info("Total sum of all stagse: delegation1, delegation2, delegation3, delegation4, revocation")
    latency_sum = delegation4_latency_sec + delegation3_latency_sec + delegation2_latency_sec + delegation1_latency_sec + revoke_latency_sec
    logging.info(f"{latency_sum:.6f} seconds")
    logging.info(f"{latency_sum * 1000:.2f} ms\n")


if __name__ == "__main__":
    main()
