import argparse
from itertools import combinations
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


def balanced_auth_assignments(names, auth_ids):
    """Assign consecutive groups evenly; earlier entries get any remainder."""
    quotient, remainder = divmod(len(names), len(auth_ids))
    assignments = {}
    start = 0
    for index, auth_id in enumerate(auth_ids):
        stop = start + quotient + (index < remainder)
        assignments.update((name, auth_id) for name in names[start:stop])
        start = stop
    return assignments


def make_auth_topology(entity_list, node_count, auth_id, auth_count):
    if auth_count < 1:
        raise ValueError("--auths must be >= 1")
    if auth_id < 1:
        raise ValueError("--auth-id must be >= 1")
    if 21900 + 10 * (auth_count - 1) + 3 > 65535:
        raise ValueError("Too many Auths for the available port range")

    auth_ids = list(range(auth_id, auth_id + auth_count))
    auth_list = [make_auth(value, 21900 + 10 * index)
                 for index, value in enumerate(auth_ids)]
    assignments = balanced_auth_assignments(
        [entity["name"] for entity in entity_list[:node_count]], auth_ids)

    # Rotate resources by one Auth: with two Auths, Resource1 belongs to Auth2.
    assignments.update(balanced_auth_assignments(
        [entity["name"] for entity in entity_list[node_count:]],
        auth_ids[1:] + auth_ids[:1]))

    auth_ports = {auth[key] for auth in auth_list
                  for key in ("tcpPort", "udpPort", "authPort", "callbackPort")}
    resource_ports = {entity["port"] for entity in entity_list if "port" in entity}

    if resource_ports & auth_ports or any(port > 65535 for port in resource_ports):
        raise ValueError("Resource ports overlap Auth ports or exceed 65535")

    trusts = [{"id1": first, "id2": second}
              for first, second in combinations(auth_ids, 2)]

    # Use the owning Auth's ordinal for all entity names, configs, and credentials.
    auth_networks = {value: f"net{index + 1}" for index, value in enumerate(auth_ids)}
    renamed_assignments = {}
    for entity in entity_list:
        assigned_auth = assignments[entity["name"]]
        net_name = auth_networks[assigned_auth]
        local_name = entity["name"].split(".", 1)[1]
        entity.update(
            name=f"{net_name}.{local_name}",
            netName=net_name,
            credentialPrefix=f"{net_name.capitalize()}.{entity['group']}",
        )
        renamed_assignments[entity["name"]] = assigned_auth
    return auth_list, trusts, renamed_assignments


def describe_topology(graph):
    group_auths = {entity["group"]: graph["assignments"][entity["name"]]
                   for entity in graph["entityList"]}
    print("\nEntity assignments (Auth1 starts at --auth-id):")
    for index, auth in enumerate(graph["authList"], start=1):
        groups = [entity["name"] for entity in graph["entityList"]
                  if graph["assignments"][entity["name"]] == auth["id"]]
        print(f"  Auth{index} (ID {auth['id']}): {', '.join(groups) or '(empty)'}")
    print("Delegation is between nodes on the same Auth; resources may be on any Auth.")
    return group_auths


def make_node_entity(i: int, net_name: str = "net1"):
    return {
        "group": f"Node{i}",
        "name": f"{net_name}.node{i}",
        "distProtocol": "TCP",
        "usePermanentDistKey": True,
        "distKeyValidityPeriod": "365*day",
        "maxSessionKeysPerRequest": 5,
        "netName": net_name,
        "credentialPrefix": f"{net_name.capitalize()}.Node{i}",
        "distributionCryptoSpec": dict(CRYPTO_SPEC_OBJ),
        "sessionCryptoSpec": dict(CRYPTO_SPEC_OBJ),
        "backupToAuthIds": [],
    }


def entity_config_path(entity):
    local_name = entity["name"].split(".", 1)[1]
    return f"configs/{entity['netName']}/{local_name}.config"


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
        "credentialPrefix": f"{net_name.capitalize()}.{resource_name}",
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
        "subjectGroup": dst,
        "objectGroup": resource,
        "validity": validity,
    }


def make_revocation_privilege(src: str, dst: str, resource: str, validity: str):
    return {
        "privilegeType": "DelegationRevoke",
        "privilegedGroup": src,
        "subjectGroup": dst,
        "objectGroup": resource,
        "validity": validity,
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
        auth_count=1,
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

    auth_list, auth_trusts, assignments = make_auth_topology(
        entity_list, node_count, auth_id, auth_count)
    node_auths = {entity["group"]: assignments[entity["name"]]
                  for entity in entity_list[:node_count]}

    # Delegated policies are stored on the delegating node's Auth. Keep the
    # recipient there too, while allowing resources on every trusted Auth.
    overall_dag = nx.DiGraph()
    overall_dag.add_nodes_from(node_groups)
    for index, auth in enumerate(auth_list):
        local_nodes = [node for node in node_groups if node_auths[node] == auth["id"]]
        local_dag = generate_dag(
            local_nodes, edge_prob=edge_prob,
            seed=None if seed is None else seed + index, force_chain=True)
        overall_dag.add_edges_from(local_dag.edges())

    resource_edges = {}

    for idx, resource in enumerate(resources):
        resource_seed = None if seed is None else seed + idx + 1

        resource_edges[resource] = select_resource_dag_edges(
            overall_dag,
            probability=0.5,
            seed=resource_seed,
        )
        if auth_count > 1:
            for auth in auth_list:
                local_edges = [(src, dst) for src, dst in overall_dag.edges()
                               if node_auths[src] == auth["id"]]
                if local_edges and not any(node_auths[src] == auth["id"]
                                           for src, _ in resource_edges[resource]):
                    resource_edges[resource].append(local_edges[0])

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
    access_after_revoke = {
        node: set(resources)
        for node, resources in access_before_revoke.items()
    }

    for node, resource in get_revoked_access_pairs(resource_edges, revocation_list):
        access_after_revoke[node].discard(resource)

    privilege_list.sort(
        key=lambda p: (
            p["objectGroup"],
            int(p["privilegedGroup"].replace("Node", "")),
            int(p["subjectGroup"].replace("Node", "")),
        )
    )

    revocation_list.sort(
        key=lambda p: (
            p["objectGroup"],
            int(p["privilegedGroup"].replace("Node", "")),
            int(p["subjectGroup"].replace("Node", "")),
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
               "authList": auth_list,
               "authTrusts": auth_trusts,
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
                    f"Failure pattern detected: {pattern}: {line.strip()}"
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


def build_delegation_records(graph, group_auths):
    group_names = {entity["group"]: entity["name"] for entity in graph.get("entityList", [])}
    records = []
    for privilege in graph["privilegeList"]:
        if privilege["privilegeType"] != "DelegationGrant":
            continue
        src = privilege["privilegedGroup"]
        dst = privilege["subjectGroup"]
        resource = privilege["objectGroup"]
        records.append({
            "id": len(records) + 1,
            "delegator": src,
            "delegatee": dst,
            "delegator_entity": group_names[src],
            "delegatee_entity": group_names[dst],
            "resource": resource,
            "resource_entity": group_names[resource],
            "delegator_auth": group_auths[src],
            "delegatee_auth": group_auths[dst],
            "resource_auth": group_auths[resource],
            "validity": privilege["validity"],
            "status": "pending",
            "success": None,
            "latency_ms": None,
            "error": None,
        })
    return records


def save_delegation_records(output_path, records):
    # Replace the complete JSON atomically, preserving a readable checkpoint.
    temporary_path = output_path.with_name(output_path.name + ".tmp")
    temporary_path.write_text(json.dumps(records, indent=4))
    temporary_path.replace(output_path)


def run_delegations(records, node_procs, node_outputs, output_path, timeout=10):
    for record in records:
        src = record["delegator"]
        dst = record["delegatee"]
        resource = record["resource"]
        validity = record["validity"]
        cmd = f"delegateAuthority {dst} {resource} {validity} 1*day 1*hour\n"
        proc = node_procs[src]
        output_q = node_outputs[src]
        print(f"\nExecuting on {src} (Auth{record['delegator_auth']}), "
              f"resource on Auth{record['resource_auth']}: {cmd.strip()}", flush=True)
        drain_output(output_q)
        record["status"] = "running"
        save_delegation_records(output_path, records)
        started = time.perf_counter()
        try:
            proc.stdin.write(cmd)
            proc.stdin.flush()
            wait_for_output(
                output_q, ["action: 'DelegationGrant'"],
                failure_patterns=["Handler: Error", "received an Auth alert!"],
                timeout=timeout,
            )
        except Exception as exc:
            record.update(
                status="timeout" if isinstance(exc, TimeoutError) else "failed",
                success=False, error=f"{type(exc).__name__}: {exc}",
            )
            # Preserve fail-fast behavior: dependent delegations must not be
            # treated as executable after their parent delegation has failed.
            raise
        except KeyboardInterrupt:
            record.update(status="interrupted", success=None, error="KeyboardInterrupt")
            raise
        else:
            record.update(status="succeeded", success=True)
        finally:
            # Measure the command/response interval, excluding JSON file I/O.
            record["latency_ms"] = (time.perf_counter() - started) * 1000
            save_delegation_records(output_path, records)
        print(f"Delegation {record['id']}: SUCCESS ({record['latency_ms']:.3f} ms)", flush=True)


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
    entity_names,
    timeout=10,
    group_auths=None,
    expected_access=None,
):
    results = []

    for node, resources in access_map.items():
        proc = node_procs[node]
        output_q = node_outputs[node]

        for resource in sorted(resources):
            target = entity_names[resource]
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
                    "node_entity": entity_names[node],
                    "resource": resource,
                    "target": target,
                    "success": success,
                    "error": error,
                    "node_auth": group_auths[node] if group_auths else None,
                    "resource_auth": group_auths[resource] if group_auths else None,
                    "expected_success": resource in expected_access.get(node, set())
                        if expected_access is not None else True,
                    "matches_expected": success == (resource in expected_access.get(node, set()))
                        if expected_access is not None else success,
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


def get_revoked_access_pairs(resource_edges, privileges):
    """Find direct and descendant revocations in each resource's delegation DAG."""
    resource_dags = {resource: nx.DiGraph(edges)
                     for resource, edges in resource_edges.items()}
    revoked_pairs = set()
    for privilege in privileges:
        if privilege["privilegeType"] != "DelegationRevoke":
            continue
        src = privilege["privilegedGroup"]
        dst = privilege["subjectGroup"]
        resource = privilege["objectGroup"]
        dag = resource_dags.get(resource)
        if dag is None or not dag.has_edge(src, dst):
            raise ValueError(f"Revocation has no delegation edge: {src} -> {dst} ({resource})")
        revoked_nodes = {dst} | nx.descendants(dag, dst)
        revoked_pairs.update((node, resource) for node in revoked_nodes)
    return revoked_pairs


def count_revoked_accesses(resource_edges, privileges):
    """Count unique access pairs removed by graph revocations, including cascades."""
    return len(get_revoked_access_pairs(resource_edges, privileges))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="random_dag", help="Output file name for the graph and json file")
    parser.add_argument("--nodes", type=int, default=6, help="Number of nodes: Node1..NodeN")
    parser.add_argument("--resources", type=int, default=3, help="Number of resources (Resource1..ResourceN)")
    parser.add_argument("--auths", type=int, default=1, help="Number of Auth servers (default: 1)")
    parser.add_argument("--auth-id", type=int, default=101, help="First Auth ID; subsequent IDs are consecutive")
    parser.add_argument("--generate-only", action="store_true", help="Write topology and expected policies without starting servers")
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

    if not 0 <= args.revoke_prob <= 1:
        raise ValueError("--revoke-prob must be between 0 and 1")

    graph, overall_dag, resource_edges, required_access, access_before_revoke, access_after_revoke = build_graph(
        node_count=args.nodes,
        resource_count=args.resources,
        auth_id=args.auth_id,
        auth_count=args.auths,
        edge_prob=args.edge_prob,
        revoke_prob=args.revoke_prob,
        seed=args.seed,
        validity=args.validity,
        print_detail=args.print_detail,
    )
    group_auths = describe_topology(graph)
    entities_by_group = {entity["group"]: entity for entity in graph["entityList"]}
    entity_names = {group: entity["name"] for group, entity in entities_by_group.items()}
    if not args.output or Path(args.output).name != args.output or args.output in (".", ".."):
        raise ValueError("--output must be a directory name, not a path")
    experiment_dir = Path(__file__).resolve().parent
    output_prefix = experiment_dir / "results" / args.output / args.output
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

    initial_access_path = output_prefix.with_name(f"{output_prefix.stem}_initial_access.json")
    initial_access_map = {
        f"Node{i}": sorted(required_access.get(f"Node{i}", set()))
        for i in range(1, args.nodes + 1)
    }
    initial_access_path.write_text(json.dumps(initial_access_map, indent=4))
    print(f"Wrote {initial_access_path}")

    delegations_path = output_prefix.with_name(f"{output_prefix.stem}_delegations.json")
    delegation_records = build_delegation_records(graph, group_auths)
    save_delegation_records(delegations_path, delegation_records)
    print(f"Wrote {delegations_path}")

    before_revoke_path = output_prefix.with_name(f"{output_prefix.stem}_access_before_revoke.json")
    before_revoke_path.write_text(json.dumps({k: sorted(v) for k, v in access_before_revoke.items()}, indent=4))
    print(f"Wrote {before_revoke_path}")

    after_revoke_path = output_prefix.with_name(f"{output_prefix.stem}_access_after_revoke.json")
    after_revoke_path.write_text(json.dumps({k: sorted(v) for k, v in access_after_revoke.items()}, indent=4))
    print(f"Wrote {after_revoke_path}")

    sys.stdout.flush()

    if args.generate_only:
        return

    # part2 is nested under experiments/, two levels below the project root.
    project_root = experiment_dir.parent.parent
    examples_dir = project_root / "iotauth" / "examples"
    auth_dir = project_root / "iotauth" / "auth" / "auth-server"
    example_entities_dir = project_root / "iotauth" / "entity" / "node" / "example_entities"
    auth_db_paths = {
        auth["id"]: project_root / "iotauth" / "auth" / "databases" / f"auth{auth['id']}" / "auth.db"
        for auth in graph["authList"]
    }
    db_snapshots = {}

    def snapshot_databases(phase):
        sizes = {str(auth_id): get_file_size_bytes(path) for auth_id, path in auth_db_paths.items()}
        db_snapshots[phase] = sizes
        return sum(sizes.values()) if all(size is not None for size in sizes.values()) else None

    resource_procs = []
    auth_procs = {}
    node_procs = {}
    node_outputs = {}

    graph_arg = output_graph_path
    policy_arg = access_path

    cmd = [
        "./generateAll.sh",
        "-g",
        str(graph_arg),
        "-po",
        str(policy_arg),
        "-p",
        "asdf",
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
        for auth in graph["authList"]:
            current_auth_id = auth["id"]
            auth_proc = subprocess.Popen(
                [
                    "java", "-jar", "target/auth-server-jar-with-dependencies.jar",
                    "-p", f"../properties/exampleAuth{current_auth_id}.properties",
                    "-s", "asdf",
                ],
                cwd=auth_dir,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            # Register immediately so startup failures still clean up this process.
            auth_procs[current_auth_id] = auth_proc
            auth_output_q = start_output_reader(auth_proc, f"Auth{current_auth_id}")
            wait_for_output(
                auth_output_q, ["Started Server@"], timeout=30,
                failure_patterns=["Exception in thread", "Address already in use"],
            )
            print(f"Auth{current_auth_id} is ready")

        for i in range(1, args.resources + 1):
            resource_entity = entities_by_group[f"Resource{i}"]
            proc = subprocess.Popen(
                [
                    "node",
                    "server.js",
                    entity_config_path(resource_entity),
                ],
                cwd=example_entities_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            resource_procs.append(proc)
            output_q = start_output_reader(proc, resource_entity["name"])
            wait_for_output(
                output_q,
                [
                    "Handler: listening on port",
                ],
                timeout=5,
            )
            print(f"Started Resource{i}")

        for i in range(1, args.nodes + 1):
            node_name = f"Node{i}"
            node_entity = entities_by_group[node_name]
            proc = subprocess.Popen(
                [
                    "node",
                    "user.js",
                    entity_config_path(node_entity),
                ],
                cwd=example_entities_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            node_procs[node_name] = proc
            node_outputs[node_name] = start_output_reader(proc, node_entity["name"])
            wait_for_output(
                node_outputs[node_name],
                [
                    "current parameters:",
                    f"{node_entity['name']}:{node_name} prompt>",
                ],
                timeout=5,
            )
            print(f"{node_name} is ready")

        time.sleep(5)

        auth_db_size_before_delegation = snapshot_databases("before_delegation")
        delegation_start = time.perf_counter()

        run_delegations(delegation_records, node_procs, node_outputs, delegations_path)
        delegation_end = time.perf_counter()
        auth_db_size_after_delegation = snapshot_databases("after_delegation")

        experiment_start2 = time.perf_counter()
        # Test assigned accesses after delegation
        before_revoke_test_path = output_prefix.with_name(f"{output_prefix.stem}_access_before_revoke_test.json")
        before_revoke_results = test_access_by_init_comm(
            access_map=access_before_revoke,
            node_procs=node_procs,
            node_outputs=node_outputs,
            output_path=before_revoke_test_path,
            phase_name="before_revoke",
            group_auths=group_auths,
            entity_names=entity_names,
            expected_access=access_before_revoke,
        )

        experiment_end2 = time.perf_counter()
        auth_db_size_after_access_checking = snapshot_databases("after_access_checking1")

        # Stop nodes
        for node_name, proc in node_procs.items():
            print(f"Stopping {node_name}")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        node_procs = {}
        node_outputs = {}
        # Reopen the nodes
        for i in range(1, args.nodes + 1):
            node_name = f"Node{i}"
            node_entity = entities_by_group[node_name]
            proc = subprocess.Popen(
                [
                    "node",
                    "user.js",
                    entity_config_path(node_entity),
                ],
                cwd=example_entities_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            node_procs[node_name] = proc
            node_outputs[node_name] = start_output_reader(proc, node_entity["name"])
            wait_for_output(
                node_outputs[node_name],
                [
                    "current parameters:",
                    f"{node_entity['name']}:{node_name} prompt>",
                ],
                timeout=5,
            )
            print(f"{node_name} is ready")
        sys.stdout.flush()

        auth_db_size_before_revocation = snapshot_databases("before_revocation")
        revocation_count = 0
        experiment_start3 = time.perf_counter()
        # Revoke descendants before ancestors, whose revocation cascades.
        for privilege in reversed(graph["privilegeList"]):
            if privilege["privilegeType"] != "DelegationRevoke":
                continue

            src = privilege["privilegedGroup"]
            dst = privilege["subjectGroup"]
            resource = privilege["objectGroup"]

            cmd = f"revoke {dst} {resource}\n"

            proc = node_procs[src]
            output_q = node_outputs[src]

            print(f"\nExecuting on {src} (Auth{group_auths[src]}), "
                  f"resource on Auth{group_auths[resource]}: {cmd.strip()}")
            drain_output(output_q)

            proc.stdin.write(cmd)
            proc.stdin.flush()

            wait_for_output(
                output_q,
                ["action: 'DelegationRevoke'"],
                failure_patterns=["Handler: Error", "received an Auth alert!"],
                timeout=10,
            )
            revocation_count += 1
            sys.stdout.flush()
            # time.sleep(0.5)
        experiment_end3 = time.perf_counter()
        auth_db_size_after_revocation = snapshot_databases("after_revocation")

        experiment_start4 = time.perf_counter()
        # Test assigned accesses after revocation
        after_revoke_test_path =  output_prefix.with_name(f"{output_prefix.stem}_access_after_revoke_test.json")
        after_revoke_results = test_access_by_init_comm(
            access_map=access_before_revoke,
            node_procs=node_procs,
            node_outputs=node_outputs,
            output_path=after_revoke_test_path,
            phase_name="after_revoke",
            group_auths=group_auths,
            entity_names=entity_names,
            expected_access=access_after_revoke,
        )
        experiment_end4 = time.perf_counter()
        auth_db_size_after_access_checking2 = snapshot_databases("after_access_checking2")

        delegation_latency_ms = (delegation_end - delegation_start) * 1000
        before_revoke_access_latency_ms = (experiment_end2 - experiment_start2) * 1000
        revocation_latency_ms = (experiment_end3 - experiment_start3) * 1000
        after_revoke_access_latency_ms = (experiment_end4 - experiment_start4) * 1000
        summary = {
            "auth_count": args.auths,
            "auth_ids": list(auth_db_paths),
            "entity_auth_assignments": graph["assignments"],
            "auth_db_size_bytes_by_auth": db_snapshots,
            "auth_db_size_bytes": {
                "before_delegation": auth_db_size_before_delegation,
                "after_delegation": auth_db_size_after_delegation,
                "after_access_checking1": auth_db_size_after_access_checking,
                "before_revocation": auth_db_size_before_revocation,
                "after_revocation": auth_db_size_after_revocation,
                "after_access_checking2": auth_db_size_after_access_checking2,
            },
            "initial_access_count": len(initial_access),
            "delegation_count": sum(record["success"] is True for record in delegation_records),
            "revocation_count": revocation_count,
            "total_revoked_access_count": count_revoked_accesses(resource_edges, graph["privilegeList"]),
            "delegation_latency_ms": delegation_latency_ms,
            "before_revoke_access_latency_ms": before_revoke_access_latency_ms,
            "revocation_latency_ms": revocation_latency_ms,
            "after_revoke_access_latency_ms": after_revoke_access_latency_ms,
            "access_check_before": {
                "success": sum(1 for r in before_revoke_results if r["success"]),
                "total": len(before_revoke_results),
                "mismatches": sum(not r["matches_expected"] for r in before_revoke_results),
            },
            "access_check_after": {
                "success": sum(1 for r in after_revoke_results if r["success"]),
                "expected_success": sum(
                    len(resources)
                    for resources in access_after_revoke.values()
                ),
                "total_access_checking": len(after_revoke_results),
                "mismatches": sum(not r["matches_expected"] for r in after_revoke_results),
            },
        }

        summary_path = output_prefix.with_name(f"{output_prefix.stem}_latency.json")
        summary_path.write_text(json.dumps(summary, indent=4))
        print(f"Wrote latency in {summary_path}")
        if any(not r["matches_expected"] or (r["error"] and r["error"].startswith("TIMEOUT:"))
               for r in before_revoke_results + after_revoke_results):
            raise RuntimeError("Access verification failed; see the per-pair results and latency summary")

    finally:
        # Stopping Resources
        for i, proc in enumerate(resource_procs, start=1):
            print(f"Stopping Resource{i}")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        time.sleep(3)
        # Stopping Nodes
        for node_name, proc in node_procs.items():
            print(f"Stopping {node_name}")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        time.sleep(3)
        # Stopping Auth
        for current_auth_id, auth_proc in auth_procs.items():
            print(f"Stopping Auth{current_auth_id}")
            auth_proc.terminate()
            try:
                auth_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                auth_proc.kill()
                auth_proc.wait()


if __name__ == "__main__":
    main()
