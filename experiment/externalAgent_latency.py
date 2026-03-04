#!/usr/bin/env python3
import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, Optional

import redis
import requests



WORKDIR = "../iotauth/entity/node/example_entities"
NODE_CMD = "node user.js configs/net1/highTrustAgent.config"
INITCOMM_CMD = "initComm net1.resourceA"

READY_REGEX = r"(current parameters|initializing\.\.\.)"

IN_COMM_REGEX = r"switching to\s+IN_COMM"

ACTION_RUN_INITCOMM = "run_initcomm"
ACTION_PUBLISH_SIGNAL = "publish_signal"
ALLOWED_ACTIONS = {ACTION_RUN_INITCOMM, ACTION_PUBLISH_SIGNAL}

LOG_FILE = f"externalAgent_log_{int(time.time())}.txt"


def setup_logging(log_file: str):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )


# =============================
# LLM (Ollama) – must return ONLY {"action": "..."}
# =============================
def build_system_prompt() -> str:
    return (
        "Return ONLY valid JSON with exactly one key: action.\n"
        "Allowed outputs:\n"
        "  {\"action\":\"run_initcomm\"}\n"
        "  {\"action\":\"publish_signal\"}\n\n"
        "Rules:\n"
        "- If in_comm is false, you MUST output run_initcomm.\n"
        "- If in_comm is true, you should normally output publish_signal.\n"
        "- No extra keys. No explanations. JSON only.\n"
    )


def ask_llm_action(
    ollama_chat_url: str,
    model: str,
    system_prompt: str,
    state: Dict[str, Any],
    timeout_sec: float,
    print_llm_raw: bool,
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

    if print_llm_raw:
        logging.info("========== LLM OUTPUT (RAW) ===========")
        logging.info(json.dumps(r.json(), indent=2))
        logging.info("======================================")

    obj = json.loads(content)

    if not isinstance(obj, dict) or "action" not in obj or len(obj) != 1:
        raise ValueError("LLM response must be JSON with exactly one key: action")

    action = obj["action"]
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"LLM returned invalid action: {action}")

    return action


# =============================
# Executor: run node + initComm + wait IN_COMM
# =============================
def run_initcomm_from_t_recv(
    t_recv_perf: float,
    timeout_sec: float,
) -> float:
    """
    Measures latency from t_recv_perf (signal receipt) -> first IN_COMM_REGEX match.
    Runs NODE_CMD in WORKDIR and sends INITCOMM_CMD after READY_REGEX is seen.
    """
    workdir_abs = os.path.abspath(WORKDIR)
    ready_pat = re.compile(READY_REGEX, re.IGNORECASE)
    in_comm_pat = re.compile(IN_COMM_REGEX, re.IGNORECASE)

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

            # sys.stdout.write(line)
            # sys.stdout.flush()

            # Always log node output to file (optional: comment out if too noisy)
            logging.info("[node] %s", line.rstrip("\n"))

            if now > deadline:
                raise TimeoutError(f"Timeout ({timeout_sec}s) waiting for IN_COMM marker.")

            if (not sent) and ready_pat.search(line):
                assert proc.stdin is not None
                proc.stdin.write(INITCOMM_CMD + "\n")
                proc.stdin.flush()
                sent = True
                logging.info("Sent initComm: %s", INITCOMM_CMD)

            if in_comm_pat.search(line):
                return now - t_recv_perf

        raise RuntimeError("Node process ended before IN_COMM marker was observed.")
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
# Publish signal
# =============================
def publish_signal(
    r: redis.Redis,
    out_channel: str,
    workflow_id: str,
    in_comm_latency_sec: float,
    llm_actions: list[str],
    delivery_gap_ms: Optional[int],
):
    event = {
        "workflow_id": workflow_id,
        "event_type": "Agent2InComm",
        "idempotency_key": f"Agent2InComm:{workflow_id}",
        "t_agent2_done_epoch_ms": int(time.time() * 1000),
        "metrics": {
            "agent2_latency_sec_from_recv": in_comm_latency_sec,
            "signal_delivery_gap_ms": delivery_gap_ms,
        },
        "agent2": {
            "workdir": os.path.abspath(WORKDIR),
            "cmd": NODE_CMD,
            "initcomm_cmd": INITCOMM_CMD,
        },
        "llm_actions": llm_actions,
    }
    r.publish(out_channel, json.dumps(event))


# =============================
# Main: subscribe Agent1 signal -> LLM -> initComm -> LLM -> publish
# =============================
def main():
    ap = argparse.ArgumentParser(
        description="Agent2: receive Agent1 signal, LLM decides next action (run initComm or publish). Enforces no publish before IN_COMM."
    )
    ap.add_argument("--redis", default="redis://127.0.0.1:6379/0")

    ap.add_argument("--in-channel", default="sacmat:workflow")

    ap.add_argument("--out-channel", default="sacmat:workflow")

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
    logging.info("Agent2 started. log_file=%s", LOG_FILE)
    logging.info("Subscribed to %s; waiting for event_type=Agent1DelegationDone", args.in_channel, )

    for msg in pubsub.listen():
        if msg.get("type") != "message":
            continue


        # Start measurement at receipt time
        t_recv_perf = time.perf_counter()
        t_recv_epoch_ms = int(time.time() * 1000)

        try:
            event_in = json.loads(msg["data"])
            logging.info("========== USER SIGNAL  =======")
            logging.info(json.dumps(event_in, indent=2, ensure_ascii=False))
            logging.info("=======================================\n")
        except Exception:
            continue


        if event_in.get("event_type") != "Agent1DelegationDone":
            continue

        wf = event_in.get("workflow_id", "unknown")
        idem = event_in.get("idempotency_key", f"Agent1DelegationDone:{wf}")
        if idem in seen:
            logging.info("Duplicate ignored: %s", idem)
            continue
        seen.add(idem)

        # Optional delivery gap (Agent1 -> Agent2)
        delivery_gap_ms = None
        t_agent1_done_epoch_ms = event_in.get("t_agent1_done_epoch_ms")
        if isinstance(t_agent1_done_epoch_ms, int):
            delivery_gap_ms = t_recv_epoch_ms - t_agent1_done_epoch_ms

        llm_actions = []

        # ---- Decision 1 (must be run_initcomm)
        state1 = {
            "workflow_id": wf,
            "in_comm": False,
            "allowed_actions": [ACTION_RUN_INITCOMM, ACTION_PUBLISH_SIGNAL],
            "rule": "DO NOT publish_signal before in_comm is true.",
            "fixed_program": {
                "workdir": WORKDIR,
                "cmd": NODE_CMD,
                "initcomm_cmd": INITCOMM_CMD,
                "ready_regex": READY_REGEX,
                "in_comm_regex": IN_COMM_REGEX,
            },
            "trigger_event": event_in,
        }

        action1 = ask_llm_action(
            ollama_chat_url=args.ollama_chat_url,
            model=args.ollama_model,
            system_prompt=system_prompt,
            state=state1,
            timeout_sec=args.ollama_timeout,
            print_llm_raw=True,
        )
        logging.info(f"LLM action1 --------------- {action1}")
        llm_actions.append(action1)

        # Hard-enforce: cannot publish before IN_COMM
        if action1 != ACTION_RUN_INITCOMM:
            llm_actions.append("enforced_run_initcomm")

        in_comm_latency = run_initcomm_from_t_recv(
            t_recv_perf=t_recv_perf,
            timeout_sec=args.timeout,
        )

        # ---- Decision 2 (now in_comm == True)
        state2 = {
            "workflow_id": wf,
            "in_comm": True,
            "in_comm_result": {
                "agent2_latency_sec_from_recv": in_comm_latency,
                "delivery_gap_ms": delivery_gap_ms,
            },
            "allowed_actions": [ACTION_RUN_INITCOMM, ACTION_PUBLISH_SIGNAL],
            "rule": "IN_COMM reached; decide whether to publish_signal now.",
            "out_channel": args.out_channel,
        }

        # action2 = ask_llm_action(
        #     ollama_chat_url=args.ollama_chat_url,
        #     model=args.ollama_model,
        #     system_prompt=system_prompt,
        #     state=state2,
        #     timeout_sec=args.ollama_timeout,
        #     print_llm_raw=args.print_llm,
        # )
        # llm_actions.append(action2)

        # if action2 == ACTION_PUBLISH_SIGNAL:
        #     publish_signal(
        #         r=r,
        #         out_channel=args.out_channel,
        #         workflow_id=wf,
        #         in_comm_latency_sec=in_comm_latency,
        #         llm_actions=llm_actions,
        #         delivery_gap_ms=delivery_gap_ms,
        #     )
        #     published = True
        # else:
        #     published = False

        logging.info("=== AGENT2 SUMMARY ===")
        logging.info("workflow_id: %s", wf)
        if delivery_gap_ms is not None:
            logging.info("signal delivery gap (agent1_done -> agent2_recv): %s ms", delivery_gap_ms)
        logging.info("agent2 latency (recv -> IN_COMM): %.6f s (%.2f ms)", in_comm_latency, in_comm_latency * 1000)
        logging.info("llm_actions: %s", llm_actions)
        # logging.info("published_signal: %s (channel=%s)", published, args.out_channel)

        return


if __name__ == "__main__":
    main()