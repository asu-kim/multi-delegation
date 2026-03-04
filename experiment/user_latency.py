#!/usr/bin/env python3
import argparse
import os
import re
import subprocess
import sys
import time
import json
import uuid
import redis
import logging

READY_PATTERNS = [
    re.compile(r"\binitializing\.\.\.\b", re.IGNORECASE),
    re.compile(r"\bcurrent parameters\b", re.IGNORECASE),
]

RESPONSE_PATTERNS = [
    re.compile(r"\breceived privilege response!\b", re.IGNORECASE),
    re.compile(r"\bFinished privilege request\b", re.IGNORECASE),
]
LOG_FILE = f"user_log_{int(time.time())}.txt"

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

def publish_to_agent1(redis_url: str, channel: str, event: dict) -> None:
    r = redis.Redis.from_url(redis_url, decode_responses=True)
    r.publish(channel, json.dumps(event))

def run_and_measure(workdir, node_cmd, send_command, timeout_sec, echo_logs, redis_url, channel):
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
            if echo_logs:
                sys.stdout.write(line)
                sys.stdout.flush()
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

                event = {
                    "workflow_id": workflow_id,
                    "event_type": "UserPrivilegeDone",
                    "idempotency_key": f"UserPrivilegeDone:{workflow_id}",
                    "metrics": {"latency_sec": latency},
                    "inputs": {
                        "agent2_default_workdir": "iotauth/entity/node/example_entities",
                        "agent2_default_node_cmd": "node user.js configs/net1/highTrustAgent.config",
                        "agent2_default_target": "net1.resourceA",
                    },
                }
                publish_to_agent1(redis_url, channel, event)

                return latency

        raise RuntimeError("Process ended before response markers were observed.")
    finally:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="iotauth/entity/node/example_entities")
    ap.add_argument("--node-cmd", default="node user.js configs/net1/Alex.config")
    ap.add_argument("--send", default="delegateAuthority MyAgents ResourceA 1*day")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--no-echo", action="store_true")

    ap.add_argument("--redis", default="redis://127.0.0.1:6379/0")
    ap.add_argument("--channel", default="sacmat:agent1")

    args = ap.parse_args()
    setup_logging(LOG_FILE)

    workdir = os.path.abspath(args.workdir)
    node_cmd = args.node_cmd.split()

    latency_sec = run_and_measure(
        workdir=workdir,
        node_cmd=node_cmd,
        send_command=args.send,
        timeout_sec=args.timeout,
        echo_logs=not args.no_echo,
        redis_url=args.redis,
        channel=args.channel,
    )

    logging.info("\n=== LATENCY RESULT (process start -> first response marker) ===")
    logging.info(f"{latency_sec:.6f} seconds")
    logging.info(f"{latency_sec * 1000:.2f} ms")

if __name__ == "__main__":
    main()