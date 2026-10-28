"""Densify long navigation-structure edges without inventing new routes.

The script inserts degree-two waypoints along the already-authored,
collision-checked edge polylines.  It preserves each original edge as a
``parent_edge_id`` and synchronizes every sandbox artifact that stores node or
edge identities.  New waypoints are deliberately marked provisional: static
geometry can establish collision-free placement, but not on-site suitability.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path


IMAGE_WIDTH = 6709
IMAGE_HEIGHT = 6702
X_MIN, X_MAX = -35.468066, 22.740802
Y_MIN, Y_MAX = -28.238046, 30.183436


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def pixel_to_usd(pixel) -> tuple[float, float, float]:
    px, py = pixel
    return (
        X_MIN + px / IMAGE_WIDTH * (X_MAX - X_MIN),
        Y_MAX - py / IMAGE_HEIGHT * (Y_MAX - Y_MIN),
        0.0,
    )


def polyline_length(points) -> float:
    xyz = [pixel_to_usd(point) for point in points]
    return sum(math.dist(left, right) for left, right in zip(xyz, xyz[1:]))


def point_at_distance(points, cumulative, distance):
    if distance <= 0:
        return tuple(points[0]), 0
    if distance >= cumulative[-1]:
        return tuple(points[-1]), len(points) - 2
    for index in range(len(points) - 1):
        if cumulative[index + 1] + 1e-12 < distance:
            continue
        start = cumulative[index]
        span = cumulative[index + 1] - start
        ratio = 0.0 if span <= 1e-12 else (distance - start) / span
        left, right = points[index], points[index + 1]
        return (
            left[0] + (right[0] - left[0]) * ratio,
            left[1] + (right[1] - left[1]) * ratio,
        ), index
    raise AssertionError("distance does not lie on polyline")


def split_polyline(points, segment_count):
    xyz = [pixel_to_usd(point) for point in points]
    cumulative = [0.0]
    for left, right in zip(xyz, xyz[1:]):
        cumulative.append(cumulative[-1] + math.dist(left, right))
    total = cumulative[-1]
    cuts = [total * index / segment_count for index in range(segment_count + 1)]
    cut_points = [point_at_distance(points, cumulative, value) for value in cuts]
    result = []
    for segment_index in range(segment_count):
        start, start_line = cut_points[segment_index]
        end, end_line = cut_points[segment_index + 1]
        segment = [start]
        for vertex_index in range(start_line + 1, end_line + 1):
            vertex_distance = cumulative[vertex_index]
            if cuts[segment_index] + 1e-9 < vertex_distance < cuts[segment_index + 1] - 1e-9:
                segment.append(tuple(points[vertex_index]))
        segment.append(end)
        rounded = []
        for point in segment:
            candidate = [round(point[0]), round(point[1])]
            if not rounded or rounded[-1] != candidate:
                rounded.append(candidate)
        if len(rounded) < 2:
            raise RuntimeError("densification created a zero-length segment")
        result.append(rounded)
    return result


def _verification(status, method, note):
    return {
        "status": status,
        "method": method,
        "reviewer": None,
        "reviewed_at": None,
        "note": note,
    }


def densify_artifacts(
    *,
    nav: dict,
    coords: dict,
    anchors: dict,
    routes: dict,
    registry: dict,
    zones: dict,
    entrances: dict,
    route_semantics: dict,
    candidates: dict | None = None,
    target_node_count: int = 120,
    max_structure_edge_m: float = 5.6,
) -> dict:
    """Mutate synchronized artifacts and return a deterministic summary."""
    if not 100 <= target_node_count <= 150:
        raise ValueError("target_node_count must be between 100 and 150")
    nodes = {node["id"]: node for node in nav.get("nodes", [])}
    if len(nodes) != len(nav.get("nodes", [])):
        raise ValueError("navigation node IDs must be unique")
    if len(nodes) >= target_node_count:
        return {
            "status": "already_dense",
            "node_count": len(nodes),
            "edge_count": len(nav.get("edges", [])),
            "inserted_nodes": 0,
        }

    anchor_by_id = {item["node_id"]: item for item in anchors.get("nodes", [])}
    edge_routes = routes.get("edge_routes", {})
    structure_edges = []
    segment_counts = {}
    for edge in nav.get("edges", []):
        left = nodes.get(edge.get("from"), {})
        right = nodes.get(edge.get("to"), {})
        if (
            left.get("node_class") == "navigation_structure"
            and right.get("node_class") == "navigation_structure"
        ):
            count = max(1, math.ceil(float(edge["distance_m"]) / max_structure_edge_m))
            structure_edges.append(edge)
            segment_counts[edge["id"]] = count

    required = target_node_count - len(nodes)
    current = sum(count - 1 for count in segment_counts.values())
    while current < required:
        edge = max(
            structure_edges,
            key=lambda item: (
                float(item["distance_m"]) / segment_counts[item["id"]],
                item["id"],
            ),
        )
        segment_counts[edge["id"]] += 1
        current += 1
    while current > required:
        candidates = [edge for edge in structure_edges if segment_counts[edge["id"]] > 1]
        if not candidates:
            raise RuntimeError("cannot reach requested node count")
        edge = min(
            candidates,
            key=lambda item: (
                float(item["distance_m"]) / (segment_counts[item["id"]] - 1),
                item["id"],
            ),
        )
        segment_counts[edge["id"]] -= 1
        current -= 1

    next_display = max(
        (item.get("display_number", 0) for item in anchors.get("nodes", [])),
        default=0,
    ) + 1
    waypoint_number = 1
    existing_ids = set(nodes)
    while f"nav-f1-waypoint-{waypoint_number:03d}" in existing_ids:
        waypoint_number += 1

    new_nodes = []
    new_edges = []
    new_edge_routes = {}
    parent_segments = {}
    semantic_nodes = route_semantics.setdefault("nodes", {})

    for edge in nav.get("edges", []):
        segment_count = segment_counts.get(edge["id"], 1)
        if segment_count == 1:
            edge.setdefault(
                "verification",
                _verification(
                    "provisional",
                    "existing_map_and_static_geometry",
                    "Requires human review before experiment-map freeze.",
                ),
            )
            edge.setdefault("wheelchair_accessible", None)
            edge.setdefault("has_stairs", None)
            edge.setdefault("width_m", None)
            edge.setdefault("width_status", "unknown")
            new_edges.append(edge)
            new_edge_routes[edge["id"]] = edge_routes[edge["id"]]
            continue

        source = edge["from"]
        target = edge["to"]
        points = [
            anchor_by_id[source]["floorplan_pixel_xy"],
            *edge_routes[edge["id"]],
            anchor_by_id[target]["floorplan_pixel_xy"],
        ]
        pieces = split_polyline(points, segment_count)
        chain = [source]
        for index in range(1, segment_count):
            waypoint_id = f"nav-f1-waypoint-{waypoint_number:03d}"
            waypoint_number += 1
            pixel = pieces[index - 1][-1]
            xyz = [round(value, 6) for value in pixel_to_usd(pixel)]
            waypoint = {
                "id": waypoint_id,
                "name_zh": f"走廊细分点{waypoint_id[-3:]}",
                "region_type": "corridor_waypoint",
                "access_zone": edge.get("access_zone", "public"),
                "navigation_status": edge.get("navigation_status", "open"),
                "node_class": "navigation_structure",
                "wheelchair_accessible": edge.get("wheelchair_accessible"),
                "has_stairs": edge.get("has_stairs"),
                "verification": _verification(
                    "provisional",
                    "derived_from_collision_checked_parent_edge",
                    f"Inserted on parent edge {edge['id']}; requires floor-plan and on-site review.",
                ),
            }
            display_number = next_display
            next_display += 1
            new_nodes.append(waypoint)
            nodes[waypoint_id] = waypoint
            chain.append(waypoint_id)
            coords["node_coordinates"][waypoint_id] = {
                "usd_xyz_m": xyz,
                "yaw_deg": 0.0,
                "clearance_radius_m": 0.0,
                "coordinate_status": "derived_from_collision_checked_parent_edge",
                "source_seed_usd_xy_m": xyz[:2],
                "usd_snap_displacement_m": 0.0,
            }
            anchor_record = {
                    "display_number": display_number,
                    "node_id": waypoint_id,
                    "name_zh": waypoint["name_zh"],
                    "floorplan_pixel_xy": pixel,
                    "floorplan_normalized_xy": [
                        round(pixel[0] / IMAGE_WIDTH, 6),
                        round(pixel[1] / IMAGE_HEIGHT, 6),
                    ],
                    "semantic_floorplan_pixel_xy": pixel,
                    "source_label": f"derived from {edge['id']}",
                    "alignment_status": "derived_from_verified_edge_polyline",
                    "usd_snap_displacement_m": 0.0,
                }
            anchors.setdefault("nodes", []).append(anchor_record)
            anchor_by_id[waypoint_id] = anchor_record
            registry.setdefault("locations", []).append(
                {
                    "canonical_name": waypoint["name_zh"],
                    "source_location_id": None,
                    "node_id": waypoint_id,
                    "aliases": [],
                    "location_class": "navigation_structure",
                }
            )
            entrances.setdefault("entries", []).append(
                {
                    "node_id": waypoint_id,
                    "name_zh": waypoint["name_zh"],
                    "region_type": "corridor_waypoint",
                    "access_zone": waypoint["access_zone"],
                    "target_kind": "corridor_waypoint",
                    "floorplan_semantic_anchor": {
                        "pixel_xy": pixel,
                        "registered_usd_xyz_m": xyz,
                        "source_label": f"derived from {edge['id']}",
                        "alignment_status": "derived_from_verified_edge_polyline",
                    },
                    "door_match": {"status": "not_applicable", "method": None},
                    "door_cluster_id": None,
                    "approach_pose": {
                        "usd_xyz_m": xyz,
                        "yaw_deg": 0.0,
                        "door_offset_m": None,
                        "yaw_source": "derived_edge_tangent_pending_review",
                    },
                    "clearance": {
                        "robot_radius_m": 0.0,
                        "required_margin_m": 0.0,
                        "required_clearance_radius_m": 0.0,
                        "static_collision_status": "inherited_collision_free_parent_polyline",
                        "navmesh_status": "not_required_for_point_topology",
                    },
                    "mapping_confidence": "derived_static_geometry",
                    "verification_status": "pending_human_review",
                }
            )
            if edge.get("emergency_route"):
                semantic_nodes[waypoint_id] = {
                    "semantic_tags": ["EMERGENCY_CORRIDOR"]
                }
        chain.append(target)
        parent_segments[edge["id"]] = []
        for index, piece in enumerate(pieces, 1):
            segment_id = edge["id"] if index == 1 else f"{edge['id']}-S{index:02d}"
            child = deepcopy(edge)
            child.update(
                {
                    "id": segment_id,
                    "from": chain[index - 1],
                    "to": chain[index],
                    "distance_m": round(polyline_length(piece), 3),
                    "parent_edge_id": edge["id"],
                    "parent_edge_from": source,
                    "parent_edge_to": target,
                    "segment_index": index,
                    "segment_count": segment_count,
                    "verification": _verification(
                        "provisional",
                        "split_from_collision_checked_parent_edge",
                        f"Segment {index}/{segment_count} of {edge['id']}; requires human review.",
                    ),
                    "wheelchair_accessible": edge.get("wheelchair_accessible"),
                    "has_stairs": edge.get("has_stairs"),
                    "width_m": edge.get("width_m"),
                    "width_status": edge.get("width_status", "unknown"),
                }
            )
            new_edges.append(child)
            new_edge_routes[segment_id] = piece[1:-1]
            parent_segments[edge["id"]].append(segment_id)

    for node in nav.get("nodes", []):
        for redundant_field in (
            "map_annotation", "safe_stop", "emergency_route", "waiting_area"
        ):
            node.pop(redundant_field, None)
        node.setdefault(
            "verification",
            _verification(
                "provisional",
                "existing_map_and_static_geometry",
                "Requires human review before experiment-map freeze.",
            ),
        )
        node.setdefault("wheelchair_accessible", None)
        node.setdefault("has_stairs", node.get("region_type") == "stairs")
    nav["nodes"].extend(new_nodes)
    nav["edges"] = new_edges
    routes["edge_routes"] = new_edge_routes
    public_zone = next(zone for zone in zones["zones"] if zone.get("id") == "public")
    public_zone["node_ids"] = sorted(
        set(public_zone.get("node_ids", ()))
        | {node["id"] for node in new_nodes if node["access_zone"] == "public"}
    )
    nav.pop("safe_stops", None)
    nav.pop("waiting_areas", None)
    nav.pop("emergency_corridor", None)
    nav["node_count_policy"] = "experiment_map_100_to_150_nodes"
    nav.setdefault("provenance", {})["topology_densification"] = {
        "method": "split_existing_collision_checked_structure_edge_polylines",
        "target_node_count": target_node_count,
        "max_structure_edge_m": max_structure_edge_m,
        "inserted_node_count": len(new_nodes),
        "human_review_status": "pending",
    }
    routes["densification"] = nav["provenance"]["topology_densification"]
    if candidates is not None:
        structure_ids = {
            node["id"]
            for node in nav["nodes"]
            if node.get("node_class") == "navigation_structure"
        }
        candidates["nodes"] = [
            {
                "id": node["id"],
                "name_zh": node["name_zh"],
                "usd_xyz_m": coords["node_coordinates"][node["id"]]["usd_xyz_m"],
                "seed_floorplan_pixel_xy": anchor_by_id[node["id"]][
                    "floorplan_pixel_xy"
                ],
                "seed_displacement_m": coords["node_coordinates"][node["id"]].get(
                    "usd_snap_displacement_m", 0.0
                ),
                "derivation": coords["node_coordinates"][node["id"]].get(
                    "coordinate_status", "unknown"
                ),
                "node_kind": node.get("region_type", "corridor_structure"),
            }
            for node in nav["nodes"]
            if node["id"] in structure_ids
        ]
        candidates["edges"] = [
            {
                "id": edge["id"],
                "from": edge["from"],
                "to": edge["to"],
                "distance_m": edge["distance_m"],
                "parent_edge_id": edge.get("parent_edge_id"),
                "verification": "derived_or_existing_collision_free_polyline",
            }
            for edge in new_edges
            if edge["from"] in structure_ids and edge["to"] in structure_ids
        ]
        candidates["verification_status"] = "static_geometry_checked_human_review_pending"
    return {
        "status": "densified",
        "node_count": len(nav["nodes"]),
        "edge_count": len(new_edges),
        "inserted_nodes": len(new_nodes),
        "split_parent_edges": len(parent_segments),
        "parent_segments": parent_segments,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--target-node-count", type=int, default=120)
    parser.add_argument("--max-structure-edge-m", type=float, default=5.6)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    paths = {
        "nav": root / "maps/hospital_1f.navigation.json",
        "coords": root / "maps/hospital_1f.coordinate_mapping.json",
        "anchors": root / "artifacts/floorplan/hospital_1f.floorplan_anchor_mapping.json",
        "routes": root / "artifacts/floorplan/hospital_1f.edge_routes.json",
        "registry": root / "semantics/location_registry.json",
        "zones": root / "semantics/access_zones.json",
        "entrances": root / "maps/hospital_1f.entrance_mapping.json",
        "route_semantics": root / "semantics/route_semantics.json",
    }
    artifacts = {name: load(path) for name, path in paths.items()}
    summary = densify_artifacts(
        **artifacts,
        target_node_count=args.target_node_count,
        max_structure_edge_m=args.max_structure_edge_m,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.write and summary["status"] == "densified":
        for name, path in paths.items():
            save(path, artifacts[name])
        report_path = root.parent / "artifacts/map_review/densification_report.json"
        save(report_path, summary)
        print(f"Wrote synchronized artifacts and {report_path}")
    elif not args.write:
        print("Dry run only; pass --write to update the sandbox")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
