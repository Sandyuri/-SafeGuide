"""Remove one navigation node and its dependent records from the sandbox."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sandbox", type=Path, default=Path("navigation_sandbox"))
    parser.add_argument("--node-id", required=True)
    args = parser.parse_args()

    root = args.sandbox
    node_id = args.node_id
    nav_path = root / "maps" / "hospital_1f.navigation.json"
    coord_path = root / "maps" / "hospital_1f.coordinate_mapping.json"
    entrance_path = root / "maps" / "hospital_1f.entrance_mapping.json"
    registry_path = root / "semantics" / "location_registry.json"
    stops_path = root / "semantics" / "safe_stops.json"
    anchor_path = root / "artifacts" / "floorplan" / "hospital_1f.floorplan_anchor_mapping.json"
    routes_path = root / "artifacts" / "floorplan" / "hospital_1f.edge_routes.json"

    nav = load(nav_path)
    removed_edges = {
        edge["id"]
        for edge in nav["edges"]
        if edge["from"] == node_id or edge["to"] == node_id
    }
    if not any(node["id"] == node_id for node in nav["nodes"]):
        raise SystemExit(f"navigation node does not exist: {node_id}")
    nav["nodes"] = [node for node in nav["nodes"] if node["id"] != node_id]
    nav["edges"] = [
        edge
        for edge in nav["edges"]
        if edge["from"] != node_id and edge["to"] != node_id
    ]
    nav.pop("waiting_areas", None)
    nav.pop("safe_stops", None)
    nav.pop("emergency_corridor", None)
    save(nav_path, nav)

    coordinates = load(coord_path)
    coordinates.get("node_coordinates", {}).pop(node_id, None)
    save(coord_path, coordinates)

    entrances = load(entrance_path)
    entrances["entries"] = [
        entry for entry in entrances.get("entries", []) if entry.get("node_id") != node_id
    ]
    save(entrance_path, entrances)

    registry = load(registry_path)
    registry["locations"] = [
        location for location in registry.get("locations", []) if location.get("node_id") != node_id
    ]
    save(registry_path, registry)

    stops = load(stops_path)
    stops["stops"] = [
        stop for stop in stops.get("stops", []) if stop.get("node_id") != node_id
    ]
    save(stops_path, stops)

    anchors = load(anchor_path)
    anchors["nodes"] = [
        node for node in anchors.get("nodes", []) if node.get("node_id") != node_id
    ]
    save(anchor_path, anchors)

    routes = load(routes_path)
    for edge_id in removed_edges:
        routes.get("edge_routes", {}).pop(edge_id, None)
    save(routes_path, routes)

    print(f"removed {node_id}; removed edges: {', '.join(sorted(removed_edges)) or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
