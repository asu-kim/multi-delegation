#!/usr/bin/env python3
import json
import networkx as nx
import os, sys
import re
import time
import random
import logging
import subprocess
from collections import defaultdict, deque

import networkx as nx
import matplotlib.pyplot as plt

LOG_FILE = f"dag_log_{int(time.time())}.txt"

READY_PATTERNS = [
    re.compile(r"\binitializing\.\.\.\b", re.IGNORECASE),
    re.compile(r"\bcurrent parameters\b", re.IGNORECASE),
]

DELEGATION_SUCCESS_PATTERNS = [
    re.compile(r"\bdisconnected from auth\b", re.IGNORECASE),
]

ACCESS_SUCCESS_PATTERNS = [
    re.compile(r"\bswitching to IN_COMM\b", re.IGNORECASE),
]

FAIL_PATTERNS = [
    re.compile(r"\berror\b", re.IGNORECASE),
    re.compile(r"\bfailed\b", re.IGNORECASE),
    re.compile(r"\bexception\b", re.IGNORECASE),
    re.compile(r"\bdenied\b", re.IGNORECASE),
]

REVOKE_SUCCESS_PATTERNS = [
    re.compile(r"\bdisconnected from auth\b", re.IGNORECASE),
]


def dag_to_graph_json(
    G: nx.DiGraph,
    source,
    sink,
    out_path="generated.graph",
    validity="1*day",
    abs_validity="1*day",
    rel_validity="1*hour"
):
    # 1) mapping node names
    mapping = {}
    middle_idx = 1

    for node in nx.topological_sort(G):
        if node == source:
            mapping[node] = ("Users", "net1.user")
        elif node == sink:
            mapping[node] = ("Resource", "net1.resource")
        else:
            mapping[node] = (f"Node{middle_idx}", f"net1.node{middle_idx}")
            middle_idx += 1

    # 2) base graph skeleton
    graph = {
        "authList": [
            {
                "id": 101,
                "entityHost": "localhost",
                "authHost": "localhost",
                "tcpPort": 21900,
                "udpPort": 21902,
                "authPort": 21901,
                "callbackPort": 21903,
                "dbProtectionMethod": 1,
                "backupEnabled": False,
                "contextualCallbackEnabled": True
            }
        ],
        "authTrusts": [],
        "assignments": {},
        "entityList": [],
        "filesharingLists": [],
        "privilegeList": []
    }

    # 3) assignments + entityList
    for old_node in nx.topological_sort(G):
        group, name = mapping[old_node]
        graph["assignments"][name] = 101

        entity = {
            "group": group,
            "name": name,
            "distProtocol": "TCP",
            "usePermanentDistKey": True,
            "distKeyValidityPeriod": "30*day",
            "maxSessionKeysPerRequest": 5,
            "netName": "net1",
            "credentialPrefix": name.replace("net1.", "Net1."),
            "distributionCryptoSpec": {
                "cipher": "AES-128-CBC",
                "mac": "SHA256"
            },
            "sessionCryptoSpec": {
                "cipher": "AES-128-CBC",
                "mac": "SHA256"
            },
            "backupToAuthIds": []
        }

        if old_node == source:
            entity["distKeyValidityPeriod"] = "365*day"
        if old_node == sink:
            entity["port"] = 21100
            entity["host"] = "localhost"
            entity["usePermanentDistKey"] = False
            entity["maxSessionKeysPerRequest"] = 30
            entity["distKeyValidityPeriod"] = "365*day"

        graph["entityList"].append(entity)

    # 4) privilegeList
    #    Convert edge A->B to grant(A, B, Resource)
    #    if B == sink, do not create privilege
    resource_group = mapping[sink][0]

    for u, v in G.edges():
        if v == sink:
            continue

        privileged_group = mapping[u][0]
        subject_group = mapping[v][0]

        privilege = {
            "privilegeType": "DelegationGrant",
            "privilegedGroup": privileged_group,
            "subject": subject_group,
            "object": resource_group,
            "validity": validity,
            "info": {
                "cryptoSpec": "AES-128-CBC:SHA256",
                "absValidity": abs_validity,
                "relValidity": rel_validity
            }
        }
        graph["privilegeList"].append(privilege)

        privilege = {
            "privilegeType": "DelegationRevoke",
            "privilegedGroup": privileged_group,
            "subject": subject_group,
            "object": resource_group,
            "validity": validity,
            "info": {
                "cryptoSpec": "AES-128-CBC:SHA256",
                "absValidity": abs_validity,
                "relValidity": rel_validity
            }
        }
        graph["privilegeList"].append(privilege)

    with open(out_path, "w") as f:
        json.dump(graph, f, indent=4)

    return graph


def generate_single_source_single_sink_dag(
    num_layers=5,
    nodes_per_layer=(2, 4),
    edge_prob=0.4,
    seed=None
):
    random.seed(seed)

    G = nx.DiGraph()

    source = "s"
    sink = "t"

    layers = [[source]]

    # middle layers
    for i in range(num_layers - 2):
        k = random.randint(nodes_per_layer[0], nodes_per_layer[1])
        layer = [f"L{i}_N{j}" for j in range(k)]
        layers.append(layer)

    layers.append([sink])

    # add nodes
    for layer in layers:
        G.add_nodes_from(layer)

    # connect adjacent layers first so every node is reachable
    for i in range(len(layers) - 1):
        curr_layer = layers[i]
        next_layer = layers[i + 1]

        # each node in next_layer gets at least one incoming edge
        for v in next_layer:
            u = random.choice(curr_layer)
            G.add_edge(u, v)

        # each node in curr_layer gets at least one outgoing edge
        for u in curr_layer:
            v = random.choice(next_layer)
            G.add_edge(u, v)

        # add extra random edges
        for u in curr_layer:
            for v in next_layer:
                if not G.has_edge(u, v) and random.random() < edge_prob:
                    G.add_edge(u, v)

    return G, source, sink, layers


def show_dag(G, layers):
    pos = {}
    for i, layer in enumerate(layers):
        for j, node in enumerate(layer):
            pos[node] = (i, -j)

    plt.figure(figsize=(10, 6))
    nx.draw(
        G, pos,
        with_labels=True,
        node_size=1800,
        arrows=True,
        node_color='skyblue'
    )
    plt.title("Single-source single-sink DAG")
    plt.show()






# 2) DAG -> execution model
def build_name_mapping(G, source, sink):
    """
    DAG node -> execution name
    s -> User
    t -> ResourceA
    others -> Node1, Node2, ...
    """
    mapping = {}
    idx = 1

    for node in nx.topological_sort(G):
        if node == source:
            mapping[node] = "User"
        elif node == sink:
            mapping[node] = "ResourceA"
        else:
            mapping[node] = f"Node{idx}"
            idx += 1

    return mapping


def build_delegation_edges(G, source, sink, mapping):
    """
    Convert DAG edges into executable delegation edges.
    We skip edges whose target is sink, because sink is the object ResourceA itself.
    Return list of tuples: (grantor, grantee, object_name)
    """
    delegations = []

    for u, v in G.edges():
        if v == sink:
            continue
        grantor = mapping[u]
        grantee = mapping[v]
        delegations.append((grantor, grantee, mapping[sink]))

    return delegations


def compute_delegation_order(delegations):
    """
    delegations: list of (grantor, grantee, object_name)

    A delegation A->B can be executed only after A itself already has authority,
    except for User which is the root authority holder.

    So dependency is:
      if X delegates to Y, then any delegation Y->Z depends on X->Y having happened.

    We build a dependency graph over delegation edges and topologically sort it.
    """
    edge_ids = {}
    for i, (grantor, grantee, obj) in enumerate(delegations):
        edge_ids[(grantor, grantee, obj)] = i

    indeg = {i: 0 for i in range(len(delegations))}
    adj = defaultdict(list)

    incoming_to_node = defaultdict(list)
    outgoing_from_node = defaultdict(list)

    for i, (grantor, grantee, obj) in enumerate(delegations):
        incoming_to_node[grantee].append(i)
        outgoing_from_node[grantor].append(i)

    # If A->B and B->C both exist, then B->C depends on A->B.
    for i, (grantor, grantee, obj) in enumerate(delegations):
        for j in outgoing_from_node.get(grantee, []):
            adj[i].append(j)
            indeg[j] += 1

    q = deque([i for i in range(len(delegations)) if indeg[i] == 0])
    order = []

    while q:
        i = q.popleft()
        order.append(delegations[i])

        for nxt in adj[i]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                q.append(nxt)

    if len(order) != len(delegations):
        raise ValueError("Delegation dependency graph has a cycle or unresolved dependency.")

    return order


def get_access_check_nodes(delegations):
    """
    Return all grantees that received delegated authority.
    """
    nodes = []
    seen = set()
    for _, grantee, _ in delegations:
        if grantee not in seen:
            seen.add(grantee)
            nodes.append(grantee)
    return nodes

def get_all_executable_nodes(G, source, sink, mapping):
    """
    Return all executable nodes except source(User) and sink(ResourceA).
    """
    result = []
    for node in nx.topological_sort(G):
        if node == source or node == sink:
            continue
        result.append(mapping[node])
    return result

def actor_to_config(actor):
    """
    User  -> user.config
    Node1 -> node1.config
    """
    if actor == "User":
        return "user.config"
    if actor.startswith("Node"):
        return f"{actor.lower()}.config"
    raise ValueError(f"Unknown actor for config mapping: {actor}")


def matches_any(line, patterns):
    return any(p.search(line) for p in patterns)


def run_node_command(workdir, config_name, command, success_patterns, timeout=90):
    """
    Launch:
      node user.js configs/net1/<config_name>
    Wait until ready pattern appears, then send command.
    """
    node_cmd = ["node", "user.js", f"configs/net1/{config_name}"]
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

    start = time.perf_counter()
    sent = False
    lines = []

    try:
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            lines.append(line)
            logging.info("[%-12s] %s", config_name, line)

            elapsed = time.perf_counter() - start
            if elapsed > timeout:
                raise TimeoutError(f"Timeout while running {config_name}: {command}")

            if not sent and matches_any(line, READY_PATTERNS):
                proc.stdin.write(command + "\n")
                proc.stdin.flush()
                sent = True
                logging.info("[%-12s] >>> %s", config_name, command)

            if matches_any(line, FAIL_PATTERNS):
                return False, elapsed, line, lines

            if matches_any(line, success_patterns):
                return True, elapsed, line, lines

        return False, time.perf_counter() - start, "Process ended before success pattern appeared", lines

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


def execute_delegation_plan(
    G,
    source,
    sink,
    workdir,
    validity="1*day",
    timeout=90,
):
    mapping = build_name_mapping(G, source, sink)
    raw_delegations = build_delegation_edges(G, source, sink, mapping)
    ordered_delegations = compute_delegation_order(raw_delegations)
    access_nodes = get_access_check_nodes(raw_delegations)

    logging.info("\n=== Node Mapping ===")
    for old, new in mapping.items():
        logging.info(f"{old:10s} -> {new}")

    logging.info("\n=== Delegation Order ===")
    for i, (grantor, grantee, obj) in enumerate(ordered_delegations, start=1):
        logging.info(f"{i:2d}. {grantor} -> {grantee}  (object={obj})")

    delegation_results = []
    access_results = []

    logging.info("\n=== Running Delegations ===")
    for grantor, grantee, obj in ordered_delegations:
        config_name = actor_to_config(grantor)
        command = f"delegateAuthority {grantee} {obj} {validity}"

        ok, latency, matched, _ = run_node_command(
            workdir=workdir,
            config_name=config_name,
            command=command,
            success_patterns=DELEGATION_SUCCESS_PATTERNS,
            timeout=timeout,
        )

        delegation_results.append({
            "grantor": grantor,
            "grantee": grantee,
            "object": obj,
            "success": ok,
            "latency_ms": latency * 1000,
            "matched": matched,
        })

        status = "SUCCESS" if ok else "FAIL"
        logging.info(f"{status:7s} | {grantor:8s} -> {grantee:8s} | {latency*1000:9.2f} ms | {matched}")

        if not ok:
            logging.info("\nDelegation failed. Stop execution.")
            return {
                "mapping": mapping,
                "ordered_delegations": ordered_delegations,
                "delegation_results": delegation_results,
                "access_results": access_results,
            }

    logging.info("\n=== Running Access Checks ===")
    for node in access_nodes:
        config_name = actor_to_config(node)
        command = "initComm"

        ok, latency, matched, _ = run_node_command(
            workdir=workdir,
            config_name=config_name,
            command=command,
            success_patterns=ACCESS_SUCCESS_PATTERNS,
            timeout=timeout,
        )

        access_results.append({
            "actor": node,
            "success": ok,
            "latency_ms": latency * 1000,
            "matched": matched,
        })

        status = "SUCCESS" if ok else "FAIL"
        logging.info(f"{status:7s} | {node:8s} -> ResourceA | {latency*1000:9.2f} ms | {matched}")

    return {
        "mapping": mapping,
        "ordered_delegations": ordered_delegations,
        "delegation_results": delegation_results,
        "access_results": access_results,
    }

def setup_logging(log_file: str):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ],
    )

def start_auth_server(auth_dir, properties_path):
    cmd = [
        "java",
        "-jar",
        "target/auth-server-jar-with-dependencies.jar",
        "-p",
        properties_path
    ]

    logging.info("\n=== Starting Auth Server ===")
    logging.info("Command: %s", " ".join(cmd))
    logging.info("Working dir: %s", auth_dir)

    proc = subprocess.Popen(
        cmd,
        cwd=auth_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    ready_patterns = [
        re.compile(r"STARTING", re.IGNORECASE),
        re.compile(r"Started Server", re.IGNORECASE),
    ]

    start = time.perf_counter()

    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("Auth server terminated unexpectedly")

        line = line.strip()
        logging.info("[auth] %s", line)

        if any(p.search(line) for p in ready_patterns):
            logging.info("Auth server is ready.")
            break

        if time.perf_counter() - start > 30:
            raise TimeoutError("Auth server startup timeout")

    return proc


def run_generate_all(graph_path, iotauth_dir, timeout=60):
    """
    Run:
      ./generateAll -g <graph_path>

    inside iotauth/examples directory.
    """

    cmd = ["./generateAll.sh", "-g", graph_path]

    logging.info(f"\n=== Running generateAll ===")
    logging.info(f"Command: {' '.join(cmd)}")
    logging.info(f"Working dir: {iotauth_dir}")

    start = time.perf_counter()

    proc = subprocess.Popen(
        cmd,
        cwd=iotauth_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    lines = []

    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            logging.info(f"[generateAll] {line}")

            if time.perf_counter() - start > timeout:
                raise TimeoutError("generateAll timeout")

        ret = proc.wait()

        if ret != 0:
            raise RuntimeError(f"generateAll failed with exit code {ret}")

        logging.info(f"generateAll finished in {(time.perf_counter()-start)*1000:.2f} ms")

    finally:
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass

    return True


def choose_random_revocation_target(G, source, sink, mapping, seed=None):
    """
    Revoke target is chosen among delegated executable nodes only:
    - exclude source(User)
    - exclude sink(ResourceA)
    """
    rng = random.Random(seed)

    candidates = []
    for node in G.nodes():
        if node == source or node == sink:
            continue
        candidates.append(node)

    if not candidates:
        raise ValueError("No revocable intermediate node exists.")

    target = rng.choice(candidates)
    return target


def get_revocation_impacted_nodes(G, revoke_target):
    """
    Return: revoke_target + all descendants in DAG
    """
    descendants = nx.descendants(G, revoke_target)
    impacted = {revoke_target, *descendants}
    return impacted

def get_revokers_for_target(G, revoke_target, mapping):
    """
    All immediate predecessors of revoke_target will revoke it.
    Returns list of execution names, e.g. ["User"] or ["Node1", "Node2"]
    """
    parents = list(G.predecessors(revoke_target))
    return [mapping[p] for p in parents]

def run_revoke(grantor, grantee, obj, workdir, timeout=90):
    config_name = actor_to_config(grantor)
    command = f"revoke {grantee} {obj}"

    ok, latency, matched, _ = run_node_command(
        workdir=workdir,
        config_name=config_name,
        command=command,
        success_patterns=REVOKE_SUCCESS_PATTERNS,
        timeout=timeout,
    )

    return {
        "grantor": grantor,
        "grantee": grantee,
        "object": obj,
        "success": ok,
        "latency_ms": latency * 1000,
        "matched": matched,
        "command": command,
    }

def run_access_checks_for_all_nodes(nodes, workdir, timeout=90):
    results = []

    for node in nodes:
        config_name = actor_to_config(node)
        command = "initComm"

        ok, latency, matched, _ = run_node_command(
            workdir=workdir,
            config_name=config_name,
            command=command,
            success_patterns=ACCESS_SUCCESS_PATTERNS,
            timeout=timeout,
        )

        results.append({
            "actor": node,
            "success": ok,
            "latency_ms": latency * 1000,
            "matched": matched,
        })

    return results

def execute_random_revocation_experiment(
    G,
    source,
    sink,
    mapping,
    workdir,
    revoke_seed=None,
    timeout=90,
):

    revoke_target_raw = choose_random_revocation_target(
        G, source, sink, mapping, seed=revoke_seed
    )
    revoke_target_name = mapping[revoke_target_raw]

    impacted_raw = get_revocation_impacted_nodes(G, revoke_target_raw)
    blocked_nodes = [mapping[n] for n in nx.topological_sort(G) if n in impacted_raw]
    all_nodes = get_all_executable_nodes(G, source, sink, mapping)
    revokers = get_revokers_for_target(G, revoke_target_raw, mapping)

    logging.info("\n=== Random Revocation Experiment ===")
    logging.info(f"Selected revoke target: {revoke_target_name}")
    logging.info(f"Revokers: {revokers}")
    logging.info(f"Blocked after revoke: {blocked_nodes}")
    logging.info(f"All executable nodes: {all_nodes}")

    revoke_results = []
    for revoker in revokers:
        result = run_revoke(
            grantor=revoker,
            grantee=revoke_target_name,
            obj="ResourceA",
            workdir=workdir,
            timeout=timeout,
        )
        revoke_results.append(result)

        status = "SUCCESS" if result["success"] else "FAIL"
        logging.info(
            f"{status:7s} | revoke by {revoker:8s} -> {revoke_target_name:8s} "
            f"| {result['latency_ms']:9.2f} ms | {result['matched']}"
        )

        if not result["success"]:
            logging.info("Revoke failed. Skip post-revoke validation.")
            return {
                "revoke_target": revoke_target_name,
                "blocked_nodes": blocked_nodes,
                "all_nodes": all_nodes,
                "revoke_results": revoke_results,
                "post_revoke_access_results": [],
                "validation_summary": [],
                "validation_ok": False,
            }

    logging.info("\n=== Post-Revoke Access Checks (All Nodes) ===")
    all_post_results = run_access_checks_for_all_nodes(
        all_nodes,
        workdir=workdir,
        timeout=timeout,
    )

    for r in all_post_results:
        status = "SUCCESS" if r["success"] else "FAIL"
        logging.info(
            f"{status:7s} | {r['actor']:8s} -> ResourceA "
            f"| {r['latency_ms']:9.2f} ms | {r['matched']}"
        )

    validation_ok, validation_summary = validate_post_revoke_results(
        all_post_results,
        blocked_nodes=blocked_nodes,
    )

    logging.info("\n=== Post-Revoke Validation Summary ===")
    for row in validation_summary:
        status = "OK" if row["valid"] else "MISMATCH"
        logging.info(
            f"{status:9s} | {row['actor']:8s} | "
            f"expected={row['expected']:10s} observed={row['observed']:10s} "
            f"| {row['latency_ms']:9.2f} ms | {row['matched']}"
        )

    logging.info(f"\nOverall revocation validation: {'PASS' if validation_ok else 'FAIL'}")

    return {
        "revoke_target": revoke_target_name,
        "blocked_nodes": blocked_nodes,
        "all_nodes": all_nodes,
        "revoke_results": revoke_results,
        "post_revoke_access_results": all_post_results,
        "validation_summary": validation_summary,
        "validation_ok": validation_ok,
    }


def validate_post_revoke_results(all_results, blocked_nodes):
    """
    blocked_nodes: set/list of actor names that should lose access
    """
    blocked_nodes = set(blocked_nodes)

    summary = []
    all_ok = True

    for r in all_results:
        actor = r["actor"]
        actual_has_access = r["success"]

        should_be_blocked = actor in blocked_nodes

        if should_be_blocked:
            expected = "BLOCKED"
            valid = not actual_has_access
            observed = "BLOCKED" if not actual_has_access else "HAS_ACCESS"
        else:
            expected = "HAS_ACCESS"
            valid = actual_has_access
            observed = "HAS_ACCESS" if actual_has_access else "BLOCKED"

        if not valid:
            all_ok = False

        summary.append({
            "actor": actor,
            "expected": expected,
            "observed": observed,
            "valid": valid,
            "latency_ms": r["latency_ms"],
            "matched": r["matched"],
        })

    return all_ok, summary


def start_resource():
    workdir="../iotauth/entity/node/example_entities"
    cmd = ["node", "server.js", "configs/net1/resource.config"]

    logging.info("\n=== Starting Resource Server ===")
    logging.info("Command: %s", cmd)
    logging.info("Working dir: %s", workdir)

    proc = subprocess.Popen(
        cmd,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    ready_patterns = [
        re.compile(r"listening on", re.IGNORECASE),
    ]

    start = time.perf_counter()

    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("Resource terminated unexpectedly")

        line = line.strip()
        logging.info("[auth] %s", line)

        if any(p.search(line) for p in ready_patterns):
            logging.info("Resource is ready.")
            break

        if time.perf_counter() - start > 30:
            raise TimeoutError("Resource startup timeout")

    return proc

def stop_auth_resource(proc):
    logging.info("\n=== Stopping Resource ===")
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    logging.info("Resource stopped.")

def stop_auth_server(proc):
    logging.info("\n=== Stopping Auth Server ===")
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    logging.info("Auth server stopped.")


if __name__ == "__main__":
    setup_logging(LOG_FILE)

    G, s, t, layers = generate_single_source_single_sink_dag(
        num_layers=5,
        nodes_per_layer=(2, 4),
        edge_prob=0.35,
        seed=67
    )
    logging.info("source candidates: %s", [n for n in G.nodes if G.in_degree(n) == 0])
    logging.info("sink candidates: %s", [n for n in G.nodes if G.out_degree(n) == 0])
    dag_to_graph_json(G, source="s", sink="t", out_path="generated.graph") 
    show_dag(G, layers)
    run_generate_all(
        graph_path="../../experiment/generated.graph",
        iotauth_dir="../iotauth/examples" 
    )
    auth_proc = start_auth_server(
        auth_dir="../iotauth/auth/auth-server",
        properties_path="../properties/exampleAuth101.properties"
    )
    logging.info(auth_proc)

    resource_proc = start_resource()
    logging.info(resource_proc)
    stop_auth_resource(resource_proc)
    stop_auth_server(auth_proc)
    # result = execute_delegation_plan(
    #     G=G,
    #     source=s,
    #     sink=t,
    #     workdir="../iotauth/entity/node/example_entities",  
    #     validity="1*day",
    #     timeout=90,
    # )
    # logging.info(result)

    # if all(r["success"] for r in result["delegation_results"]):
    #     revoke_result = execute_random_revocation_experiment(
    #         G=G,
    #         source=s,
    #         sink=t,
    #         mapping=result["mapping"],
    #         workdir="../iotauth/entity/node/example_entities",
    #         timeout=90,
    #     )
    # logging.info(revoke_result)
