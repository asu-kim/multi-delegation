#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, Optional

import redis
import requests
import logging


WORKDIR = "../iotauth/entity/node/example_entities"
NODE_CMD = "node user.js configs/net1/alexAgent.config"
DELEGATE_CMD = "delegateAuthority ExternalAgents ResourceA 1*day"

READY_REGEX = r"(current parameters|initializing\.\.\.)"
DONE_REGEX = r"(Finished privilege request|received privilege response!)"

ACTION_RUN_DELEGATION = "run_delegation"
ACTION_PUBLISH_SIGNAL = "publish_signal"
ALLOWED_ACTIONS = {ACTION_RUN_DELEGATION, ACTION_PUBLISH_SIGNAL}
LOG_FILE = f"personaAgent_log_{int(time.time())}.txt"

def setup_logging(log_file: str):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ],
    )


# =============================
# LLM call (must return only {"action": "..."} )
# =============================
def build_system_prompt() -> str:
    return (
        "Return ONLY valid JSON with exactly one key: action.\n"
        "Allowed outputs:\n"
        "  {\"action\":\"run_delegation\"}\n"
        "  {\"action\":\"publish_signal\"}\n\n"
        "Rules:\n"
        "- If delegation_done is false, you MUST output run_delegation.\n"
        "- If delegation_done is true, you should normally output publish_signal.\n"
        "- No extra keys. No explanations. JSON only.\n"
    )


def ask_llm_action(
    ollama_chat_url: str,
    model: str,
    system_prompt: str,
    state: Dict[str, Any],
    timeout_sec: float,
) -> str:
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(state, ensure_ascii=False)},
        ],
    }
    logging.info("========== LLM INOUT ==========")
    logging.info(json.dumps(payload, indent=2))
    logging.info("================================\n")

    r = requests.post(ollama_chat_url, json=payload, timeout=timeout_sec)
    r.raise_for_status()
    content = r.json()["message"]["content"]
    
    logging.info("========== LLM OUTPUT ==========")
    logging.info(content)
    logging.info(json.dumps(r.json(), indent=2))
    logging.info("================================\n")

    obj = json.loads(content)

    if not isinstance(obj, dict) or "action" not in obj or len(obj) != 1:
        raise ValueError("LLM response must be JSON with exactly one key: action")

    action = obj["action"]
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"LLM returned invalid action: {action}")

    return action


# =============================
# Delegation executor
# =============================
def run_delegation_from_t_recv(
    t_recv_perf: float,
    echo_logs: bool,
    timeout_sec: float,
) -> float:
    """
    Measure latency from t_recv_perf (signal receipt) -> first DONE_REGEX match.
    Runs NODE_CMD in WORKDIR and sends DELEGATE_CMD once READY_REGEX is seen.
    """
    workdir_abs = os.path.abspath(WORKDIR)
    ready_pat = re.compile(READY_REGEX, re.IGNORECASE)
    done_pat = re.compile(DONE_REGEX, re.IGNORECASE)

    proc = subprocess.Popen(
        NODE_CMD.split(),
        cwd=workdir_abs,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    sent = False
    deadline = t_recv_perf + timeout_sec

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            now = time.perf_counter()

            if echo_logs:
                sys.stdout.write(line)
                sys.stdout.flush()

            if now > deadline:
                raise TimeoutError(f"Timeout ({timeout_sec}s) waiting for delegation completion marker.")

            if (not sent) and ready_pat.search(line):
                assert proc.stdin is not None
                proc.stdin.write(DELEGATE_CMD + "\n")
                proc.stdin.flush()
                sent = True

            if done_pat.search(line):
                return now - t_recv_perf

        raise RuntimeError("Node process ended before completion markers were observed.")
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


# =============================
# Publish signal executor
# =============================
def publish_signal(
    r: redis.Redis,
    out_channel: str,
    workflow_id: str,
    delivery_gap_ms: Optional[int],
    delegation_latency_sec: float,
    plan_trace: list[str],
) -> None:
    event = {
        "workflow_id": workflow_id,
        "event_type": "Agent1DelegationDone",
        "idempotency_key": f"Agent1DelegationDone:{workflow_id}",
        "t_agent1_done_epoch_ms": int(time.time() * 1000),
        "metrics": {
            "agent1_latency_sec_from_recv": delegation_latency_sec,
            "signal_delivery_gap_ms": delivery_gap_ms,
        },
        "agent1": {
            "workdir": os.path.abspath(WORKDIR),
            "cmd": NODE_CMD,
            "delegate_cmd": DELEGATE_CMD,
        },
        "llm_actions": plan_trace,
    }
    r.publish(out_channel, json.dumps(event))


# =============================
# Main: subscribe -> LLM -> run_delegation -> LLM -> publish
# =============================
def main():
    ap = argparse.ArgumentParser(
        description="Agent1: on user signal, LLM chooses between two actions; code enforces no publish before delegation."
    )
    ap.add_argument("--redis", default="redis://127.0.0.1:6379/0")
    ap.add_argument("--in-channel", default="sacmat:agent1")       # user -> agent1
    ap.add_argument("--out-channel", default="sacmat:workflow")    # agent1 -> next
    ap.add_argument("--no-echo", action="store_true")
    ap.add_argument("--timeout", type=float, default=180.0)

    ap.add_argument("--ollama-chat-url", default="http://127.0.0.1:11434/api/chat")
    ap.add_argument("--ollama-model", default="gpt-oss:20b")
    ap.add_argument("--ollama-timeout", type=float, default=60.0)

    args = ap.parse_args()
    setup_logging(LOG_FILE)

    r = redis.Redis.from_url(args.redis, decode_responses=True)
    pubsub = r.pubsub()
    pubsub.subscribe(args.in_channel)

    system_prompt = build_system_prompt()

    seen = set()
    logging.info(f"[agent1] subscribed to {args.in_channel}. waiting for UserPrivilegeDone...")

    for msg in pubsub.listen():
        if msg.get("type") != "message":
            continue

        t_recv_perf = time.perf_counter()
        t_recv_epoch_ms = int(time.time() * 1000)

        try:
            event_in = json.loads(msg["data"])
            logging.info("========== USER SIGNAL  =======")
            logging.info(json.dumps(event_in, indent=2, ensure_ascii=False))
            logging.info("=======================================\n")
        except Exception:
            continue

        if event_in.get("event_type") != "UserPrivilegeDone":
            continue

        wf = event_in.get("workflow_id", "unknown")
        idem = event_in.get("idempotency_key", f"UserPrivilegeDone:{wf}")
        if idem in seen:
            logging.info(f"[agent1] duplicate ignored: {idem}")
            continue
        seen.add(idem)

        # delivery gap (optional)
        delivery_gap_ms = None
        t_user_done_epoch_ms = event_in.get("t_user_done_epoch_ms")
        if isinstance(t_user_done_epoch_ms, int):
            delivery_gap_ms = t_recv_epoch_ms - t_user_done_epoch_ms

        # -------------------------
        # Decision 1 (must be run_delegation if not done)
        # -------------------------
        plan_trace = []
        state1 = {
            "workflow_id": wf,
            "delegation_done": False,
            "allowed_actions": [ACTION_RUN_DELEGATION, ACTION_PUBLISH_SIGNAL],
            "rule": "DO NOT publish_signal before delegation_done is true.",
            "fixed_program": {
                "workdir": WORKDIR,
                "cmd": NODE_CMD,
                "delegate_cmd": DELEGATE_CMD,
                "ready_regex": READY_REGEX,
                "done_regex": DONE_REGEX,
            },
            "trigger_event": event_in,
        }

        action1 = ask_llm_action(
            ollama_chat_url=args.ollama_chat_url,
            model=args.ollama_model,
            system_prompt=system_prompt,
            state=state1,
            timeout_sec=args.ollama_timeout,
        )
        logging.info(f"LLM action1 --------------- {action1}")
        plan_trace.append(action1)

        # Hard-enforce: cannot publish before delegation
        if action1 != ACTION_RUN_DELEGATION:
            logging.info("LLM did not return propoer output: ")
            logging.info(action1)
            plan_trace.append("enforced_run_delegation")
        delegation_latency = run_delegation_from_t_recv(
            t_recv_perf=t_recv_perf,
            echo_logs= not args.no_echo,
            timeout_sec=args.timeout,
        )

        # -------------------------
        # Decision 2 (now delegation_done == True)
        # -------------------------
        state2 = {
            "workflow_id": wf,
            "delegation_done": True,
            "delegation_result": {
                "agent1_latency_sec_from_recv": delegation_latency,
                "signal_delivery_gap_ms": delivery_gap_ms,
            },
            "allowed_actions": [ACTION_RUN_DELEGATION, ACTION_PUBLISH_SIGNAL],
            "rule": "Delegation is done; decide whether to publish_signal now.",
            "out_channel": args.out_channel,
        }

        action2 = ask_llm_action(
            ollama_chat_url=args.ollama_chat_url,
            model=args.ollama_model,
            system_prompt=system_prompt,
            state=state2,
            timeout_sec=args.ollama_timeout,
        )
        logging.info(f"LLM action2 --------------- {action2}")
        plan_trace.append(action2)

        if action2 == ACTION_PUBLISH_SIGNAL:
            publish_signal(
                r=r,
                out_channel=args.out_channel,
                workflow_id=wf,
                delivery_gap_ms=delivery_gap_ms,
                delegation_latency_sec=delegation_latency,
                plan_trace=plan_trace,
            )
            published = True
        else:
            # If LLM insists on run_delegation again, we will NOT loop indefinitely.
            # We finish without publishing (by design).
            logging.info("LLM did not return propoer output: ")
            logging.info(action2)
            published = False

        logging.info("\n=== AGENT1 SUMMARY ===")
        logging.info(f"workflow_id: {wf}")
        if delivery_gap_ms is not None:
            logging.info(f"signal delivery gap (user_done -> agent1_recv): {delivery_gap_ms} ms")
        logging.info(f"agent1 delegation latency (agent1_recv -> done_marker): {delegation_latency:.6f} s ({delegation_latency*1000:.2f} ms)")
        logging.info(f"llm actions: {plan_trace}")
        logging.info(f"published_signal: {published} (channel={args.out_channel})")

        return


if __name__ == "__main__":
    main()