#!/usr/bin/env python3
"""
generate_warehouse_realdata.py

Generate an SST/IoTAuth warehouse experiment from the real-world warehouse CSVs:
  - Product.csv
  - Storage_Location.csv
  - Picking_Wave.csv
  - Support_Points_Navigation.csv (optional but recommended)

Outputs:
  - warehouse.graph
  - warehouse.policy.json
  - warehouse_layout.json
  - workload.json

Key design choices:
  * One SST resource represents one observed (product reference, storage location) pair.
  * Real storage coordinates come from Storage_Location.csv; no synthetic item coordinates.
  * Four logical Auth zones A/B/C/D are derived from the real warehouse coordinate bounds.
  * Robot home zones remain logical ownership zones, but robot positions may be anywhere in
    the warehouse. If navigation support points are supplied, robot positions are sampled
    from those real navigation points.
  * Workload rows come from actual Picking_Wave.csv records.
  * The Manager/Supervisor that performs delegation is the selected robot's home-zone manager.
    Therefore a request is cross-Auth when selected_robot_home_zone != resource_zone.

Example:
  python generate_warehouse_realdata.py \
      --product Product.csv \
      --storage-locations Storage_Location.csv \
      --picking-wave Picking_Wave.csv \
      --navigation-points Support_Points_Navigation.csv \
      --output-dir generated \
      --robots-per-zone 5 \
      --requests 100 \
      --items-per-position 3 \
      --num-resources 200 \
      --seed 7
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

ZONES = ("A", "B", "C", "D")
ZONE_AUTH_IDS = {"A": 101, "B": 102, "C": 103, "D": 104}
ZONE_NETS = {"A": "net1", "B": "net2", "C": "net3", "D": "net4"}
AUTH_TCP_PORT_BASE = {"A": 21900, "B": 22900, "C": 23900, "D": 24900}
RESOURCE_PORT_BASE = {"A": 31000, "B": 35000, "C": 39000, "D": 43000}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate a multi-Auth SST warehouse experiment from real warehouse CSVs."
    )
    p.add_argument("--product", help="Path to Product.csv", default="dataset/Product.csv")
    p.add_argument("--storage-locations", help="Path to Storage_Location.csv", default="dataset/Storage_Location.csv")
    p.add_argument("--picking-wave", help="Path to Picking_Wave.csv", default="dataset/Picking_Wave.csv")
    p.add_argument(
        "--navigation-points",
        default=None,
        help="Path to Support_Points_Navigation.csv. If omitted, robot positions are uniform within real bounds."
    )
    p.add_argument("--output-dir", default="generated", help="Output directory")
    p.add_argument("--robots-per-zone", type=int, default=5)
    p.add_argument(
        "--requests",
        type=int,
        default=100,
        help="Number of robot-position batches. Total request count = requests * items-per-position.",
    )
    p.add_argument("--items-per-position", type=int, default=3)
    p.add_argument(
        "--num-resources",
        type=int,
        default=0,
        help="Maximum number of observed reference/location resources to register. 0 = all usable resources.",
    )
    p.add_argument(
        "--workload-order",
        choices=("random", "chronological"),
        default="random",
        help="Select real picking rows randomly (seeded) or in CSV order.",
    )
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--validity", default="1*day")
    return p.parse_args()


def write_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def read_semicolon_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, sep=";", engine="python")


def strip_object_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].astype(str).str.strip()
    return df


def safe_token(value: Any) -> str:
    s = str(value).strip()
    s = re.sub(r"[^A-Za-z0-9_]+", "_", s)
    return s.strip("_") or "unknown"


def resource_group(item: dict[str, Any]) -> str:
    return f"Item_{safe_token(item['reference'])}_{safe_token(item['storage_location_id'])}"


def resource_entity_name(item: dict[str, Any]) -> str:
    net = ZONE_NETS[item["zone"]]
    return (
        f"{net}.item_{safe_token(item['reference']).lower()}_"
        f"{safe_token(item['storage_location_id']).lower()}"
    )


def supervisor_entity_name(zone: str) -> str:
    return f"{ZONE_NETS[zone]}.supervisor"


def robot_group(zone: str, index: int) -> str:
    return f"Robot{zone}{index}"


def robot_entity_name(zone: str, index: int) -> str:
    return f"{ZONE_NETS[zone]}.robot{zone}{index}"


def next_auth_id(zone: str) -> int:
    idx = ZONES.index(zone)
    return ZONE_AUTH_IDS[ZONES[(idx + 1) % len(ZONES)]]


def compute_bounds(storage_df: pd.DataFrame) -> dict[str, float]:
    return {
        "xmin": float(storage_df["x"].min()),
        "xmax": float(storage_df["x"].max()),
        "ymin": float(storage_df["y"].min()),
        "ymax": float(storage_df["y"].max()),
    }


def coordinate_zone(x: float, y: float, bounds: dict[str, float]) -> str:
    """Split the real warehouse bounding box into four logical Auth zones."""
    xmid = (bounds["xmin"] + bounds["xmax"]) / 2.0
    ymid = (bounds["ymin"] + bounds["ymax"]) / 2.0
    if x < xmid and y >= ymid:
        return "A"
    if x >= xmid and y >= ymid:
        return "B"
    if x < xmid and y < ymid:
        return "C"
    return "D"


def zone_bounds(bounds: dict[str, float]) -> dict[str, dict[str, float]]:
    xmid = (bounds["xmin"] + bounds["xmax"]) / 2.0
    ymid = (bounds["ymin"] + bounds["ymax"]) / 2.0
    return {
        "A": {"xmin": bounds["xmin"], "xmax": xmid, "ymin": ymid, "ymax": bounds["ymax"]},
        "B": {"xmin": xmid, "xmax": bounds["xmax"], "ymin": ymid, "ymax": bounds["ymax"]},
        "C": {"xmin": bounds["xmin"], "xmax": xmid, "ymin": bounds["ymin"], "ymax": ymid},
        "D": {"xmin": xmid, "xmax": bounds["xmax"], "ymin": bounds["ymin"], "ymax": ymid},
    }


def load_real_data(
    product_path: Path,
    storage_path: Path,
    picking_path: Path,
    num_resources: int,
    seed: int,
) -> tuple[list[dict[str, Any]], pd.DataFrame, dict[str, float], dict[str, Any]]:
    product = strip_object_columns(read_semicolon_csv(product_path))
    storage = strip_object_columns(pd.read_csv(storage_path))
    picking = strip_object_columns(read_semicolon_csv(picking_path))

    required_product = {"Reference", "ABCCOD", "Sector"}
    required_storage = {"originalLocation", "x", "y", "z"}
    required_picking = {
        "waveNumber",
        "reference",
        "Size (US)",
        "quantityToPick (units)",
        "locations",
        "operator",
    }
    for label, df, required in (
        ("Product.csv", product, required_product),
        ("Storage_Location.csv", storage, required_storage),
        ("Picking_Wave.csv", picking, required_picking),
    ):
        missing = sorted(required - set(df.columns))
        if missing:
            raise ValueError(f"{label} missing required columns: {', '.join(missing)}")

    storage = storage.drop_duplicates("originalLocation").copy()
    for col in ("x", "y", "z"):
        storage[col] = pd.to_numeric(storage[col], errors="coerce")
    storage = storage.dropna(subset=["x", "y", "z"])

    bounds = compute_bounds(storage)
    storage["resource_zone"] = storage.apply(
        lambda r: coordinate_zone(float(r["x"]), float(r["y"]), bounds), axis=1
    )

    product_meta = product.drop_duplicates("Reference").set_index("Reference")
    storage_meta = storage.set_index("originalLocation")

    # Keep only rows for which both the product and its physical location are present.
    picking = picking[
        picking["reference"].isin(product_meta.index)
        & picking["locations"].isin(storage_meta.index)
    ].copy()
    if picking.empty:
        raise ValueError("No Picking_Wave rows match both Product and Storage_Location data")

    picking["quantityToPick (units)"] = pd.to_numeric(
        picking["quantityToPick (units)"], errors="coerce"
    ).fillna(0).astype(int)
    picking["waveNumber"] = pd.to_numeric(picking["waveNumber"], errors="coerce").astype("Int64")

    # An SST resource is a concrete product-at-location pair so that location and Auth zone
    # stay stable for the lifetime of the resource entity.
    pairs = picking[["reference", "locations"]].drop_duplicates().copy()
    pair_frequency = (
        picking.groupby(["reference", "locations"], dropna=False)
        .size()
        .rename("pick_count")
        .reset_index()
    )
    pairs = pairs.merge(pair_frequency, on=["reference", "locations"], how="left")

    if 0 < num_resources < len(pairs):
        # Sample actual resource pairs reproducibly. Weight by observed picking frequency so
        # commonly used resources are proportionally more likely to remain in smaller tests.
        pairs = pairs.sample(
            n=num_resources,
            weights=pairs["pick_count"].clip(lower=1),
            random_state=seed,
            replace=False,
        )

    selected_pairs = set(zip(pairs["reference"], pairs["locations"]))
    picking = picking[
        picking.apply(lambda r: (r["reference"], r["locations"]) in selected_pairs, axis=1)
    ].reset_index(drop=True)

    items: list[dict[str, Any]] = []
    for row in pairs.itertuples(index=False):
        reference = str(row.reference)
        location = str(row.locations)
        s = storage_meta.loc[location]
        p = product_meta.loc[reference]
        items.append(
            {
                "reference": reference,
                "item_id": reference,
                "storage_location_id": location,
                "zone": str(s["resource_zone"]),
                "x": float(s["x"]),
                "y": float(s["y"]),
                "z": float(s["z"]),
                "abc_code": str(p["ABCCOD"]),
                "sector": str(p["Sector"]),
                "observed_pick_count": int(row.pick_count),
            }
        )

    diagnostics = {
        "product_rows": int(len(product)),
        "storage_location_rows": int(len(storage)),
        "usable_picking_rows": int(len(picking)),
        "usable_picking_waves": int(picking["waveNumber"].nunique()),
        "registered_resources": int(len(items)),
    }
    return items, picking, bounds, diagnostics


def load_navigation_points(path: Path | None, bounds: dict[str, float]) -> list[dict[str, Any]]:
    if path is None:
        return []
    df = strip_object_columns(read_semicolon_csv(path))
    if not {"points_specified", "labels"}.issubset(df.columns):
        raise ValueError("Navigation CSV must contain points_specified and labels")

    points = []
    for row in df.itertuples(index=False):
        try:
            xyz = ast.literal_eval(str(row.points_specified))
            x, y, z = float(xyz[0]), float(xyz[1]), float(xyz[2])
        except (ValueError, SyntaxError, TypeError, IndexError) as exc:
            raise ValueError(f"Invalid navigation point: {row.points_specified}") from exc
        points.append(
            {
                "label": str(row.labels),
                "x": x,
                "y": y,
                "z": z,
                "zone": coordinate_zone(x, y, bounds),
            }
        )
    return points


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
    auth_ids = [ZONE_AUTH_IDS[z] for z in ZONES]
    return [
        {"id1": id1, "id2": id2}
        for i, id1 in enumerate(auth_ids)
        for id2 in auth_ids[i + 1 :]
    ]


def make_node_entity(group: str, name: str, net: str, zone: str) -> dict[str, Any]:
    return {
        "group": group,
        "name": name,
        "distProtocol": "TCP",
        "usePermanentDistKey": True,
        "distKeyValidityPeriod": "365*day",
        "maxSessionKeysPerRequest": 5,
        "netName": net,
        "credentialPrefix": f"{net.capitalize()}.{group}",
        "distributionCryptoSpec": {"cipher": "AES-128-CBC", "mac": "SHA256"},
        "sessionCryptoSpec": {"cipher": "AES-128-CBC", "mac": "SHA256"},
        "backupToAuthIds": [next_auth_id(zone)],
    }


def make_resource_entity(item: dict[str, Any], port: int) -> dict[str, Any]:
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
        "distributionCryptoSpec": {"cipher": "AES-128-CBC", "mac": "SHA256"},
        "sessionCryptoSpec": {"cipher": "AES-128-CBC", "mac": "SHA256"},
        "host": "localhost",
        "backupToAuthIds": [next_auth_id(zone)],
    }


def make_access_privilege(robot: str, resource: str, validity: str) -> dict[str, Any]:
    return {
        "privilegeType": "DelegationGrant",
        "privilegedGroup": "Supervisors",
        "subjectGroup": robot,
        "objectGroup": resource,
        "validity": validity,
    }


def make_revoke_privilege(robot: str, resource: str) -> dict[str, Any]:
    return {
        "privilegeType": "DelegationRevoke",
        "privilegedGroup": "Supervisors",
        "subjectGroup": robot,
        "objectGroup": resource,
    }


def generate_supervisors() -> list[dict[str, str]]:
    return [
        {"zone": z, "group": "Supervisors", "name": supervisor_entity_name(z)} for z in ZONES
    ]


def generate_robots(robots_per_zone: int) -> list[dict[str, Any]]:
    if robots_per_zone < 1:
        raise ValueError("--robots-per-zone must be >= 1")
    return [
        {
            "zone": z,
            "index": i,
            "group": robot_group(z, i),
            "name": robot_entity_name(z, i),
        }
        for z in ZONES
        for i in range(1, robots_per_zone + 1)
    ]


def build_assignments(
    items: list[dict[str, Any]],
    supervisors: list[dict[str, str]],
    robots: list[dict[str, Any]],
) -> dict[str, int]:
    assignments: dict[str, int] = {}
    for s in supervisors:
        assignments[s["name"]] = ZONE_AUTH_IDS[s["zone"]]
    for r in robots:
        assignments[r["name"]] = ZONE_AUTH_IDS[r["zone"]]
    for item in items:
        assignments[resource_entity_name(item)] = ZONE_AUTH_IDS[item["zone"]]
    return assignments


def assign_resource_ports(items: list[dict[str, Any]]) -> dict[str, int]:
    counters = defaultdict(int)
    ports: dict[str, int] = {}
    for item in items:
        zone = item["zone"]
        port = RESOURCE_PORT_BASE[zone] + counters[zone]
        if port > 65535:
            raise ValueError(f"Resource port exceeded 65535 in zone {zone}")
        ports[resource_group(item)] = port
        counters[zone] += 1
    return ports


def build_graph(
    items: list[dict[str, Any]],
    supervisors: list[dict[str, str]],
    robots: list[dict[str, Any]],
    validity: str,
) -> dict[str, Any]:
    entity_list: list[dict[str, Any]] = []
    for s in supervisors:
        entity_list.append(make_node_entity("Supervisors", s["name"], ZONE_NETS[s["zone"]], s["zone"]))
    for r in robots:
        entity_list.append(make_node_entity(r["group"], r["name"], ZONE_NETS[r["zone"]], r["zone"]))

    ports = assign_resource_ports(items)
    for item in items:
        entity_list.append(make_resource_entity(item, ports[resource_group(item)]))

    privilege_list: list[dict[str, Any]] = []
    for item in items:
        resource = resource_group(item)
        for robot in robots:
            privilege_list.append(make_access_privilege(robot["group"], resource, validity))
            privilege_list.append(make_revoke_privilege(robot["group"], resource))

    return {
        "authList": [make_auth(z) for z in ZONES],
        "authTrusts": make_auth_trusts(),
        "assignments": build_assignments(items, supervisors, robots),
        "entityList": entity_list,
        "filesharingLists": [],
        "privilegeList": privilege_list,
    }


def build_supervisor_access_policies(graph: dict[str, Any]) -> list[dict[str, Any]]:
    supervisor_groups = sorted(
        {e["group"] for e in graph["entityList"] if str(e.get("group", "")).startswith("Supervisor")}
    )
    resource_groups = sorted(
        {e["group"] for e in graph["entityList"] if str(e.get("group", "")).startswith("Item_")}
    )
    if not supervisor_groups or not resource_groups:
        raise ValueError("Initial access policies require Supervisors and items")
    return [
        {
            "RequestingGroup": supervisor,
            "TargetType": "Group",
            "Target": resource,
            "MaxNumSessionKeyOwners": 2,
            "SessionCryptoSpec": "AES-128-CBC:SHA256",
            "AbsoluteValidity": "1*day",
            "RelativeValidity": "2*hour",
            "Expiration": "Infinity",
            "IsDelegated": 0,
        }
        for supervisor in supervisor_groups
        for resource in resource_groups
    ]


def build_layout(
    items: list[dict[str, Any]],
    bounds: dict[str, float],
    navigation_points: list[dict[str, Any]],
) -> dict[str, Any]:
    storage_locations: dict[str, Any] = {}
    item_locations: dict[str, Any] = {}
    for item in items:
        loc = item["storage_location_id"]
        storage_locations[loc] = {
            "storage_location_id": loc,
            "zone": item["zone"],
            "x": item["x"],
            "y": item["y"],
            "z": item["z"],
        }
        item_locations[resource_group(item)] = {
            "reference": item["reference"],
            "storage_location_id": loc,
            "zone": item["zone"],
            "x": item["x"],
            "y": item["y"],
            "z": item["z"],
        }
    return {
        "coordinate_source": "Storage_Location.csv",
        "warehouse_bounds": bounds,
        "zone_bounds": zone_bounds(bounds),
        "storage_locations": storage_locations,
        "item_locations": item_locations,
        "navigation_points": navigation_points,
    }


def random_robot_positions(
    robots: list[dict[str, Any]],
    rng: random.Random,
    bounds: dict[str, float],
    navigation_points: list[dict[str, Any]],
) -> dict[str, dict[str, float]]:
    positions: dict[str, dict[str, float]] = {}
    if navigation_points:
        # Each robot can physically appear anywhere in the warehouse irrespective of home zone.
        if len(robots) <= len(navigation_points):
            chosen = rng.sample(navigation_points, k=len(robots))
        else:
            chosen = [rng.choice(navigation_points) for _ in robots]
        for robot, p in zip(robots, chosen):
            positions[robot["group"]] = {"x": p["x"], "y": p["y"], "z": p["z"], "nav_label": p["label"]}
    else:
        for robot in robots:
            positions[robot["group"]] = {
                "x": rng.uniform(bounds["xmin"], bounds["xmax"]),
                "y": rng.uniform(bounds["ymin"], bounds["ymax"]),
                "z": 1.0,
            }
    return positions


def euclidean_distance_2d(p1: dict[str, float], p2: dict[str, float]) -> float:
    return math.hypot(float(p1["x"]) - float(p2["x"]), float(p1["y"]) - float(p2["y"]))


def nearest_robot(
    item: dict[str, Any],
    robots: list[dict[str, Any]],
    positions: dict[str, dict[str, float]],
) -> tuple[dict[str, Any], float]:
    target = {"x": item["x"], "y": item["y"]}
    best = min(robots, key=lambda r: euclidean_distance_2d(positions[r["group"]], target))
    return best, euclidean_distance_2d(positions[best["group"]], target)


def generate_workload(
    items: list[dict[str, Any]],
    picking: pd.DataFrame,
    robots: list[dict[str, Any]],
    bounds: dict[str, float],
    navigation_points: list[dict[str, Any]],
    num_positions: int,
    items_per_position: int,
    workload_order: str,
    seed: int,
) -> dict[str, Any]:
    if num_positions < 1:
        raise ValueError("--requests must be >= 1")
    if items_per_position < 1:
        raise ValueError("--items-per-position must be >= 1")
    if items_per_position > len(robots):
        raise ValueError("--items-per-position cannot exceed total number of robots")

    item_by_pair = {(i["reference"], i["storage_location_id"]): i for i in items}
    picking = picking[
        picking.apply(lambda r: (r["reference"], r["locations"]) in item_by_pair, axis=1)
    ].copy()
    if picking.empty:
        raise ValueError("No picking rows remain for registered resources")

    total_needed = num_positions * items_per_position
    rng = random.Random(seed + 10_000)

    # Deduplicate identical operational rows only for selection diversity; repeated picks remain
    # represented through frequency because random sampling is performed from the original rows.
    rows = picking.to_dict("records")
    if workload_order == "chronological":
        selected_rows = [rows[i % len(rows)] for i in range(total_needed)]
    else:
        selected_rows = [rng.choice(rows) for _ in range(total_needed)]

    requests: list[dict[str, Any]] = []
    cursor = 0
    for position_id in range(num_positions):
        # New physical robot positions per operational batch. Home zone does not constrain location.
        positions = random_robot_positions(robots, rng, bounds, navigation_points)
        available_robots = list(robots)

        # Avoid duplicate resources within one position batch when possible.
        used_resources: set[str] = set()
        batch_rows: list[dict[str, Any]] = []
        attempts = 0
        while len(batch_rows) < items_per_position and attempts < items_per_position * 50:
            row = selected_rows[cursor % len(selected_rows)] if workload_order == "chronological" else rng.choice(rows)
            if workload_order == "chronological":
                cursor += 1
            attempts += 1
            item = item_by_pair.get((str(row["reference"]), str(row["locations"])))
            if item is None:
                continue
            rg = resource_group(item)
            if rg in used_resources:
                continue
            used_resources.add(rg)
            batch_rows.append(row)

        if len(batch_rows) < items_per_position:
            raise ValueError("Could not form a workload batch with enough distinct resources")

        for row in batch_rows:
            item = item_by_pair[(str(row["reference"]), str(row["locations"]))]
            robot, distance = nearest_robot(item, available_robots, positions)
            available_robots.remove(robot)
            resource = resource_group(item)

            requests.append(
                {
                    "request_id": len(requests),
                    "position_id": position_id,
                    "wave_number": int(row["waveNumber"]) if pd.notna(row["waveNumber"]) else None,
                    "operator": str(row["operator"]),
                    "item_id": item["reference"],
                    "reference": item["reference"],
                    "size_us": None if pd.isna(row["Size (US)"]) else float(row["Size (US)"]),
                    "quantity_to_pick": int(row["quantityToPick (units)"]),
                    "storage_location_id": item["storage_location_id"],
                    "resource": resource,
                    "resource_zone": item["zone"],
                    "resource_position": {"x": item["x"], "y": item["y"], "z": item["z"]},
                    # Preserve the original experiment rule: the selected robot's home-zone
                    # supervisor/manager performs the delegation.
                    "supervisor": supervisor_entity_name(robot["zone"]),
                    "supervisor_group": "Supervisors",
                    "selected_robot": robot["group"],
                    "selected_robot_home_zone": robot["zone"],
                    "selected_robot_distance_m": round(distance, 4),
                    "cross_auth": robot["zone"] != item["zone"],
                    "robot_positions": {
                        r["group"]: {
                            k: (round(v, 4) if isinstance(v, float) else v)
                            for k, v in positions[r["group"]].items()
                        }
                        for r in robots
                    },
                }
            )

    return {
        "metadata": {
            "seed": seed,
            "num_requests": len(requests),
            "num_positions": num_positions,
            "items_per_position": items_per_position,
            "workload_source": "Picking_Wave.csv",
            "workload_order": workload_order,
            "robot_position_source": (
                "Support_Points_Navigation.csv" if navigation_points else "uniform_real_warehouse_bounds"
            ),
        },
        "requests": requests,
    }


def summarize(
    items: list[dict[str, Any]],
    supervisors: list[dict[str, str]],
    robots: list[dict[str, Any]],
    layout: dict[str, Any],
    workload: dict[str, Any],
    diagnostics: dict[str, Any],
) -> None:
    item_counts = defaultdict(int)
    robot_counts = defaultdict(int)
    for i in items:
        item_counts[i["zone"]] += 1
    for r in robots:
        robot_counts[r["zone"]] += 1
    cross = sum(1 for req in workload["requests"] if req["cross_auth"])

    print("\nGenerated warehouse experiment from real warehouse CSVs")
    print("-------------------------------------------------------")
    print(f"Resources: {len(items)}")
    print(f"Supervisors: {len(supervisors)}")
    print(f"Robots: {len(robots)}")
    print(f"Requests: {len(workload['requests'])}")
    print(f"Cross-Auth requests: {cross}")
    print(f"Real storage locations represented: {len(layout['storage_locations'])}")
    print(f"Navigation points: {len(layout['navigation_points'])}")
    print(f"Usable real picking rows: {diagnostics['usable_picking_rows']}")
    for z in ZONES:
        print(
            f"Zone {z}: Auth {ZONE_AUTH_IDS[z]}, "
            f"{item_counts[z]} resources, {robot_counts[z]} robots"
        )


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    items, picking, bounds, diagnostics = load_real_data(
        Path(args.product),
        Path(args.storage_locations),
        Path(args.picking_wave),
        args.num_resources,
        args.seed,
    )
    navigation_points = load_navigation_points(
        Path(args.navigation_points) if args.navigation_points else None,
        bounds,
    )
    supervisors = generate_supervisors()
    robots = generate_robots(args.robots_per_zone)

    graph = build_graph(items, supervisors, robots, args.validity)
    policies = build_supervisor_access_policies(graph)
    layout = build_layout(items, bounds, navigation_points)
    workload = generate_workload(
        items,
        picking,
        robots,
        bounds,
        navigation_points,
        args.requests,
        args.items_per_position,
        args.workload_order,
        args.seed,
    )

    graph_path = output_dir / "warehouse.graph"
    policy_path = graph_path.with_suffix(".policy.json")
    layout_path = output_dir / "warehouse_layout.json"
    workload_path = output_dir / "workload.json"
    write_json(graph, graph_path)
    write_json(policies, policy_path)
    write_json(layout, layout_path)
    write_json(workload, workload_path)

    summarize(items, supervisors, robots, layout, workload, diagnostics)
    print("\nWrote:")
    for p in (graph_path, policy_path, layout_path, workload_path):
        print(f"  {p}")


if __name__ == "__main__":
    main()
