"""Export deterministic node/edge review sheets and an automated pre-audit."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict, deque
from datetime import date
import json
import math
from pathlib import Path


IMAGE_WIDTH = 6709
IMAGE_HEIGHT = 6702
X_MIN, X_MAX = -35.468066, 22.740802
Y_MIN, Y_MAX = -28.238046, 30.183436


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def pixel_to_usd(pixel):
    return (
        X_MIN + pixel[0] / IMAGE_WIDTH * (X_MAX - X_MIN),
        Y_MAX - pixel[1] / IMAGE_HEIGHT * (Y_MAX - Y_MIN),
        0.0,
    )


def write_csv(path: Path, rows: list[dict], fields: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = (args.output or root.parent / "artifacts/map_review").resolve()
    nav = load(root / "maps/hospital_1f.navigation.json")
    coords = load(root / "maps/hospital_1f.coordinate_mapping.json")
    anchors = load(root / "artifacts/floorplan/hospital_1f.floorplan_anchor_mapping.json")
    routes = load(root / "artifacts/floorplan/hospital_1f.edge_routes.json")
    no_through = load(root / "maps/hospital_1f.no_through_zones.json")
    route_semantics = load(root / "semantics/route_semantics.json")
    risk_zones = load(root / "semantics/hospital_1f.risk_zones.json")
    safe_stops = load(root / "semantics/safe_stops.json")

    nodes = {item["id"]: item for item in nav["nodes"]}
    anchor_by_id = {item["node_id"]: item for item in anchors["nodes"]}
    safe_stop_ids = {item["node_id"] for item in safe_stops.get("stops", [])}
    semantic_nodes = route_semantics.get("nodes", {})
    waiting_area_ids = {
        node_id
        for node_id, value in semantic_nodes.items()
        if "WAITING_AREA" in value.get("semantic_tags", ())
    }
    emergency_node_ids = {
        node_id
        for zone in risk_zones.get("risk_zones", [])
        if zone.get("type") in {"EMERGENCY_CORRIDOR", "CRITICAL_ACCESS"}
        for node_id in zone.get("affected_nodes", [])
    }
    degrees = Counter()
    incident = defaultdict(list)
    graph = defaultdict(list)
    for edge in nav["edges"]:
        degrees[edge["from"]] += 1
        degrees[edge["to"]] += 1
        incident[edge["from"]].append(edge["id"])
        incident[edge["to"]].append(edge["id"])
        graph[edge["from"]].append(edge["to"])
        if edge.get("bidirectional"):
            graph[edge["to"]].append(edge["from"])

    issues = []
    node_rows = []
    for node_id, node in nodes.items():
        anchor = anchor_by_id[node_id]
        route_pixel = anchor["floorplan_pixel_xy"]
        semantic_pixel = anchor.get("semantic_floorplan_pixel_xy", route_pixel)
        semantic_offset = math.dist(
            pixel_to_usd(route_pixel), pixel_to_usd(semantic_pixel)
        )
        verification = node.get("verification", {})
        flags = []
        if verification.get("status") != "verified":
            flags.append("PENDING_HUMAN_REVIEW")
        if semantic_offset > 2.0:
            flags.append("SEMANTIC_ROUTE_OFFSET_GT_2M")
            issues.append(
                {
                    "severity": "critical" if semantic_offset > 10.0 else "warning",
                    "type": "NODE_POSITION_OFFSET",
                    "target": node_id,
                    "detail": f"semantic-to-route offset is {semantic_offset:.3f} m",
                }
            )
        if degrees[node_id] == 0:
            flags.append("ISOLATED")
            issues.append(
                {"severity": "error", "type": "ISOLATED_NODE", "target": node_id}
            )
        node_rows.append(
            {
                "node_id": node_id,
                "name_zh": node.get("name_zh"),
                "node_class": node.get("node_class"),
                "region_type": node.get("region_type"),
                "access_zone": node.get("access_zone"),
                "navigation_status": node.get("navigation_status"),
                "degree": degrees[node_id],
                "incident_edges": "|".join(sorted(incident[node_id])),
                "pixel_x": route_pixel[0],
                "pixel_y": route_pixel[1],
                "usd_x": coords["node_coordinates"][node_id]["usd_xyz_m"][0],
                "usd_y": coords["node_coordinates"][node_id]["usd_xyz_m"][1],
                "semantic_route_offset_m": round(semantic_offset, 3),
                "waiting_area": node_id in waiting_area_ids,
                "emergency_route": node_id in emergency_node_ids,
                "safe_stop": node_id in safe_stop_ids,
                "wheelchair_accessible": node.get("wheelchair_accessible"),
                "has_stairs": node.get("has_stairs"),
                "verification_status": verification.get("status", "missing"),
                "verification_method": verification.get("method"),
                "reviewer": verification.get("reviewer"),
                "reviewed_at": verification.get("reviewed_at"),
                "review_flags": "|".join(flags),
                "review_note": verification.get("note", ""),
            }
        )

    edge_rows = []
    edge_routes = routes["edge_routes"]
    for edge in nav["edges"]:
        left = anchor_by_id[edge["from"]]["floorplan_pixel_xy"]
        right = anchor_by_id[edge["to"]]["floorplan_pixel_xy"]
        points = [left, *edge_routes[edge["id"]], right]
        calculated = sum(
            math.dist(pixel_to_usd(a), pixel_to_usd(b))
            for a, b in zip(points, points[1:])
        )
        straight = math.dist(
            coords["node_coordinates"][edge["from"]]["usd_xyz_m"],
            coords["node_coordinates"][edge["to"]]["usd_xyz_m"],
        )
        verification = edge.get("verification", {})
        flags = []
        if verification.get("status") != "verified":
            flags.append("PENDING_HUMAN_REVIEW")
        if edge.get("width_status") == "unknown":
            flags.append("WIDTH_UNKNOWN")
        if edge.get("wheelchair_accessible") is None:
            flags.append("ACCESSIBILITY_UNKNOWN")
        if abs(float(edge["distance_m"]) - calculated) > 0.025:
            flags.append("DISTANCE_GEOMETRY_MISMATCH")
            issues.append(
                {
                    "severity": "error",
                    "type": "EDGE_DISTANCE_MISMATCH",
                    "target": edge["id"],
                    "detail": f"stored={edge['distance_m']} calculated={calculated:.3f}",
                }
            )
        edge_rows.append(
            {
                "edge_id": edge["id"],
                "parent_edge_id": edge.get("parent_edge_id", ""),
                "segment_index": edge.get("segment_index", ""),
                "segment_count": edge.get("segment_count", ""),
                "from": edge["from"],
                "to": edge["to"],
                "bidirectional": edge.get("bidirectional"),
                "distance_m": edge.get("distance_m"),
                "geometry_distance_m": round(calculated, 3),
                "straight_distance_m": round(straight, 3),
                "detour_ratio": round(calculated / straight, 3) if straight else "",
                "polyline_vertices": len(points),
                "access_zone": edge.get("access_zone"),
                "navigation_status": edge.get("navigation_status"),
                "emergency_route": edge.get("emergency_route"),
                "wheelchair_accessible": edge.get("wheelchair_accessible"),
                "has_stairs": edge.get("has_stairs"),
                "width_m": edge.get("width_m"),
                "width_status": edge.get("width_status"),
                "collision_clear": "static_point_geometry_inherited_or_checked",
                "verification_status": verification.get("status", "missing"),
                "verification_method": verification.get("method"),
                "reviewer": verification.get("reviewer"),
                "reviewed_at": verification.get("reviewed_at"),
                "review_flags": "|".join(flags),
                "review_note": verification.get("note", ""),
            }
        )

    coordinate_groups = defaultdict(list)
    for node_id in nodes:
        point = coords["node_coordinates"][node_id]["usd_xyz_m"]
        coordinate_groups[(round(point[0], 3), round(point[1], 3))].append(node_id)
    duplicate_groups = [values for values in coordinate_groups.values() if len(values) > 1]
    for group in duplicate_groups:
        issues.append(
            {
                "severity": "warning",
                "type": "COLOCATED_NODES",
                "target": "|".join(group),
                "detail": "Confirm that colocated semantic destinations are intentional.",
            }
        )

    start = next(iter(nodes))
    seen = {start}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for neighbor in graph[current]:
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append(neighbor)
    if seen != set(nodes):
        issues.append(
            {
                "severity": "error",
                "type": "UNREACHABLE_NODES",
                "target": "|".join(sorted(set(nodes) - seen)),
            }
        )

    node_fields = list(node_rows[0])
    edge_fields = list(edge_rows[0])
    write_csv(output / "hospital_1f_node_review.csv", node_rows, node_fields)
    write_csv(output / "hospital_1f_edge_review.csv", edge_rows, edge_fields)
    report = {
        "schema_version": "1.0.0",
        "generated_on": date.today().isoformat(),
        "map_id": nav.get("map_id"),
        "status": "UNDER_REVIEW",
        "node_count": len(nodes),
        "business_node_count": sum(
            node.get("node_class") == "safegate_location" for node in nodes.values()
        ),
        "task_node_count": sum(
            node.get("node_class") == "task_location" for node in nodes.values()
        ),
        "structure_node_count": sum(
            node.get("node_class") == "navigation_structure" for node in nodes.values()
        ),
        "edge_count": len(nav["edges"]),
        "verified_node_count": sum(
            node.get("verification", {}).get("status") == "verified"
            for node in nodes.values()
        ),
        "verified_edge_count": sum(
            edge.get("verification", {}).get("status") == "verified"
            for edge in nav["edges"]
        ),
        "unknown_accessibility_edge_count": sum(
            edge.get("wheelchair_accessible") is None for edge in nav["edges"]
        ),
        "unknown_width_edge_count": sum(
            edge.get("width_status") == "unknown" for edge in nav["edges"]
        ),
        "no_through_zone_count": len(no_through.get("zones", [])),
        "automated_issue_count": len(issues),
        "issues": issues,
        "freeze_blockers": [
            "Every node and edge must be manually reviewed.",
            "Wheelchair and stair semantics must be resolved.",
            "Unknown widths need an explicit operational policy.",
            "USD robot-footprint clearance must pass; point geometry is insufficient.",
            "Risk-zone geometry and emergency-corridor membership must be approved.",
        ],
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "map_validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: report[key] for key in (
        "status", "node_count", "edge_count", "verified_node_count",
        "verified_edge_count", "automated_issue_count"
    )}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
