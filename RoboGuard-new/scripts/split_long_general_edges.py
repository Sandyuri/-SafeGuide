"""Split the eleven operator-confirmed long general-navigation edges.

Each parent polyline is cut at its arc-length midpoint.  This preserves the
reviewed route geometry while enforcing the 8 m general-navigation edge cap.
Medication-delivery-only edges are deliberately out of scope.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date
import json
from pathlib import Path

from apply_safegate_csv_review import write_scene
from densify_navigation_topology import (
    IMAGE_HEIGHT,
    IMAGE_WIDTH,
    load,
    pixel_to_usd,
    polyline_length,
    save,
    split_polyline,
)


MAX_GENERAL_EDGE_M = 8.0
TARGET_EDGE_IDS = (
    "E298",
    "E287",
    "E258",
    "E296",
    "E249",
    "E292",
    "E295",
    "E269",
    "E294",
    "E267",
    "E290",
)


def verification(parent_id: str, segment: int | None = None) -> dict:
    if segment is None:
        note = (
            f"Inserted at the arc-length midpoint of {parent_id} under the "
            "operator-confirmed 8 m general-edge policy; requires on-site review."
        )
    else:
        note = (
            f"Segment {segment}/2 of {parent_id}; geometry is inherited from the "
            "existing parent polyline and requires on-site review."
        )
    return {
        "status": "provisional",
        "method": "split_at_parent_polyline_arc_length_midpoint",
        "reviewer": None,
        "reviewed_at": None,
        "note": note,
    }


def apply(project_root: Path, reviewed_at: str) -> dict:
    sandbox = project_root / "navigation_sandbox"
    paths = {
        "nav": sandbox / "maps/hospital_1f.navigation.json",
        "coords": sandbox / "maps/hospital_1f.coordinate_mapping.json",
        "anchors": sandbox
        / "artifacts/floorplan/hospital_1f.floorplan_anchor_mapping.json",
        "routes": sandbox / "artifacts/floorplan/hospital_1f.edge_routes.json",
        "registry": sandbox / "semantics/location_registry.json",
        "zones": sandbox / "semantics/access_zones.json",
        "entrances": sandbox / "maps/hospital_1f.entrance_mapping.json",
        "route_semantics": sandbox / "semantics/route_semantics.json",
        "risk_zones": sandbox / "semantics/hospital_1f.risk_zones.json",
        "stops": sandbox / "semantics/safe_stops.json",
    }
    data = {name: load(path) for name, path in paths.items()}
    nav = data["nav"]
    provenance = nav.setdefault("provenance", {})
    previous = provenance.get("general_edge_8m_split")
    if previous:
        long_general = [
            edge["id"]
            for edge in nav["edges"]
            if edge.get("general_navigation_allowed") is not False
            and edge.get("routing_scope") != "medication_delivery_only"
            and float(edge["distance_m"]) > MAX_GENERAL_EDGE_M + 1e-9
        ]
        if long_general:
            raise RuntimeError(
                "split provenance exists but long general edges remain: "
                + ", ".join(long_general)
            )
        return {
            "status": "already_applied",
            "node_count": len(nav["nodes"]),
            "edge_count": len(nav["edges"]),
            "inserted_nodes": previous.get("inserted_node_ids", []),
        }

    nodes = {item["id"]: item for item in nav["nodes"]}
    edges_by_id = {item["id"]: item for item in nav["edges"]}
    anchors = data["anchors"]
    anchor_by_id = {item["node_id"]: item for item in anchors["nodes"]}
    edge_routes = data["routes"]["edge_routes"]

    missing = [edge_id for edge_id in TARGET_EDGE_IDS if edge_id not in edges_by_id]
    if missing:
        raise ValueError(f"target edges are missing: {missing}")
    for edge_id in TARGET_EDGE_IDS:
        edge = edges_by_id[edge_id]
        if (
            edge.get("general_navigation_allowed") is False
            or edge.get("routing_scope") == "medication_delivery_only"
        ):
            raise ValueError(f"target edge is not general navigation: {edge_id}")
        if float(edge["distance_m"]) <= MAX_GENERAL_EDGE_M:
            raise ValueError(f"target edge is not over 8 m: {edge_id}")
        child_id = f"{edge_id}-S02"
        if child_id in edges_by_id:
            raise ValueError(f"child edge ID already exists: {child_id}")

    next_display = max(item["display_number"] for item in anchors["nodes"]) + 1
    next_waypoint = 1
    while f"nav-f1-waypoint-{next_waypoint:03d}" in nodes:
        next_waypoint += 1

    zone_by_id = {item["id"]: item for item in data["zones"]["zones"]}
    semantic_nodes = data["route_semantics"].setdefault("nodes", {})
    new_nodes = []
    new_edges = []
    new_routes = {}
    split_records = []

    for edge in nav["edges"]:
        if edge["id"] not in TARGET_EDGE_IDS:
            new_edges.append(edge)
            new_routes[edge["id"]] = edge_routes[edge["id"]]
            continue

        parent_id = edge["id"]
        source, target = edge["from"], edge["to"]
        points = [
            anchor_by_id[source]["floorplan_pixel_xy"],
            *edge_routes[parent_id],
            anchor_by_id[target]["floorplan_pixel_xy"],
        ]
        pieces = split_polyline(points, 2)
        waypoint_id = f"nav-f1-waypoint-{next_waypoint:03d}"
        next_waypoint += 1
        pixel = pieces[0][-1]
        xyz = [round(value, 6) for value in pixel_to_usd(pixel)]
        display_number = next_display
        next_display += 1
        waypoint = {
            "id": waypoint_id,
            "name_zh": f"长边细分点{waypoint_id[-3:]}",
            "region_type": "corridor_waypoint",
            "access_zone": edge.get("access_zone", "public"),
            "navigation_status": edge.get("navigation_status", "open"),
            "node_class": "navigation_structure",
            "wheelchair_accessible": edge.get("wheelchair_accessible"),
            "has_stairs": edge.get("has_stairs"),
            "verification": verification(parent_id),
        }
        new_nodes.append(waypoint)
        nodes[waypoint_id] = waypoint
        data["coords"]["node_coordinates"][waypoint_id] = {
            "usd_xyz_m": xyz,
            "yaw_deg": 0.0,
            "clearance_radius_m": 0.0,
            "coordinate_status": "derived_from_existing_parent_edge_polyline",
            "source_seed_usd_xy_m": xyz[:2],
            "usd_snap_displacement_m": 0.0,
        }
        anchor = {
            "display_number": display_number,
            "node_id": waypoint_id,
            "name_zh": waypoint["name_zh"],
            "floorplan_pixel_xy": pixel,
            "floorplan_normalized_xy": [
                round(pixel[0] / IMAGE_WIDTH, 6),
                round(pixel[1] / IMAGE_HEIGHT, 6),
            ],
            "semantic_floorplan_pixel_xy": pixel,
            "source_label": f"8 m split of {parent_id}",
            "alignment_status": "derived_from_existing_parent_edge_polyline",
            "usd_snap_displacement_m": 0.0,
        }
        anchors["nodes"].append(anchor)
        anchor_by_id[waypoint_id] = anchor
        data["registry"]["locations"].append(
            {
                "canonical_name": waypoint["name_zh"],
                "source_location_id": None,
                "node_id": waypoint_id,
                "aliases": [],
                "location_class": "navigation_structure",
            }
        )
        data["entrances"]["entries"].append(
            {
                "node_id": waypoint_id,
                "name_zh": waypoint["name_zh"],
                "region_type": "corridor_waypoint",
                "access_zone": waypoint["access_zone"],
                "target_kind": "corridor_waypoint",
                "floorplan_semantic_anchor": {
                    "pixel_xy": pixel,
                    "registered_usd_xyz_m": xyz,
                    "source_label": f"8 m split of {parent_id}",
                    "alignment_status": "derived_from_existing_parent_edge_polyline",
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
        zone = zone_by_id.get(waypoint["access_zone"])
        if zone is None:
            raise ValueError(f"unknown access zone: {waypoint['access_zone']}")
        zone.setdefault("node_ids", []).append(waypoint_id)
        zone["node_ids"] = sorted(set(zone["node_ids"]))
        if edge.get("emergency_route"):
            semantic_nodes[waypoint_id] = {"semantic_tags": ["EMERGENCY_CORRIDOR"]}

        segment_ids = [parent_id, f"{parent_id}-S02"]
        endpoints = [(source, waypoint_id), (waypoint_id, target)]
        segment_distances = []
        for index, (segment_id, piece, (left, right)) in enumerate(
            zip(segment_ids, pieces, endpoints), 1
        ):
            child = deepcopy(edge)
            distance = round(polyline_length(piece), 3)
            child.update(
                {
                    "id": segment_id,
                    "from": left,
                    "to": right,
                    "distance_m": distance,
                    "distance_source": "split_from_existing_parent_edge_polyline",
                    "parent_edge_id": parent_id,
                    "parent_edge_from": source,
                    "parent_edge_to": target,
                    "segment_index": index,
                    "segment_count": 2,
                    "verification": verification(parent_id, index),
                }
            )
            if distance > MAX_GENERAL_EDGE_M + 1e-9:
                raise RuntimeError(f"split segment still exceeds 8 m: {segment_id}")
            new_edges.append(child)
            new_routes[segment_id] = piece[1:-1]
            segment_distances.append(distance)

        # If a future target is an emergency edge, keep risk-zone references complete.
        for risk_zone in data["risk_zones"].get("risk_zones", []):
            affected = risk_zone.get("affected_edges", [])
            if parent_id in affected and segment_ids[1] not in affected:
                affected.append(segment_ids[1])

        split_records.append(
            {
                "parent_edge_id": parent_id,
                "parent_from": source,
                "parent_to": target,
                "parent_distance_m": edge["distance_m"],
                "inserted_node_id": waypoint_id,
                "display_number": display_number,
                "pixel_xy": pixel,
                "segment_edge_ids": segment_ids,
                "segment_distances_m": segment_distances,
            }
        )

    nav["nodes"].extend(new_nodes)
    nav["edges"] = new_edges
    data["routes"]["edge_routes"] = new_routes
    split_provenance = {
        "method": "split_selected_general_edges_at_polyline_arc_length_midpoint",
        "max_general_edge_m": MAX_GENERAL_EDGE_M,
        "confirmed_on": reviewed_at,
        "parent_edge_ids": list(TARGET_EDGE_IDS),
        "inserted_node_ids": [item["id"] for item in new_nodes],
        "human_review_status": "split_policy_confirmed_locations_pending",
    }
    provenance["general_edge_8m_split"] = split_provenance
    data["routes"]["general_edge_8m_split"] = split_provenance

    long_general = [
        edge
        for edge in new_edges
        if edge.get("general_navigation_allowed") is not False
        and edge.get("routing_scope") != "medication_delivery_only"
        and float(edge["distance_m"]) > MAX_GENERAL_EDGE_M + 1e-9
    ]
    if long_general:
        raise RuntimeError(
            "general edges still exceed 8 m: "
            + ", ".join(edge["id"] for edge in long_general)
        )

    for name, path in paths.items():
        if name != "stops":
            save(path, data[name])
    safe_stop_ids = {item["node_id"] for item in data["stops"].get("stops", [])}
    write_scene(
        sandbox / "scene/roboguard_hospital_sandbox.usda",
        nav,
        data["coords"],
        safe_stop_ids,
    )
    report = {
        "status": "applied",
        "policy": {"general_navigation_max_edge_m": MAX_GENERAL_EDGE_M},
        "confirmed_on": reviewed_at,
        "node_count_before": len(nav["nodes"]) - len(new_nodes),
        "node_count_after": len(nav["nodes"]),
        "edge_count_before": len(nav["edges"]) - len(TARGET_EDGE_IDS),
        "edge_count_after": len(nav["edges"]),
        "inserted_node_count": len(new_nodes),
        "split_parent_edge_count": len(split_records),
        "maximum_general_edge_m_after": max(
            float(edge["distance_m"])
            for edge in new_edges
            if edge.get("general_navigation_allowed") is not False
            and edge.get("routing_scope") != "medication_delivery_only"
        ),
        "task_only_edges_unchanged": sum(
            edge.get("routing_scope") == "medication_delivery_only"
            for edge in new_edges
        ),
        "splits": split_records,
    }
    save(
        project_root
        / "artifacts/map_review/schema_v2/long_edge_split_apply_report.json",
        report,
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--reviewed-at", default=date.today().isoformat())
    args = parser.parse_args()
    result = apply(args.project_root.resolve(), args.reviewed_at)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
