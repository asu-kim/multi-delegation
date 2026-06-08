import argparse
import json
import random
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
    }


def generate_dag(nodes, edge_prob=0.35, seed=None, force_chain=False):
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


def select_resource_dag_edges(overall_dag, probability=0.5, seed=None):
    rng = random.Random(seed)
    selected_edges = []

    for src, dst in overall_dag.edges():
        if rng.random() < probability:
            selected_edges.append((src, dst))

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

    privilege_list = []

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

    return {
               "authList": [make_auth(auth_id)],
               "authTrusts": [],
               "assignments": assignments,
               "entityList": entity_list,
               "privilegeList": privilege_list,
           }, overall_dag, resource_edges


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="random_dag.graph", help="Output .graph file path")
    parser.add_argument("--nodes", type=int, default=7, help="Number of nodes: Node1..NodeN")
    parser.add_argument("--resources", type=int, default=3, help="Number of resources (Resource1..ResourceN)")
    parser.add_argument("--auth-id", type=int, default=101, help="Single Auth ID used for all assignments")
    parser.add_argument("--edge-prob", type=float, default=0.20,
                        help="Probability of extra DAG edges. If this value is 1, then it will return all possible edges. ex) 3 nodes -> 3! edges")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    parser.add_argument("--validity", default="1*day", help="Privilege validity")
    parser.add_argument("--print-detail", default=True, help="Print Resource-specific DAG delegation edge")
    args = parser.parse_args()

    if args.nodes < 1:
        raise ValueError("--nodes must be >= 1")

    if args.resources < 1:
        raise ValueError("--resources must be >= 1")

    if not 0 <= args.edge_prob <= 1:
        raise ValueError("--edge-prob must be between 0 and 1")

    graph, overall_dag, resource_edges = build_graph(
        node_count=args.nodes,
        resource_count=args.resources,
        auth_id=args.auth_id,
        edge_prob=args.edge_prob,
        seed=args.seed,
        validity=args.validity,
        print_detail=args.print_detail,
    )

    output_path = Path(args.output)
    output_path.write_text(json.dumps(graph, indent="\t"))
    print(f"\nWrote {output_path}")


if __name__ == "__main__":
    main()