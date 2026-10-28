"""Validate whether hospital_1f is structurally ready or fully freeze-ready."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path


AUTHORITATIVE_FILES = (
    "maps/hospital_1f.navigation.json",
    "maps/hospital_1f.coordinate_mapping.json",
    "maps/hospital_1f.entrance_mapping.json",
    "maps/hospital_1f.no_through_zones.json",
    "artifacts/floorplan/hospital_1f.edge_routes.json",
    "semantics/access_zones.json",
    "semantics/location_registry.json",
    "semantics/route_semantics.json",
    "semantics/hospital_1f.risk_zones.json",
    "semantics/safe_stops.json",
    "semantics/task_destinations.json",
)


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def validate(root: Path, *, allow_provisional: bool = False) -> dict:
    errors = []
    warnings = []

    def check(condition, message, *, warning=False):
        if not condition:
            (warnings if warning else errors).append(message)

    missing = [relative for relative in AUTHORITATIVE_FILES if not (root / relative).is_file()]
    check(not missing, f"missing authoritative files: {missing}")
    if missing:
        return {"status": "INVALID", "errors": errors, "warnings": warnings}

    nav = load(root / AUTHORITATIVE_FILES[0])
    coords = load(root / AUTHORITATIVE_FILES[1])
    entrances = load(root / AUTHORITATIVE_FILES[2])
    routes = load(root / AUTHORITATIVE_FILES[4])
    zones = load(root / AUTHORITATIVE_FILES[5])
    registry = load(root / AUTHORITATIVE_FILES[6])
    semantics = load(root / AUTHORITATIVE_FILES[7])
    risk = load(root / AUTHORITATIVE_FILES[8])
    stops = load(root / AUTHORITATIVE_FILES[9])
    task_destinations = load(root / AUTHORITATIVE_FILES[10])

    nodes = {item.get("id"): item for item in nav.get("nodes", [])}
    edges = {item.get("id"): item for item in nav.get("edges", [])}
    node_ids = set(nodes)
    edge_ids = set(edges)
    check(None not in node_ids and len(nodes) == len(nav.get("nodes", [])), "node IDs are missing or duplicated")
    check(None not in edge_ids and len(edges) == len(nav.get("edges", [])), "edge IDs are missing or duplicated")
    check(100 <= len(nodes) <= 150, f"node count must be 100-150, got {len(nodes)}")
    check(bool(edges), "edge set is empty")
    check(set(coords.get("node_coordinates", {})) == node_ids, "coordinate node set differs")
    check({item.get("node_id") for item in entrances.get("entries", [])} == node_ids, "entrance node set differs")
    check({item.get("node_id") for item in registry.get("locations", [])} == node_ids, "registry node set differs")
    check(set(routes.get("edge_routes", {})) == edge_ids, "edge-route ID set differs")
    check(set(semantics.get("nodes", {})) <= node_ids, "route semantics reference unknown nodes")
    task_node_ids = {
        item.get("node_id") for item in task_destinations.get("destinations", [])
    }
    check(None not in task_node_ids, "task destination is missing node_id")
    check(task_node_ids <= node_ids, "task destinations reference unknown nodes")
    check(
        all(nodes[node_id].get("node_class") == "task_location" for node_id in task_node_ids if node_id in nodes),
        "task destinations must use node_class=task_location",
    )

    zone_members = set()
    for zone in zones.get("zones", []):
        for node_id in zone.get("node_ids", []):
            check(node_id in nodes, f"access zone references unknown node: {node_id}")
            check(node_id not in zone_members, f"node appears in multiple access zones: {node_id}")
            zone_members.add(node_id)
    check(zone_members == node_ids, "access-zone membership does not cover exactly all nodes")

    for node_id, node in nodes.items():
        verification = node.get("verification", {})
        check(bool(verification), f"node lacks verification record: {node_id}")
        if verification.get("status") != "verified":
            message = f"node is not manually verified: {node_id}"
            (warnings if allow_provisional else errors).append(message)
        check("wheelchair_accessible" in node, f"node lacks wheelchair field: {node_id}")
        check("has_stairs" in node, f"node lacks stairs field: {node_id}")
        if node.get("routing_scope") == "medication_delivery_only":
            check(
                node.get("general_navigation_allowed") is False,
                f"task route node is exposed to general navigation: {node_id}",
            )

    graph = {node_id: set() for node_id in nodes}
    for edge_id, edge in edges.items():
        left, right = edge.get("from"), edge.get("to")
        check(left in nodes and right in nodes and left != right, f"invalid edge endpoints: {edge_id}")
        if left in nodes and right in nodes:
            graph[left].add(right)
            if edge.get("bidirectional"):
                graph[right].add(left)
        check(
            isinstance(edge.get("distance_m"), (int, float))
            and not isinstance(edge.get("distance_m"), bool)
            and math.isfinite(edge["distance_m"])
            and edge["distance_m"] > 0,
            f"invalid edge distance: {edge_id}",
        )
        if (
            isinstance(edge.get("distance_m"), (int, float))
            and edge.get("general_navigation_allowed") is not False
            and edge.get("routing_scope") != "medication_delivery_only"
        ):
            check(
                edge["distance_m"] <= 8.0,
                f"general-navigation edge exceeds 8 m: {edge_id} distance={edge['distance_m']}",
            )
        verification = edge.get("verification", {})
        if edge.get("routing_scope") == "medication_delivery_only":
            check(
                edge.get("general_navigation_allowed") is False,
                f"task route edge is exposed to general navigation: {edge_id}",
            )
        check(bool(verification), f"edge lacks verification record: {edge_id}")
        if verification.get("status") != "verified":
            message = f"edge is not manually verified: {edge_id}"
            (warnings if allow_provisional else errors).append(message)
        for field in ("wheelchair_accessible", "has_stairs", "width_m", "width_status"):
            check(field in edge, f"edge lacks {field}: {edge_id}")
        if edge.get("width_status") == "unknown":
            message = f"edge width remains unknown: {edge_id}"
            (warnings if allow_provisional else errors).append(message)

    if nodes:
        start = next(iter(nodes))
        seen = {start}
        stack = [start]
        while stack:
            current = stack.pop()
            for neighbor in graph[current]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        check(seen == node_ids, f"graph is not fully reachable: {sorted(node_ids - seen)}")

    for node_id, node in nodes.items():
        if node_id.startswith("nav-f1-waypoint-"):
            degree = sum(
                node_id in (edge.get("from"), edge.get("to"))
                and edge.get("general_navigation_allowed") is not False
                for edge in edges.values()
            )
            expected_degree = (
                3
                if node_id
                in {
                    "nav-f1-waypoint-018",
                    "nav-f1-waypoint-019",
                    "nav-f1-waypoint-013",
                    "nav-f1-waypoint-015",
                    "nav-f1-waypoint-016",
                    "nav-f1-waypoint-024",
                    "nav-f1-waypoint-052",
                    "nav-f1-waypoint-063",
                    "nav-f1-waypoint-001",
                }
                else 2
            )
            if node_id == "nav-f1-waypoint-016":
                expected_degree = 4
            check(
                degree == expected_degree,
                f"derived waypoint has unexpected degree: {node_id} "
                f"degree={degree} expected={expected_degree}",
            )

    emergency_edges = {edge_id for edge_id, edge in edges.items() if edge.get("emergency_route")}

    risk_edge_union = set()
    risk_node_union = set()
    for item in risk.get("risk_zones", []):
        affected_edges = set(item.get("affected_edges", []))
        affected_nodes = set(item.get("affected_nodes", []))
        check(affected_edges <= edge_ids, f"risk zone references unknown edges: {item.get('risk_zone_id')}")
        check(affected_nodes <= node_ids, f"risk zone references unknown nodes: {item.get('risk_zone_id')}")
        risk_edge_union |= affected_edges
        risk_node_union |= affected_nodes
        if item.get("verification", {}).get("status") != "verified":
            message = f"risk zone is not verified: {item.get('risk_zone_id')}"
            (warnings if allow_provisional else errors).append(message)
    check(emergency_edges <= risk_edge_union, "one or more emergency edges are absent from risk zones")

    stop_ids = {item.get("node_id") for item in stops.get("stops", [])}
    check(stop_ids <= node_ids, "safe stops reference unknown nodes")

    return {
        "status": "VALID_FOR_REVIEW" if not errors and warnings else "READY_TO_FREEZE" if not errors else "INVALID",
        "map_id": nav.get("map_id"),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "safe_stop_count": len(stop_ids),
        "errors": errors,
        "warnings": warnings,
        "hashes": {relative: sha256(root / relative) for relative in AUTHORITATIVE_FILES},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--allow-provisional", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = validate(args.root.resolve(), allow_provisional=args.allow_provisional)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0 if report["status"] != "INVALID" else 1


if __name__ == "__main__":
    raise SystemExit(main())
