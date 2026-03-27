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

ACCESS_RESPONSE_PATTERNS = [
    re.compile(r"\bswitching to IN_COMM\b", re.IGNORECASE),
]
RESPONSE_PATTERNS = [
    re.compile(r"\bdisconnected from auth\b", re.IGNORECASE),
]

LOG_FILE = f"user_node1_server_log_{int(time.time())}.txt"


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="../iotauth/entity/node/example_entities")
    ap.add_argument("--timeout", type=float, default=90.0)

    args = ap.parse_args()
    setup_logging(LOG_FILE)
    workdir = os.path.abspath(args.workdir)

    total_start = time.perf_counter()

    logging.info("\n=== Start process: User(Alex) delegateAuthority Node1 ResourceA 1*day ===")
    node_cmd = "node user.js configs/net1/Alex.config"
    send_command = "delegateAuthority Node1 ResourceA 1*day"
    delegation_latency_sec = run_and_measure(
        workdir=workdir,
        node_cmd=node_cmd.split(),
        send_command=send_command,
        timeout_sec=args.timeout,
        pattern=RESPONSE_PATTERNS, 
    )
    logging.info("\n=== LATENCY RESULT (process start -> delegation (Users -> Node1) response) ===")
    logging.info(f"{delegation_latency_sec:.6f} seconds")
    logging.info(f"{delegation_latency_sec * 1000:.2f} ms\n\n")

    logging.info("\n=== Start process: Node1 access ResourceA 1*day ===")
    node_cmd = "node user.js configs/net1/node1.config"
    send_command = "initComm"
    access_latency = run_and_measure(
        workdir=workdir,
        node_cmd=node_cmd.split(),
        send_command=send_command,
        timeout_sec=args.timeout,
        pattern=ACCESS_RESPONSE_PATTERNS,
    )
    logging.info("\n=== LATENCY RESULT (process start -> access (Node1 -> ResourceA) response) ===")
    logging.info(f"{access_latency:.6f} seconds")
    logging.info(f"{access_latency * 1000:.2f} ms\n\n")

    logging.info("\n=== Start process: User(Alex) revoke Node1 ResourceA ===")
    node_cmd = "node user.js configs/net1/Alex.config"
    send_command = "revoke Node1 ResourceA"
    revoke_latency_sec = run_and_measure(
        workdir=workdir,
        node_cmd=node_cmd.split(),
        send_command=send_command,
        timeout_sec=args.timeout,
        pattern=RESPONSE_PATTERNS,
    )
    logging.info("\n=== LATENCY RESULT (process start -> revocation (Users -> Node1) response) ===")
    logging.info(f"{revoke_latency_sec:.6f} seconds")
    logging.info(f"{revoke_latency_sec * 1000:.2f} ms\n\n")

    total_end = time.perf_counter()
    total_latency_sec = total_end - total_start

    logging.info("\n=== TOTAL END-TO-END LATENCY RESULT (wall-clock time) ===")
    logging.info("From the delegation start to final revocation response")
    logging.info(f"{total_latency_sec:.6f} seconds")
    logging.info(f"{total_latency_sec * 1000:.2f} ms\n")
    logging.info("Total sum of all stagse: delegation1, access, revocation")
    latency_sum =  access_latency + delegation_latency_sec + revoke_latency_sec
    logging.info(f"{latency_sum:.6f} seconds")
    logging.info(f"{latency_sum * 1000:.2f} ms\n")


if __name__ == "__main__":
    main()
