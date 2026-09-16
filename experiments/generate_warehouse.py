#!/usr/bin/env python3
"""
generate_warehouse.py

Generate an SST/IoTAuth warehouse experiment from logistics_dataset.csv.

Outputs:
  - warehouse.graph
  - warehouse_layout.json
  - workload.json

Design:
  * Dataset item -> SST resource
  * Zone A/B/C/D -> Auth 101/102/103/104
  * One Supervisor entity per zone
  * N Robot entities per zone
  * Supervisor initially owns Access to every resource in its zone
  * Runtime workload selects a resource and the nearest robot; delegation itself is
    executed later by run_warehouse_experiment.py

Example:
    python generate_warehouse.py \
        --dataset logistics_dataset.csv \
        --output-dir generated \
        --robots-per-zone 5 \
        --requests 100 \
        --seed 7

For a smaller first run:
    python generate_warehouse.py \
        --dataset logistics_dataset.csv \
        --output-dir generated_small \
        --robots-per-zone 3 \
        --num-resources 40 \
        --requests 20 \
        --seed 7
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_INFO = {
    "cryptoSpec": "AES-128-CBC:SHA256",
    "absValidity": "1*day",
    "relValidity": "1*hour",
}

ZONES = ("A", "B", "C", "D")

ZONE_AUTH_IDS = {
    "A": 101,
    "B": 102,
    "C": 103,
    "D": 104,
}

ZONE_NETS = {
    "A": "net1",
    "B": "net2",
    "C": "net3",
    "D": "net4",
}

WAREHOUSE_BOUNDS = (0.0, 100.0, 0.0, 100.0)

# 100 m x 100 m synthetic warehouse split into four zones.
ZONE_BOUNDS = {
    "A": (0.0, 50.0, 50.0, 100.0),
    "B": (50.0, 100.0, 50.0, 100.0),
    "C": (0.0, 50.0, 0.0, 50.0),
    "D": (50.0, 100.0, 0.0, 50.0),
}

# Auth port bases follow the same 1000-step convention as the existing
# multi-Auth graph examples: 21900, 22900, 23900, 24900.
AUTH_TCP_PORT_BASE = {
    "A": 21900,
    "B": 22900,
    "C": 23900,
    "D": 24900,
}

# Keep resource ports well away from Auth ports.
RESOURCE_PORT_BASE = {
    "A": 31000,
    "B": 35000,
    "C": 39000,
    "D": 43000,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a multi-Auth SST warehouse experiment graph and workload."
    )
    parser.add_argument("--dataset", required=True, help="Path to warehouse_dataset.csv")
    parser.add_argument("--output-dir", default="generated", help="Output directory")
    parser.add_argument(
        "--robots-per-zone",
        type=int,
        default=5,
        help="Number of robot entities managed by each zone Supervisor",
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=100,
        help="Number of workload requests to generate",
    )
    parser.add_argument(
        "--num-resources",
        type=int,
        default=0,
        help="Number of dataset items to register. 0 means all items.",
    )
    parser.add_argument(
        "--resource-selection",
        choices=("daily_demand", "uniform"),
        default="daily_demand",
        help="How workload requests choose resources",
    )
    parser.add_argument(
        "--robot-motion-radius",
        type=float,
        default=10.0,
        help=(
            "Maximum meters a robot moves between requests. "
            "Request 0 uses a random position; later positions use bounded random motion."
        ),
    )
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--validity",
        default="1*day",
        help="Initial Supervisor->resource Access validity stored in .graph",
    )
    return parser.parse_args()


def write_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent="\t"), encoding="utf-8")


def safe_token(value: Any) -> str:
    """
    Convert an arbitrary dataset identifier to a conservative SST group/name token.
    """
    s = str(value).strip()
    s = re.sub(r"[^A-Za-z0-9_]+", "_", s)
    return s.strip("_") or "unknown"


def resource_group(item: dict[str, Any]) -> str:
    return f"Item_{safe_token(item['item_id'])}"


def resource_entity_name(item: dict[str, Any]) -> str:
    net = ZONE_NETS[item["zone"]]
    return f"{net}.item_{safe_token(item['item_id']).lower()}"


def supervisor_group(zone: str) -> str:
    return f"Supervisor{zone}"


def supervisor_entity_name(zone: str) -> str:
    net = ZONE_NETS[zone]
    return f"{net}.supervisor"


def robot_group(zone: str, index: int) -> str:
    return f"Robot{zone}{index}"


def robot_entity_name(zone: str, index: int) -> str:
    net = ZONE_NETS[zone]
    return f"{net}.robot{zone}{index}"


def next_auth_id(zone: str) -> int:
    idx = ZONES.index(zone)
    next_zone = ZONES[(idx + 1) % len(ZONES)]
    return ZONE_AUTH_IDS[next_zone]


def load_items(
    dataset_path: Path,
    num_resources: int,
    seed: int,
) -> list[dict[str, Any]]:
    df = pd.read_csv(dataset_path)

    required_columns = {
        "item_id",
        "storage_location_id",
        "zone",
        "daily_demand",
    }
    missing = sorted(required_columns - set(df.columns))
    if missing:
        raise ValueError(
            "Dataset is missing required column(s): " + ", ".join(missing)
        )

    df = df.copy()
    df["zone"] = df["zone"].astype(str).str.strip().str.upper()
    df = df[df["zone"].isin(ZONES)]

    if df.empty:
        raise ValueError("No rows with zones A/B/C/D were found.")

    # To avoid a subset being dominated by the first CSV rows, sample reproducibly
    # when --num-resources is smaller than the dataset.
    if 0 < num_resources < len(df):
        df = df.sample(n=num_resources, random_state=seed)
        df = df.sort_values(["zone", "item_id"]).reset_index(drop=True)

    items: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        items.append(
            {
                "item_id": str(row["item_id"]),
                "zone": str(row["zone"]),
                "storage_location_id": str(row["storage_location_id"]),
                "daily_demand": float(row["daily_demand"])
                if pd.notna(row["daily_demand"])
                else 0.0,
                "category": str(row["category"]) if "category" in df.columns else "",
                "picking_time_seconds": float(row["picking_time_seconds"])
                if "picking_time_seconds" in df.columns
                and pd.notna(row["picking_time_seconds"])
                else None,
            }
        )

    return items


def make_auth(zone: str) -> dict[str, Any]:
    tcp_port = AUTH_TCP_PORT_BASE[zone]
    return {
        "id": ZONE_AUTH_IDS[zone],
        "entityHost": "localhost",
        "authHost": "localhost",
        "tcpPort": tcp_port,
        "udpPort": tcp_port + 2,
        "authPort": tcp_port + 1,
        "callbackPort": tcp_port + 3,
        "dbProtectionMethod": 1,
        "backupEnabled": False,
        "contextualCallbackEnabled": True,
    }


def make_auth_trusts() -> list[dict[str, int]]:
    """
    Match the existing graph generator's fully connected trust-pair convention.
    This does not force cross-zone delegation; it only declares trusted Auth pairs.
    """
    auth_ids = [ZONE_AUTH_IDS[z] for z in ZONES]
    trusts = []
    for i, id1 in enumerate(auth_ids):
        for id2 in auth_ids[i + 1 :]:
            trusts.append({"id1": id1, "id2": id2})
    return trusts


def make_node_entity(
    group: str,
    name: str,
    net: str,
    zone: str,
) -> dict[str, Any]:
    return {
        "group": group,
        "name": name,
        "distProtocol": "TCP",
        "usePermanentDistKey": True,
        "distKeyValidityPeriod": "365*day",
        "maxSessionKeysPerRequest": 5,
        "netName": net,
        "credentialPrefix": f"{net.capitalize()}.{group}",
        "distributionCryptoSpec": {
            "cipher": "AES-128-CBC",
            "mac": "SHA256",
        },
        "sessionCryptoSpec": {
            "cipher": "AES-128-CBC",
            "mac": "SHA256",
        },
        "backupToAuthIds": [next_auth_id(zone)],
    }


def make_resource_entity(
    item: dict[str, Any],
    port: int,
) -> dict[str, Any]:
    zone = item["zone"]
    net = ZONE_NETS[zone]
    group = resource_group(item)

    return {
        "group": group,
        "name": resource_entity_name(item),
        "port": port,
        "distProtocol": "TCP",
        "usePermanentDistKey": False,
        "distKeyValidityPeriod": "365*day",
        "maxSessionKeysPerRequest": 30,
        "netName": net,
        "credentialPrefix": f"{net.capitalize()}.{group}",
        "distributionCryptoSpec": {
            "cipher": "AES-128-CBC",
            "mac": "SHA256",
        },
        "sessionCryptoSpec": {
            "cipher": "AES-128-CBC",
            "mac": "SHA256",
        },
        "host": "localhost",
        "backupToAuthIds": [next_auth_id(zone)],
    }


def make_access_privilege(robot: str, resource: str, validity: str) -> dict[str, Any]:
    return {
        "privilegeType": "DelegationGrant",
        "privilegedGroup": "Supervisors",
        "subject": robot,
        "object": resource,
        "validity": validity,
        "info": dict(DEFAULT_INFO),
    }


def make_revoke_privilege(robot: str, resource: str) -> dict[str, Any]:
    return {
        "privilegeType": "DelegationRevoke",
        "privilegedGroup": "Supervisors",
        "subject": robot,
        "object": resource,
    }


def generate_supervisors() -> list[dict[str, str]]:
    return [
        {
            "zone": zone,
            "group": "Supervisors",
            "name": supervisor_entity_name(zone),
        }
        for zone in ZONES
    ]


def generate_robots(robots_per_zone: int) -> list[dict[str, Any]]:
    if robots_per_zone < 1:
        raise ValueError("--robots-per-zone must be >= 1")

    robots: list[dict[str, Any]] = []
    for zone in ZONES:
        for i in range(1, robots_per_zone + 1):
            robots.append(
                {
                    "zone": zone,
                    "index": i,
                    "group": robot_group(zone, i),
                    "name": robot_entity_name(zone, i),
                }
            )
    return robots


def build_assignments(
    items: list[dict[str, Any]],
    supervisors: list[dict[str, str]],
    robots: list[dict[str, Any]],
) -> dict[str, int]:
    assignments: dict[str, int] = {}

    for supervisor in supervisors:
        assignments[supervisor["name"]] = ZONE_AUTH_IDS[supervisor["zone"]]

    for robot in robots:
        assignments[robot["name"]] = ZONE_AUTH_IDS[robot["zone"]]

    for item in items:
        assignments[resource_entity_name(item)] = ZONE_AUTH_IDS[item["zone"]]

    return assignments


def assign_resource_ports(
    items: list[dict[str, Any]],
) -> dict[str, int]:
    """
    Give each resource a unique port, with a separate port range for each zone.
    """
    per_zone_counter = defaultdict(int)
    ports: dict[str, int] = {}

    for item in items:
        zone = item["zone"]
        port = RESOURCE_PORT_BASE[zone] + per_zone_counter[zone]
        if port > 65535:
            raise ValueError(f"Resource port exceeded 65535 in zone {zone}")
        ports[resource_group(item)] = port
        per_zone_counter[zone] += 1

    return ports


def build_graph(
    items: list[dict[str, Any]],
    supervisors: list[dict[str, str]],
    robots: list[dict[str, Any]],
    validity: str,
) -> dict[str, Any]:
    entity_list: list[dict[str, Any]] = []

    # Add supervisors
    for supervisor in supervisors:
        entity_list.append(
            make_node_entity(
                group="Supervisors",
                name=supervisor["name"],
                net=ZONE_NETS[supervisor["zone"]],
                zone=supervisor["zone"],
            )
        )

    # Add robots
    for robot in robots:
        entity_list.append(
            make_node_entity(
                group=robot["group"],
                name=robot["name"],
                net=ZONE_NETS[robot["zone"]],
                zone=robot["zone"],
            )
        )

    # Add resources
    ports = assign_resource_ports(items)

    for item in items:
        entity_list.append(
            make_resource_entity(
                item=item,
                port=ports[resource_group(item)],
            )
        )

    # Group robots by zone
    robots_by_zone: dict[str, list[dict[str, Any]]] = {
        zone: [] for zone in ZONES
    }

    for robot in robots:
        robots_by_zone[robot["zone"]].append(robot)

    privilege_list: list[dict[str, Any]] = []

    # For every resource, allow its zone Supervisor to delegate/revoke
    # the resource access to any robot managed in the same zone.
    for item in items:
        resource = resource_group(item)

        for robot in robots:
            robot_name = robot["group"]

            # Supervisor can delegate this resource to this robot
            privilege_list.append(
                make_access_privilege(robot=robot_name, resource=resource, validity=validity))

            # Supervisor can revoke this delegated access
            privilege_list.append(make_revoke_privilege(robot=robot_name, resource=resource))

    return {
        "authList": [make_auth(zone) for zone in ZONES],
        "authTrusts": make_auth_trusts(),
        "assignments": build_assignments(items, supervisors, robots,),
        "entityList": entity_list,
        "filesharingLists": [],
        "privilegeList": privilege_list,
    }


def generate_storage_coordinates(
    items: list[dict[str, Any]],
    seed: int,
) -> dict[str, Any]:
    """
    Dataset storage_location_id is not a physical coordinate, so assign a synthetic
    coordinate to each (zone, storage_location_id). All items in the same zone/location
    share the same coordinate.
    """
    rng = random.Random(seed)

    unique_locations = sorted(
        {
            (item["zone"], item["storage_location_id"])
            for item in items
        }
    )

    storage_locations: dict[str, Any] = {}
    for zone, location_id in unique_locations:
        xmin, xmax, ymin, ymax = ZONE_BOUNDS[zone]
        key = f"{zone}:{location_id}"
        storage_locations[key] = {
            "zone": zone,
            "storage_location_id": location_id,
            "x": round(rng.uniform(xmin, xmax), 4),
            "y": round(rng.uniform(ymin, ymax), 4),
        }

    item_locations = {}
    for item in items:
        key = f"{item['zone']}:{item['storage_location_id']}"
        item_locations[resource_group(item)] = {
            "zone": item["zone"],
            "storage_location_id": item["storage_location_id"],
            "location_key": key,
            "x": storage_locations[key]["x"],
            "y": storage_locations[key]["y"],
        }

    return {
        "warehouse_width_m": 100.0,
        "warehouse_height_m": 100.0,
        "zone_bounds": {
            zone: {
                "xmin": ZONE_BOUNDS[zone][0],
                "xmax": ZONE_BOUNDS[zone][1],
                "ymin": ZONE_BOUNDS[zone][2],
                "ymax": ZONE_BOUNDS[zone][3],
            }
            for zone in ZONES
        },
        "storage_locations": storage_locations,
        "item_locations": item_locations,
    }


def random_robot_positions(robots: list[dict[str, Any]], rng: random.Random,) -> dict[str, dict[str, float]]:
    positions = {}

    xmin, xmax, ymin, ymax = WAREHOUSE_BOUNDS

    for robot in robots:
        group = robot["group"]

        positions[group] = {
            "x": rng.uniform(xmin, xmax),
            "y": rng.uniform(ymin, ymax),
        }

    return positions


def move_robot_positions(
        robots: list[dict[str, Any]],
        previous: dict[str, dict[str, float]],
        rng: random.Random,
        motion_radius: float,
) -> dict[str, dict[str, float]]:
    positions = {}
    for robot in robots:
        group = robot["group"]

        xmin, xmax, ymin, ymax = WAREHOUSE_BOUNDS
        angle = rng.uniform(0.0, 2.0 * math.pi)
        radius = rng.uniform(0.0, motion_radius)

        x = previous[group]["x"] + radius * math.cos(angle)
        y = previous[group]["y"] + radius * math.sin(angle)

        # Keep the robot inside its Supervisor's zone.
        x = min(max(x, xmin), xmax)
        y = min(max(y, ymin), ymax)

        positions[group] = {"x": x, "y": y}

    return positions


def euclidean_distance(p1: dict[str, float], p2: dict[str, float],) -> float:
    return math.hypot(p1["x"] - p2["x"], p1["y"] - p2["y"])


def nearest_robot(
    item: dict[str, Any],
    robots: list[dict[str, Any]],
    positions: dict[str, dict[str, float]],
    layout: dict[str, Any],
) -> tuple[dict[str, Any], float]:

    resource = resource_group(item)
    resource_pos = layout["item_locations"][resource]

    best_robot = min(
        robots,
        key=lambda robot: euclidean_distance(
            positions[robot["group"]],
            resource_pos,
        ),
    )

    distance = euclidean_distance(
        positions[best_robot["group"]],
        resource_pos,
    )

    return best_robot, distance


def select_item(
    rng: random.Random,
    items: list[dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    if mode == "uniform":
        return rng.choice(items)

    else:
        weights = [
            max(float(item["daily_demand"]), 0.0)
            for item in items
        ]

    if sum(weights) <= 0:
        return rng.choice(items)

    return rng.choices(items, weights=weights, k=1)[0]


def generate_workload(
    items: list[dict[str, Any]],
    robots: list[dict[str, Any]],
    layout: dict[str, Any],
    num_requests: int,
    resource_selection: str,
    robot_motion_radius: float,
    seed: int,
) -> dict[str, Any]:

    if num_requests < 1:
        raise ValueError("--requests must be >= 1")

    rng = random.Random(seed + 10_000)
    positions = random_robot_positions(robots, rng)

    requests = []

    for request_id in range(num_requests):

        # Every request gets a new random warehouse-wide
        # position for every robot.
        if request_id > 0:
            positions = move_robot_positions(
                robots,
                previous=positions,
                rng=rng,
                motion_radius=robot_motion_radius,
            )

        # Select a resource based on demand.
        item = select_item(
            rng,
            items,
            resource_selection,
        )

        # Search ALL robots, regardless of Auth/zone.
        robot, distance_m = nearest_robot(
            item=item,
            robots=robots,
            positions=positions,
            layout=layout,
        )

        resource = resource_group(item)
        resource_pos = layout["item_locations"][resource]

        requests.append(
            {
                "request_id": request_id,

                # Resource's administrative zone
                "resource_zone": item["zone"],

                # The Supervisor associated with the resource's zone
                "supervisor": supervisor_entity_name(item["zone"]),

                "supervisor_group": "Supervisor",

                "resource": resource,
                "item_id": item["item_id"],
                "storage_location_id": item["storage_location_id"],

                "resource_position": {
                    "x": resource_pos["x"],
                    "y": resource_pos["y"],
                },

                # All robots, not only robots belonging to resource zone
                "robot_positions": {
                    robot_info["group"]: {
                        "x": round(
                            positions[robot_info["group"]]["x"], 4
                        ),
                        "y": round(
                            positions[robot_info["group"]]["y"], 4
                        ),
                    }
                    for robot_info in robots
                },

                "selected_robot": robot["group"],
                "selected_robot_home_zone": robot["zone"],
                "selected_robot_distance_m": round(distance_m, 4,),

                "cross_auth": ( robot["zone"] != item["zone"]),

            }
        )

    return {
        "metadata": {
            "seed": seed,
            "num_requests": num_requests,
            "resource_selection": resource_selection,
            "robot_motion_radius_m": robot_motion_radius,
        },
        "requests": requests,
    }


def summarize(
    items: list[dict[str, Any]],
    supervisors: list[dict[str, str]],
    robots: list[dict[str, Any]],
    layout: dict[str, Any],
    workload: dict[str, Any],
) -> None:
    item_counts = defaultdict(int)
    robot_counts = defaultdict(int)

    for item in items:
        item_counts[item["zone"]] += 1
    for robot in robots:
        robot_counts[robot["zone"]] += 1

    print("\nGenerated warehouse experiment")
    print("--------------------------------")
    print(f"Resources: {len(items)}")
    print(f"Supervisors:  {len(supervisors)}")
    print(f"Robots:    {len(robots)}")
    print(f"Requests:  {len(workload['requests'])}")
    print(f"Storage locations: {len(layout['storage_locations'])}")

    for zone in ZONES:
        print(
            f"Zone {zone}: Auth {ZONE_AUTH_IDS[zone]}, "
            f"{item_counts[zone]} resources, "
            f"{robot_counts[zone]} robots"
        )


def main() -> None:
    args = parse_args()

    dataset_path = Path(args.dataset)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    items = load_items(
        dataset_path=dataset_path,
        num_resources=args.num_resources,
        seed=args.seed,
    )
    supervisors = generate_supervisors()
    robots = generate_robots(args.robots_per_zone)

    graph = build_graph(
        items=items,
        supervisors=supervisors,
        robots=robots,
        validity=args.validity,
    )
    layout = generate_storage_coordinates(items, seed=args.seed)
    workload = generate_workload(
        items=items,
        robots=robots,
        layout=layout,
        num_requests=args.requests,
        resource_selection=args.resource_selection,
        robot_motion_radius=args.robot_motion_radius,
        seed=args.seed,
    )

    graph_path = output_dir / "warehouse.graph"
    layout_path = output_dir / "warehouse_layout.json"
    workload_path = output_dir / "workload.json"

    write_json(graph, graph_path)
    write_json(layout, layout_path)
    write_json(workload, workload_path)

    summarize(items, supervisors, robots, layout, workload)

    print("\nWrote:")
    print(f"  {graph_path}")
    print(f"  {layout_path}")
    print(f"  {workload_path}")


if __name__ == "__main__":
    main()
