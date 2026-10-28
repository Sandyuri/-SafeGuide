"""Validate the synchronized RoboGuard hospital navigation sandbox."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
import json
import math
from pathlib import Path
import sys


IMAGE_WIDTH = 6709
IMAGE_HEIGHT = 6702
X_MIN, X_MAX = -35.468066, 22.740802
Y_MIN, Y_MAX = -28.238046, 30.183436


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def pixel_to_usd(point):
    x, y = point
    return (
        X_MIN + x / IMAGE_WIDTH * (X_MAX - X_MIN),
        Y_MAX - y / IMAGE_HEIGHT * (Y_MAX - Y_MIN),
        0.0,
    )


def point_in_polygon(point, polygon):
    x, y = point
    inside = False
    for index, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(index + 1) % len(polygon)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True, help="navigation_sandbox directory")
    parser.add_argument("--usd", type=Path, help="also verify every pose/polyline against the OpenUSD collision grid")
    args = parser.parse_args()
    root = args.root.resolve()
    failures = []

    def check(condition, message):
        if not condition:
            failures.append(message)

    nav = load(root / "maps/hospital_1f.navigation.json")
    coords = load(root / "maps/hospital_1f.coordinate_mapping.json")
    entrances = load(root / "maps/hospital_1f.entrance_mapping.json")
    anchors = load(root / "artifacts/floorplan/hospital_1f.floorplan_anchor_mapping.json")
    routes = load(root / "artifacts/floorplan/hospital_1f.edge_routes.json")
    registry = load(root / "semantics/location_registry.json")
    zones = load(root / "semantics/access_zones.json")
    stops = load(root / "semantics/safe_stops.json")
    route_semantics = load(root / "semantics/route_semantics.json")
    task_destinations = load(root / "semantics/task_destinations.json")
    safegate_allowlist = load(root / "semantics/safegate_first_floor_allowlist.json")
    no_through_path = root / "maps/hospital_1f.no_through_zones.json"
    no_through_zones = load(no_through_path)["zones"]
    check(route_semantics.get("map_id") == nav.get("map_id"), "route semantics map_id differs")

    nodes = {item["id"]: item for item in nav.get("nodes", [])}
    check(len(nodes) == len(nav.get("nodes", [])) and bool(nodes), "nodes must be unique and non-empty")
    node_ids = set(nodes)
    semantic_node_ids = set(route_semantics.get("nodes", {}))
    check(semantic_node_ids <= node_ids, f"route semantics reference unknown nodes: {sorted(semantic_node_ids - node_ids)}")
    task_node_ids = {
        item.get("node_id") for item in task_destinations.get("destinations", [])
    }
    check(None not in task_node_ids, "task destination is missing node_id")
    check(task_node_ids <= node_ids, f"task destinations reference unknown nodes: {sorted(task_node_ids - node_ids)}")
    check(
        all(nodes[node_id].get("node_class") == "task_location" for node_id in task_node_ids if node_id in nodes),
        "task destinations must use node_class=task_location",
    )
    safegate_node_ids = set(
        safegate_allowlist.get("reference_resolution", {})
        .get("location_aliases", {})
        .values()
    )
    check(not (task_node_ids & safegate_node_ids), "task-only node is exposed by the SafeGate location allowlist")
    structure = {node_id for node_id, node in nodes.items() if node.get("node_class") == "navigation_structure"}
    task_route_only = {
        node_id
        for node_id, node in nodes.items()
        if node.get("general_navigation_allowed") is False
        or node.get("routing_scope") == "medication_delivery_only"
    }
    business = node_ids - structure - task_route_only
    new_corridors = {node_id for node_id in structure if node_id.startswith("nav-f1-corridor-")}
    check(len(new_corridors) == 23, f"expected 23 new corridor nodes, got {len(new_corridors)}")
    check(100 <= len(nodes) <= 150, f"experiment map must contain 100-150 nodes, got {len(nodes)}")
    check(64 <= len(structure) <= 114, f"unexpected structure node count: {len(structure)}")

    coordinate_ids = set(coords.get("node_coordinates", {}))
    anchor_by_id = {item["node_id"]: item for item in anchors.get("nodes", [])}
    display_numbers = [item.get("display_number") for item in anchors.get("nodes", [])]
    check(
        len(display_numbers) == len(set(display_numbers)),
        "floor-plan display numbers must be unique",
    )
    entry_by_id = {item["node_id"]: item for item in entrances.get("entries", [])}
    registry_ids = {item["node_id"] for item in registry.get("locations", [])}
    check(coordinate_ids == node_ids, f"coordinate node set differs: {sorted(node_ids ^ coordinate_ids)}")
    check(set(anchor_by_id) == node_ids, f"anchor node set differs: {sorted(node_ids ^ set(anchor_by_id))}")
    check(set(entry_by_id) == node_ids, f"entrance node set differs: {sorted(node_ids ^ set(entry_by_id))}")
    check(registry_ids == node_ids, f"registry node set differs: {sorted(node_ids ^ registry_ids)}")

    expected_operator_pixels = {
        "sg-f1-outpatient-waiting": [3120, 1466],
        "sg-f1-functional-exam-waiting": [2260, 5097],
    }
    for node_id, expected_pixel in expected_operator_pixels.items():
        check(anchor_by_id.get(node_id, {}).get("floorplan_pixel_xy") == expected_pixel, f"operator-corrected floor-plan coordinate changed: {node_id}")
    check(
        anchor_by_id.get("sg-f1-outpatient-waiting", {}).get("floorplan_pixel_xy")
        == anchor_by_id.get("nav-f1-corridor-23", {}).get("floorplan_pixel_xy"),
        "display nodes 14 and 70 must remain colocated",
    )

    for node_id in node_ids & coordinate_ids & set(anchor_by_id):
        xyz = coords["node_coordinates"][node_id].get("usd_xyz_m")
        check(isinstance(xyz, list) and len(xyz) == 3, f"invalid USD coordinate: {node_id}")
        if isinstance(xyz, list) and len(xyz) == 3:
            projected = pixel_to_usd(anchor_by_id[node_id]["floorplan_pixel_xy"])
            check(math.dist(xyz, projected) <= 0.012, f"pixel/USD mismatch: {node_id}")
        if node_id in entry_by_id and isinstance(xyz, list):
            check(entry_by_id[node_id].get("approach_pose", {}).get("usd_xyz_m") == xyz, f"entrance approach mismatch: {node_id}")
        point = anchor_by_id[node_id]["floorplan_pixel_xy"]
        for zone in no_through_zones:
            allowed_task_nodes = set(zone.get("authorized_task_destination_node_ids", []))
            check(
                not point_in_polygon(point, zone["polygon_pixel_xy"])
                or node_id in allowed_task_nodes,
                f"node in no-through room: {node_id} zone={zone['id']}",
            )

    edges = nav.get("edges", [])
    edge_ids = [edge.get("id") for edge in edges]
    check(len(set(edge_ids)) == len(edge_ids), "edge IDs are not unique")
    edge_routes = routes.get("edge_routes", {})
    check(set(edge_routes) == set(edge_ids), f"edge route set differs: {sorted(set(edge_routes) ^ set(edge_ids))}")
    pairs = set()
    parent_pairs = set()
    graph = defaultdict(list)
    degrees = Counter()
    general_degrees = Counter()
    for edge in edges:
        edge_id = edge.get("id")
        left, right = edge.get("from"), edge.get("to")
        check(left in nodes and right in nodes and left != right, f"invalid edge endpoints: {edge_id}")
        pair = frozenset((left, right))
        check(pair not in pairs, f"duplicate undirected edge: {edge_id}")
        pairs.add(pair)
        parent_from = edge.get("parent_edge_from")
        parent_to = edge.get("parent_edge_to")
        if parent_from and parent_to:
            parent_pairs.add(frozenset((parent_from, parent_to)))
        check(edge.get("bidirectional") is True, f"edge is not bidirectional: {edge_id}")
        if edge.get("routing_scope") == "medication_delivery_only":
            check(
                edge.get("general_navigation_allowed") is False,
                f"task route edge is exposed to general navigation: {edge_id}",
            )
            check(
                edge.get("required_task_kind") == "MEDICATION_DELIVERY",
                f"task route edge has the wrong task kind: {edge_id}",
            )
        if left not in nodes or right not in nodes:
            continue
        degrees[left] += 1
        degrees[right] += 1
        if edge.get("general_navigation_allowed") is not False:
            general_degrees[left] += 1
            general_degrees[right] += 1
        graph[left].append(right)
        graph[right].append(left)
        points = [anchor_by_id[left]["floorplan_pixel_xy"], *edge_routes.get(edge_id, []), anchor_by_id[right]["floorplan_pixel_xy"]]
        usd_points = [pixel_to_usd(point) for point in points]
        for zone in no_through_zones:
            polygon = zone["polygon_pixel_xy"]
            crosses = False
            for first, second in zip(points, points[1:]):
                samples = max(1, math.ceil(math.dist(first, second) / 5))
                if any(point_in_polygon((first[0] + (second[0] - first[0]) * step / samples,
                                         first[1] + (second[1] - first[1]) * step / samples), polygon)
                       for step in range(samples + 1)):
                    crosses = True
                    break
            if crosses:
                allowed_edges = set(zone.get("authorized_task_edge_ids", []))
                allowed_nodes = set(zone.get("authorized_task_destination_node_ids", []))
                authorized_task_edge = (
                    edge_id in allowed_edges
                    and edge.get("access_zone") != "public"
                    and bool({left, right} & allowed_nodes)
                )
                check(
                    authorized_task_edge,
                    f"edge enters no-through room: {edge_id} zone={zone['id']}",
                )
        calculated = sum(math.dist(a, b) for a, b in zip(usd_points, usd_points[1:]))
        distance = edge.get("distance_m")
        check(isinstance(distance, (int, float)) and distance > 0, f"invalid distance: {edge_id}")
        if isinstance(distance, (int, float)):
            check(abs(distance - calculated) <= 0.025, f"distance/polyline mismatch: {edge_id} stored={distance}, calculated={calculated:.3f}")
            straight = math.dist(coords["node_coordinates"][left]["usd_xyz_m"], coords["node_coordinates"][right]["usd_xyz_m"])
            check(distance + 0.02 >= straight, f"distance shorter than endpoints: {edge_id}")
            if (
                edge.get("general_navigation_allowed") is not False
                and edge.get("routing_scope") != "medication_delivery_only"
            ):
                check(distance <= 8.0, f"general-navigation edge exceeds 8 m: {edge_id} distance={distance}")

    edge_by_id = {edge["id"]: edge for edge in edges}
    for destination in task_destinations.get("destinations", []):
        if destination.get("route_graph_scope") != "medication_delivery_only":
            continue
        destination_id = destination.get("node_id")
        check(
            destination.get("general_navigation_allowed") is False,
            f"task destination is exposed to general navigation: {destination_id}",
        )
        check(
            destination.get("safegate_exposed") is False,
            f"task destination is exposed to SafeGate: {destination_id}",
        )
        declared_node_ids = set(destination.get("task_route_node_ids", [])) | {destination_id}
        declared_edge_ids = set(destination.get("task_route_edge_ids", []))
        check(
            declared_node_ids <= node_ids,
            f"task route references unknown nodes: {sorted(declared_node_ids - node_ids)}",
        )
        check(
            declared_edge_ids <= set(edge_ids),
            f"task route references unknown edges: {sorted(declared_edge_ids - set(edge_ids))}",
        )
        for node_id in declared_node_ids & node_ids:
            check(
                nodes[node_id].get("routing_scope") == "medication_delivery_only"
                and nodes[node_id].get("general_navigation_allowed") is False,
                f"medication-delivery node is exposed to general navigation: {node_id}",
            )
        for edge_id in declared_edge_ids & set(edge_ids):
            edge = edge_by_id[edge_id]
            check(
                edge.get("routing_scope") == "medication_delivery_only"
                and edge.get("general_navigation_allowed") is False
                and edge.get("required_task_kind") == "MEDICATION_DELIVERY",
                f"medication-delivery edge lacks task-only isolation: {edge_id}",
            )
        route_ids = set()
        for variant in destination.get("route_variants", []):
            route_id = variant.get("route_id")
            check(route_id not in route_ids, f"duplicate task route variant: {route_id}")
            route_ids.add(route_id)
            node_sequence = variant.get("node_sequence", [])
            edge_sequence = variant.get("edge_sequence", [])
            check(
                len(node_sequence) == len(edge_sequence) + 1,
                f"task route sequence length mismatch: {route_id}",
            )
            check(
                bool(node_sequence)
                and node_sequence[0] == variant.get("entry_navigation_node_id")
                and node_sequence[-1] == destination_id,
                f"task route endpoints are invalid: {route_id}",
            )
            for left, edge_id, right in zip(node_sequence, edge_sequence, node_sequence[1:]):
                edge = edge_by_id.get(edge_id)
                check(
                    edge_id in declared_edge_ids
                    and edge is not None
                    and {edge.get("from"), edge.get("to")} == {left, right},
                    f"task route step does not match edge endpoints: {route_id}/{edge_id}",
                )
        check(
            route_ids
            == {
                "pharmacy_delivery_north_route",
                "pharmacy_delivery_south_route",
                "pharmacy_delivery_north_east_route",
                "pharmacy_delivery_south_west_route",
                "pharmacy_delivery_south_lower_route",
            },
            f"unexpected medication-delivery route variants: {sorted(route_ids)}",
        )

    allowed_business_pairs = {
        frozenset(("sg-f1-patient-service-center", "sg-f1-self-service-registration-payment")),
        frozenset(("sg-f1-patient-service-center", "sg-f1-escalator")),
        frozenset(("sg-f1-patient-service-center", "sg-f1-consultation-room")),
        frozenset(("sg-f1-functional-exam-waiting-east", "sg-f1-self-service-report-machine")),
    }
    multi_attached_business = {
        "sg-f1-information-desk": 1,
        "sg-f1-queue-ticket-machine": 2,
        "sg-f1-public-elevator-hall": 2,
        "sg-f1-medication-consultation": 1,
        "sg-f1-consultation-room": 3,
        "sg-f1-outpatient-waiting": 2,
        "sg-f1-orthopedic-rehabilitation": 2,
        "sg-f1-spinal-cord-rehabilitation": 2,
        "sg-f1-self-service-registration-payment": 3,
        "sg-f1-neurological-rehabilitation": 2,
        "sg-f1-patient-service-center": 5,
        "sg-f1-escalator": 3,
        "sg-f1-nebulization-room": 2,
        "sg-f1-functional-exam-waiting-east": 4,
        "sg-f1-self-service-report-machine": 2,
        "sg-f1-notice-board": 2,
        "sg-f1-pharmacy": 2,
        "sg-f1-restroom-lower-left": 2,
        "sg-f1-cardiopulmonary-rehabilitation": 2,
        "sg-f1-mental-health": 2,
    }
    for node_id in business:
        expected_degree = multi_attached_business.get(node_id, 1)
        check(degrees[node_id] == expected_degree, f"unexpected business degree: {node_id} degree={degrees[node_id]} expected={expected_degree}")
        if graph[node_id]:
            check(
                all(
                    neighbor in structure
                    or frozenset((node_id, neighbor)) in allowed_business_pairs
                    for neighbor in graph[node_id]
                ),
                f"business location has an unauthorized business neighbor: {node_id}",
            )
    required_direct_pairs = {
        frozenset(("nav-f1-waypoint-001", "nav-f1-spine-01")),
        frozenset(("sg-f1-queue-ticket-machine", "nav-f1-spine-01")),
        frozenset(("sg-f1-public-elevator-hall", "nav-f1-corridor-13")),
        frozenset(("sg-f1-medication-consultation", "nav-f1-waypoint-019")),
        frozenset(("sg-f1-consultation-room", "nav-f1-corridor-09")),
        frozenset(("sg-f1-outpatient-waiting", "nav-f1-spine-04")),
        frozenset(("sg-f1-orthopedic-rehabilitation", "nav-f1-corridor-04")),
        frozenset(("sg-f1-notice-board", "nav-f1-spine-04")),
        frozenset(("sg-f1-spinal-cord-rehabilitation", "nav-f1-corridor-03")),
        frozenset(("nav-f1-corridor-13", "nav-f1-spine-07")),
        frozenset(("nav-f1-corridor-24", "nav-f1-spine-07")),
        frozenset(("nav-f1-corridor-24", "nav-f1-spine-06")),
        frozenset(("nav-f1-corridor-24", "nav-f1-corridor-17")),
        frozenset(("nav-f1-corridor-24", "nav-f1-corridor-22")),
        frozenset(("sg-f1-self-service-registration-payment", "nav-f1-spine-08")),
        frozenset(("sg-f1-neurological-rehabilitation", "nav-f1-corridor-02")),
        frozenset(("sg-f1-patient-service-center", "sg-f1-self-service-registration-payment")),
        frozenset(("sg-f1-patient-service-center", "sg-f1-escalator")),
        frozenset(("sg-f1-patient-service-center", "sg-f1-consultation-room")),
        frozenset(("sg-f1-nebulization-room", "nav-f1-corridor-17")),
        frozenset(("sg-f1-patient-service-center", "nav-f1-corridor-24")),
        frozenset(("sg-f1-functional-exam-waiting-east", "nav-f1-corridor-24")),
        frozenset(("sg-f1-functional-exam-waiting-east", "nav-f1-waypoint-052")),
        frozenset(("sg-f1-pharmacy", "nav-f1-waypoint-018")),
        frozenset(("sg-f1-functional-exam-waiting-east", "sg-f1-self-service-report-machine")),
        frozenset(("sg-f1-notice-board", "nav-f1-waypoint-013")),
        frozenset(("nav-f1-waypoint-015", "nav-f1-waypoint-016")),
        frozenset(("nav-f1-waypoint-016", "nav-f1-waypoint-024")),
        frozenset(("sg-f1-restroom-lower-left", "nav-f1-spine-07")),
        frozenset(("nav-f1-corridor-22", "nav-f1-spine-06")),
        frozenset(("nav-f1-corridor-16", "nav-f1-corridor-22")),
        frozenset(("nav-f1-corridor-08", "sg-f1-cardiopulmonary-rehabilitation")),
        frozenset(("nav-f1-waypoint-063", "sg-f1-escalator")),
        frozenset(("nav-f1-corridor-07", "sg-f1-mental-health")),
    }
    check(
        required_direct_pairs <= pairs | parent_pairs,
        "one or more operator-confirmed direct routes are missing",
    )
    for node_id in sorted(node_ids):
        if node_id.startswith("nav-f1-waypoint-"):
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
                general_degrees[node_id] == expected_degree,
                f"derived waypoint has unexpected general-navigation degree: {node_id} "
                f"degree={general_degrees[node_id]} expected={expected_degree}",
            )
    forbidden_direct_pairs = {
        frozenset(("sg-f1-registration-payment-counter", "nav-f1-corridor-04")),
        frozenset(("nav-f1-spine-02", "nav-f1-corridor-08")),
        frozenset(("sg-f1-information-desk", "nav-f1-spine-01")),
        frozenset(("sg-f1-information-desk", "nav-f1-corridor-02")),
    }
    check(not (forbidden_direct_pairs & pairs), "registration/payment counter has a forbidden direct link to display node 51")
    actual_business_pairs = {
        frozenset((left, right))
        for left in graph
        for right in graph[left]
        if left in business and right in business
    }
    check(actual_business_pairs <= allowed_business_pairs, "unauthorized business-to-business edge exists")
    check("nav-f1-corridor-10" not in node_ids and "sg-f1-rest-area" not in node_ids, "removed nodes remain in navigation graph")
    colocated_pair = ("sg-f1-self-service-registration-payment-south", "sg-f1-stairs")
    check(
        coords["node_coordinates"][colocated_pair[0]]["usd_xyz_m"] == coords["node_coordinates"][colocated_pair[1]]["usd_xyz_m"],
        "display nodes 24 and 29 must share the same coordinate",
    )
    check(
        set(graph[colocated_pair[0]]) == set(graph[colocated_pair[1]]) == {"nav-f1-spine-07"},
        "display nodes 24 and 29 must share the same corridor connection",
    )

    start = "nav-f1-spine-04"
    seen = {start}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for neighbor in graph[current]:
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append(neighbor)
    check(seen == node_ids, f"unreachable nodes from lobby: {sorted(node_ids - seen)}")

    actual_stops = {item["node_id"] for item in stops.get("stops", [])}
    check(actual_stops <= node_ids, f"safe stops reference unknown nodes: {sorted(actual_stops - node_ids)}")
    for stop in stops.get("stops", []):
        node_id = stop["node_id"]
        check(stop.get("usd_xyz_m") == coords["node_coordinates"][node_id].get("usd_xyz_m"), f"safe-stop coordinate mismatch: {node_id}")

    zone_membership = Counter()
    for zone in zones.get("zones", []):
        for node_id in zone.get("node_ids", []):
            check(node_id in nodes, f"zone references unknown node: {node_id}")
            zone_membership[node_id] += 1
            if node_id in nodes:
                check(nodes[node_id].get("access_zone") == zone.get("id"), f"zone mismatch: {node_id}")
    check(all(zone_membership[node_id] == 1 for node_id in node_ids), "every node must appear in exactly one access zone")

    aliases = {}
    for item in registry.get("locations", []):
        for alias in [item.get("canonical_name"), *item.get("aliases", [])]:
            check(isinstance(alias, str) and bool(alias.strip()), f"empty location alias at {item.get('node_id')}")
            key = alias.strip().casefold() if isinstance(alias, str) else ""
            check(key not in aliases or aliases[key] == item["node_id"], f"ambiguous alias: {alias}")
            aliases[key] = item["node_id"]

    if args.usd:
        try:
            import rebuild_point_navigation_topology as router
        except ImportError as error:
            failures.append(f"cannot import USD router: {error}")
        else:
            blocked, mesh_count, triangle_count = router.build_occupancy(args.usd.resolve(), no_through_path)
            for node_id in node_ids:
                cell = router.cell_of(*coords["node_coordinates"][node_id]["usd_xyz_m"][:2])
                check(cell not in blocked, f"node collides with USD geometry: {node_id}")
            for edge in edges:
                points = [anchor_by_id[edge["from"]]["floorplan_pixel_xy"], *edge_routes[edge["id"]], anchor_by_id[edge["to"]]["floorplan_pixel_xy"]]
                cells = [router.cell_of(*pixel_to_usd(point)[:2]) for point in points]
                for index, (left, right) in enumerate(zip(cells, cells[1:])):
                    check(router.line_clear(left, right, blocked), f"edge intersects USD collision: {edge['id']} segment={index}")
            print(f"USD collision audit: meshes={mesh_count}, triangles={triangle_count}")

    print(
        f"nodes={len(nodes)} business={len(business)} structure={len(structure)} "
        f"task_route_only={len(task_route_only)} edges={len(edges)} safe_stops={len(actual_stops)}"
    )
    if failures:
        print(f"VALIDATION FAILED ({len(failures)} errors)")
        for failure in failures:
            print(" -", failure)
        return 1
    print("VALIDATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
