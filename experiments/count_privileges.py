#!/usr/bin/env python3

import argparse
import json
from collections import Counter


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("graph", help="Path to .graph file")
    args = parser.parse_args()

    with open(args.graph, "r") as f:
        graph = json.load(f)

    counts = Counter(
        privilege["privilegeType"]
        for privilege in graph.get("privilegeList", [])
    )

    print(f"DelegationGrant : {counts.get('DelegationGrant', 0)}")
    print(f"DelegationRevoke: {counts.get('DelegationRevoke', 0)}")


if __name__ == "__main__":
    main()

