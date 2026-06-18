import argparse
import json
import random
import time
import threading
import queue
import subprocess
import sys
from pathlib import Path
import networkx as nx

CRYPTO_SPEC_OBJ = {
    "cipher": "AES-128-CBC",
    "mac": "SHA256",
}

PRIV_INFO = {
    "cryptoSpec": "AES-128-CBC:SHA256",
    "absValidity": "1*day",
    "relValidity": "1*hour",
}


def make_auth(auth_id: int, base_port: int = 21900):
    return {
        "id": auth_id,
        "entityHost": "localhost",
        "authHost": "localhost",
        "tcpPort": base_port,
        "udpPort": base_port + 2,
        "authPort": base_port + 1,
        "callbackPort": base_port + 3,
        "dbProtectionMethod": 1,
        "backupEnabled": False,
        "contextualCallbackEnabled": True,
    }


def make_node_entity(i: int, net_name: str = "net1"):
    return {
        "group": f"Node{i}",
        "name": f"{net_name}.node{i}",
        "distProtocol": "TCP",
        "usePermanentDistKey": True,
        "distKeyValidityPeriod": "365*day",
        "maxSessionKeysPerRequest": 5,
        "netName": net_name,
        "credentialPrefix": f"Net1.Node{i}",
        "distributionCryptoSpec": dict(CRYPTO_SPEC_OBJ),
        "sessionCryptoSpec": dict(CRYPTO_SPEC_OBJ),
        "backupToAuthIds": [],
    }


def make_resource_entity(resource_name: str, port: int = 21100, net_name: str = "net1"):
    local_name = resource_name[0].lower() + resource_name[1:]

    return {
        "group": resource_name,
        "name": f"{net_name}.{local_name}",
        "port": port,
        "distProtocol": "TCP",
        "usePermanentDistKey": False,
        "distKeyValidityPeriod": "365*day",
        "maxSessionKeysPerRequest": 30,
        "netName": net_name,
        "credentialPrefix": f"Net1.{resource_name}",
        "distributionCryptoSpec": dict(CRYPTO_SPEC_OBJ),
        "sessionCryptoSpec": dict(CRYPTO_SPEC_OBJ),
        "host": "localhost",
        "backupToAuthIds": [],
    }


def generate_dag(nodes, edge_prob=0.4, seed=None, force_chain=False):
    rng = random.Random(seed)

    dag = nx.DiGraph()
    dag.add_nodes_from(nodes)

    if force_chain:  # If force_chain is true: add node1 -> node2 -> node3 -> ... -> nodeN
        for i in range(len(nodes) - 1):
            dag.add_edge(nodes[i], nodes[i + 1])

    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            if rng.random() < edge_prob:
                dag.add_edge(nodes[i], nodes[j])

    validate_dag(dag)

    return dag


def validate_dag(dag):
    if not nx.is_directed_acyclic_graph(dag):
        raise ValueError("Generated graph is not a DAG")

    for src, dst in dag.edges():
        if src == dst:
            raise ValueError(f"Self-edge is not allowed: {src} -> {dst}")

        if dag.has_edge(dst, src):
            raise ValueError(f"Bidirectional edge is not allowed: {src} <-> {dst}")


def make_delegation_privilege(src: str, dst: str, resource: str, validity: str):
    return {
        "privilegeType": "DelegationGrant",
        "privilegedGroup": src,
        "subject": dst,
        "object": resource,
        "validity": validity,
        "info": dict(PRIV_INFO),
    }


def make_revocation_privilege(src: str, dst: str, resource: str, validity: str):
    return {
        "privilegeType": "DelegationRevoke",
        "privilegedGroup": src,
        "subject": dst,
        "object": resource,
        "validity": validity,
        "info": dict(PRIV_INFO),
    }


def select_resource_dag_edges(overall_dag, probability=0.5, seed=None):
    rng = random.Random(seed)

    edges = list(overall_dag.edges())
    rng.shuffle(edges)  # avoid always preferring earlier edges

    selected_edges = []
    already_delegated_nodes = set()

    for src, dst in edges:
        if dst in already_delegated_nodes:
            continue

        if rng.random() < probability:
            selected_edges.append((src, dst))
            already_delegated_nodes.add(dst)

    return selected_edges


def print_dag_edges(title, edges):
    print(f"\n{title}")

    if not edges:
        print("  No delegation edges")
        return

    for src, dst in edges:
        print(f"  {src} -> {dst}")


def print_dag_hierarchy(title, dag):
    print(f"\n{title}")

    for node in nx.topological_sort(dag):
        children = list(dag.successors(node))

        if children:
            print(f"  {node} -> {', '.join(children)}")
        else:
            print(f"  {node} -> []")


def build_graph(
        node_count,
        resource_count,
        auth_id,
        edge_prob,
        revoke_prob,
        seed,
        validity,
        print_detail,
):
    node_groups = [
        f"Node{i}"
        for i in range(1, node_count + 1)
    ]

    resources = [
        f"Resource{i}"
        for i in range(1, resource_count + 1)
    ]

    entity_list = [
        make_node_entity(i)
        for i in range(1, node_count + 1)
    ]

    entity_list.extend(
        make_resource_entity(resource, port=21100 + idx)
        for idx, resource in enumerate(resources)
    )

    assignments = {
        entity["name"]: auth_id
        for entity in entity_list
    }

    overall_dag = generate_dag(
        node_groups,
        edge_prob=edge_prob,
        seed=seed,
        force_chain=True,
    )

    resource_edges = {}

    for idx, resource in enumerate(resources):
        resource_seed = None if seed is None else seed + idx + 1

        resource_edges[resource] = select_resource_dag_edges(
            overall_dag,
            probability=0.5,
            seed=resource_seed,
        )

    required_access = {}
    access_before_revoke = {}
    access_after_revoke = {}

    for resource, edges in resource_edges.items():
        delegated_to = set()
        for _, dst in edges:
            delegated_to.add(dst)

        heads = []
        for node in node_groups:
            if node not in delegated_to:
                heads.append(node)

        for head in heads:
            required_access.setdefault(head, set()).add(resource)
            access_before_revoke.setdefault(head, set()).add(resource)
            access_after_revoke.setdefault(head, set()).add(resource)

    privilege_list = []
    revocation_list = []

    revocation_probability = revoke_prob
    revocation_rng = random.Random(None if seed is None else seed + 10000)

    for resource, edges in resource_edges.items():
        for src, dst in edges:
            privilege_list.append(
                make_delegation_privilege(
                    src,
                    dst,
                    resource,
                    validity,
                )
            )
            access_before_revoke.setdefault(dst, set()).add(resource)
            access_after_revoke.setdefault(dst, set()).add(resource)

            if revocation_rng.random() < revocation_probability:
                revocation_list.append(
                    make_revocation_privilege(
                        src,
                        dst,
                        resource,
                        validity,
                    )
                )
                access_after_revoke[dst].remove(resource)

    privilege_list.sort(
        key=lambda p: (
            p["object"],
            int(p["privilegedGroup"].replace("Node", "")),
            int(p["subject"].replace("Node", "")),
        )
    )

    revocation_list.sort(
        key=lambda p: (
            p["object"],
            int(p["privilegedGroup"].replace("Node", "")),
            int(p["subject"].replace("Node", "")),
        )
    )

    privilege_list.extend(revocation_list)

    print("\nRequired initial access for head node(s):")
    for node in sorted(required_access):
        resources_str = ", ".join(sorted(required_access[node]))
        print(f"  {node}: {resources_str}")

    print_dag_edges(
        "Overall DAG delegation graph:",
        list(overall_dag.edges()),
    )

    print_dag_hierarchy(
        "Overall DAG hierarchy:",
        overall_dag,
    )

    if print_detail:
        print("\nResource-specific DAG delegation edges:")
        for resource, edges in resource_edges.items():
            print_dag_edges(f"[{resource}]", edges)
    sys.stdout.flush()

    return {
               "authList": [make_auth(auth_id)],
               "authTrusts": [],
               "assignments": assignments,
               "entityList": entity_list,
               "filesharingLists": [],
               "privilegeList": privilege_list,
           }, overall_dag, resource_edges, required_access, access_before_revoke, access_after_revoke


def start_output_reader(proc, prefix):
    q = queue.Queue()

    def reader():
        for line in proc.stdout:
            print(f"[{prefix}] {line}", end="", flush=True)
            q.put(line)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    return q


def wait_for_output(output_q, success_patterns, timeout=1, failure_patterns=None):
    if isinstance(success_patterns, str):
        success_patterns = [success_patterns]

    if failure_patterns is None:
        failure_patterns = []

    if isinstance(failure_patterns, str):
        failure_patterns = [failure_patterns]

    start = time.perf_counter()
    collected = []

    while time.perf_counter() - start < timeout:
        try:
            line = output_q.get(timeout=0.01)
        except queue.Empty:
            continue

        collected.append(line)

        for pattern in failure_patterns:
            if pattern in line:
                raise RuntimeError(
                    f"Failure pattern detected: {pattern}"
                )

        for pattern in success_patterns:
            if pattern in line:
                return collected

    raise TimeoutError(
        f"Timeout waiting for patterns: {success_patterns}"
    )


def drain_output(output_q):
    drained = []
    while True:
        try:
            drained.append(output_q.get_nowait())
        except queue.Empty:
            break
    return drained


def get_file_size_bytes(path):
    path = Path(path)
    if not path.exists():
        return None
    return path.stat().st_size


def test_access_by_init_comm(
    access_map,
    node_procs,
    node_outputs,
    output_path,
    phase_name,
    timeout=1,
):
    results = []

    for node, resources in access_map.items():
        proc = node_procs[node]
        output_q = node_outputs[node]

        for resource in resources:
            resource_name = resource[0].lower() + resource[1:]
            target = f"net1.{resource_name}"
            cmd = f"initComm {target}\n"

            print(f"\n[{phase_name}] Testing {node} -> {target}")
            print(f"{node}: {cmd.strip()}", flush=True)

            drain_output(output_q)

            proc.stdin.write(cmd)
            proc.stdin.flush()

            success = True
            error = None

            try:
                wait_for_output(
                    output_q,
                    success_patterns=[
                        "switching to IN_COMM",
                    ],
                    failure_patterns=[
                        "Handler: Error in secure comm",
                    ],
                    timeout=timeout,
                )
            except RuntimeError as e:
                success = False
                error = f"AUTH_FAILURE: {e}"

            except TimeoutError as e:
                success = False
                error = f"TIMEOUT: {e}"

            results.append(
                {
                    "phase": phase_name,
                    "node": node,
                    "resource": resource,
                    "target": target,
                    "success": success,
                    "error": error,
                }
            )

            print(
                f"[{phase_name}] {node} -> {resource}: "
                f"{'SUCCESS' if success else 'FAIL'} "
                , flush=True
            )

    output_path.write_text(json.dumps(results, indent=4))
    print(f"Wrote {output_path}")

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="random_dag", help="Output file name for the graph and json file")
    parser.add_argument("--nodes", type=int, default=6, help="Number of nodes: Node1..NodeN")
    parser.add_argument("--resources", type=int, default=3, help="Number of resources (Resource1..ResourceN)")
    parser.add_argument("--auth-id", type=int, default=101, help="Single Auth ID used for all assignments")
    parser.add_argument("--edge-prob", type=float, default=0.4,
                        help="Probability of extra DAG edges. If this value is 1, then it will return all possible "
                             "edges. ex) 3 nodes -> 3! edges")
    parser.add_argument("--revoke-prob", type=float, default=0.3, help="Probability of generating DelegationRevoke "
                                                                       "for each DelegationGrant edge")
    parser.add_argument("--seed", type=int, default=10, help="Random seed for reproducibility")
    parser.add_argument("--validity", default="1*day", help="Privilege validity")
    parser.add_argument("--print-detail", default=True, help="Print Resource-specific DAG delegation edge")
    args = parser.parse_args()

    if args.nodes < 1:
        raise ValueError("--nodes must be >= 1")

    if args.resources < 1:
        raise ValueError("--resources must be >= 1")

    if not 0 <= args.edge_prob <= 1:
        raise ValueError("--edge-prob must be between 0 and 1")

    graph, overall_dag, resource_edges, required_access, access_before_revoke, access_after_revoke = build_graph(
        node_count=args.nodes,
        resource_count=args.resources,
        auth_id=args.auth_id,
        edge_prob=args.edge_prob,
        revoke_prob=args.revoke_prob,
        seed=args.seed,
        validity=args.validity,
        print_detail=args.print_detail,
    )
    output_prefix = Path(f"results/{args.output}")
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    output_graph_path = output_prefix.with_suffix(".graph")
    output_graph_path.write_text(json.dumps(graph, indent="\t"))
    print(f"\nWrote {output_graph_path}")

    initial_access = []
    for node in sorted(required_access):
        for resource in sorted(required_access[node]):
            initial_access.append(
                {
                    "RequestingGroup": node,
                    "TargetType": "Group",
                    "Target": resource,
                    "MaxNumSessionKeyOwners": 2,
                    "SessionCryptoSpec": "AES-128-CBC:SHA256",
                    "AbsoluteValidity": "1*day",
                    "RelativeValidity": "2*hour",
                    "Expiration": "Infinity",
                    "IsDelegated": 0,
                }
            )

    access_path = output_prefix.with_suffix(".json")
    access_path.write_text(json.dumps(initial_access, indent=4))
    print(f"Wrote {access_path}")

    before_revoke_path = output_prefix.with_name(f"{output_prefix.stem}_access_before_revoke.json")
    before_revoke_path.write_text(json.dumps({k: list(v) for k, v in access_before_revoke.items()}, indent=4))
    print(f"Wrote {before_revoke_path}")

    after_revoke_path = output_prefix.with_name(f"{output_prefix.stem}_access_after_revoke.json")
    after_revoke_path.write_text(json.dumps({k: list(v) for k, v in access_after_revoke.items()}, indent=4))
    print(f"Wrote {after_revoke_path}")

    sys.stdout.flush()

    # Run generateAll.sh from multi-delegation/iotauth/examples
    experiment_dir = Path(__file__).resolve().parent
    project_root = experiment_dir.parent
    examples_dir = project_root / "iotauth" / "examples"
    auth_dir = project_root / "iotauth" / "auth" / "auth-server"
    example_entities_dir = project_root / "iotauth" / "entity" / "node" / "example_entities"
    auth_db_path = project_root / "iotauth" / "auth" / "databases" / "auth101" / "auth.db"

    resource_procs = []
    auth_proc = None
    node_procs = {}
    node_outputs = {}

    graph_arg = Path("../../experiment/results") / output_graph_path.name
    policy_arg = Path("../../experiment/results") / access_path.name

    cmd = [
        "./generateAll.sh",
        "-g",
        str(graph_arg),
        "-po",
        str(policy_arg),
    ]

    print("\nRuns:")
    print(f"  cd {examples_dir}")
    print(f"  {' '.join(cmd)}")

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

    try:
        auth_proc = subprocess.Popen(
            [
                "java",
                "-jar",
                "target/auth-server-jar-with-dependencies.jar",
                "-p",
                "../properties/exampleAuth101.properties",
            ],
            cwd=auth_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        auth_output_q = start_output_reader(auth_proc, "Auth")

        wait_for_output(
            auth_output_q,
            ["Are you sure to continue(y/n)?"],
            timeout=10,
        )
        
        auth_proc.stdin.write("y\n")
        auth_proc.stdin.flush()
        
        wait_for_output(
            auth_output_q,
            ["Please enter Auth password"],
            timeout=10,
        )
        
        auth_proc.stdin.write("asdf\n")
        auth_proc.stdin.flush()
        
        wait_for_output(
            auth_output_q,
            ["Started Server@"],
            timeout=10,
        )
        print("Auth server is ready")

        for i in range(1, args.resources + 1):
            proc = subprocess.Popen(
                [
                    "node",
                    "server.js",
                    f"configs/net1/resource{i}.config",
                ],
                cwd=example_entities_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            output_q = start_output_reader(proc, f"resource{i}")
            wait_for_output(
                output_q,
                [
                    "Handler: listening on port",
                ],
                timeout=5,
            )
            resource_procs.append(proc)
            print(f"Started Resource{i}")

        for i in range(1, args.nodes + 1):
            node_name = f"Node{i}"
            proc = subprocess.Popen(
                [
                    "node",
                    "user.js",
                    f"configs/net1/node{i}.config",
                ],
                cwd=example_entities_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            node_procs[node_name] = proc
            node_outputs[node_name] = start_output_reader(proc, f"node{i}")
            wait_for_output(
                node_outputs[node_name],
                [
                    "current parameters:",
                    f"net1.node{i}:Node{i} prompt>",
                ],
                timeout=5,
            )
            print(f"{node_name} is ready")

        time.sleep(5)

        auth_db_size_before_delegation = get_file_size_bytes(auth_db_path)
        delegation_start = time.perf_counter()

        # Perform delegation
        for privilege in graph["privilegeList"]:
            if privilege["privilegeType"] != "DelegationGrant":
                continue

            src = privilege["privilegedGroup"]
            dst = privilege["subject"]
            resource = privilege["object"]
            validity = privilege["validity"]

            cmd = f"delegateAuthority {dst} {resource} {validity}\n"

            proc = node_procs[src]
            output_q = node_outputs[src]

            print(f"\nExecuting on {src}: {cmd.strip()}")

            proc.stdin.write(cmd)
            proc.stdin.flush()

            wait_for_output(
                output_q,
                [
                    # "Finished privilege request",
                    "disconnected from auth",
                ],
                timeout=1,
            )
            sys.stdout.flush()
            # time.sleep(0.5)
        delegation_end = time.perf_counter()
        auth_db_size_after_delegation = get_file_size_bytes(auth_db_path)

        experiment_start2 = time.perf_counter()
        # Test assigned accesses after delegation
        before_revoke_test_path = Path("access_before_revoke_test.json")
        before_revoke_results = test_access_by_init_comm(
            access_map=access_before_revoke,
            node_procs=node_procs,
            node_outputs=node_outputs,
            output_path=before_revoke_test_path,
            phase_name="before_revoke",
        )

        experiment_end2 = time.perf_counter()
        auth_db_size_after_access_checking = get_file_size_bytes(auth_db_path)

        # Stop nodes
        for node_name, proc in node_procs.items():
            print(f"Stopping {node_name}")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        node_procs = {}
        node_outputs = {}
        # Reopen the nodes
        for i in range(1, args.nodes + 1):
            node_name = f"Node{i}"
            proc = subprocess.Popen(
                [
                    "node",
                    "user.js",
                    f"configs/net1/node{i}.config",
                ],
                cwd=example_entities_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            node_procs[node_name] = proc
            node_outputs[node_name] = start_output_reader(proc, f"node{i}")
            wait_for_output(
                node_outputs[node_name],
                [
                    "current parameters:",
                    f"net1.node{i}:Node{i} prompt>",
                ],
                timeout=5,
            )
            print(f"{node_name} is ready")
        sys.stdout.flush()

        auth_db_size_before_revocation = get_file_size_bytes(auth_db_path)
        experiment_start3 = time.perf_counter()
        # Perform revocation
        for privilege in graph["privilegeList"]:
            if privilege["privilegeType"] != "DelegationRevoke":
                continue

            src = privilege["privilegedGroup"]
            dst = privilege["subject"]
            resource = privilege["object"]

            cmd = f"revoke {dst} {resource}\n"

            proc = node_procs[src]
            output_q = node_outputs[src]

            print(f"\nExecuting on {src}: {cmd.strip()}")

            proc.stdin.write(cmd)
            proc.stdin.flush()

            wait_for_output(
                output_q,
                [
                    # "Finished privilege request",
                    "disconnected from auth",
                ],
                timeout=5,
            )
            sys.stdout.flush()
            # time.sleep(0.5)
        experiment_end3 = time.perf_counter()
        auth_db_size_after_revocation = get_file_size_bytes(auth_db_path)

        experiment_start4 = time.perf_counter()
        # Test assigned accesses after revocation
        after_revoke_test_path = Path("access_after_revoke_test.json")
        after_revoke_results = test_access_by_init_comm(
            access_map=access_before_revoke,
            node_procs=node_procs,
            node_outputs=node_outputs,
            output_path=after_revoke_test_path,
            phase_name="after_revoke",
        )
        experiment_end4 = time.perf_counter()
        auth_db_size_after_access_checking2 = get_file_size_bytes(auth_db_path)

        delegation_latency_ms = (delegation_end - delegation_start) * 1000
        before_revoke_access_latency_ms = (experiment_end2 - experiment_start2) * 1000
        revocation_latency_ms = (experiment_end3 - experiment_start3) * 1000
        after_revoke_access_latency_ms = (experiment_end4 - experiment_start4) * 1000
        summary = {
            "auth_db_size_bytes": {
                "before_delegation": auth_db_size_before_delegation,
                "after_delegation": auth_db_size_after_delegation,
                "after_access_checking1": auth_db_size_after_access_checking,
                "before_revocation": auth_db_size_before_revocation,
                "after_revocation": auth_db_size_after_revocation,
                "after_access_checking2": auth_db_size_after_access_checking2,
            },
            "delegation_latency_ms": delegation_latency_ms,
            "before_revoke_access_latency_ms": before_revoke_access_latency_ms,
            "revocation_latency_ms": revocation_latency_ms,
            "after_revoke_access_latency_ms": after_revoke_access_latency_ms,
            "access_check_count_before": len(before_revoke_results),
            "access_check_count_after": len(after_revoke_results),
        }

        summary_path = output_prefix.with_name(f"{output_prefix.stem}_latency.json")
        summary_path.write_text(json.dumps(summary, indent=4))
        print(f"Wrote latency in {summary_path}")

    finally:
        # Stopping Resources
        for i, proc in enumerate(resource_procs, start=1):
            print(f"Stopping Resource{i}")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        time.sleep(3)
        # Stopping Nodes
        for node_name, proc in node_procs.items():
            print(f"Stopping {node_name}")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        time.sleep(3)
        # Stopping Auth
        if auth_proc is not None:
            print("Stopping Auth server")
            auth_proc.terminate()
            try:
                auth_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                auth_proc.kill()


if __name__ == "__main__":
    main()
