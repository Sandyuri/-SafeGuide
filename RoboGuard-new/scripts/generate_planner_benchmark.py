"""Generate 120 deterministic offline hospital navigation scenarios."""

from __future__ import annotations

import argparse
from heapq import heappop, heappush
import json
import math
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT_ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from roboguard.medical.providers import NavigationSandboxSceneProvider


def shortest_path(regions, source, destination, *, blocked=()):
    blocked = set(blocked)
    if source in blocked or destination in blocked:
        return ()
    queue = [(0.0, (source,), source)]
    best = {source: 0.0}
    while queue:
        distance, path, node = heappop(queue)
        if distance != best[node]:
            continue
        if node == destination:
            return path
        for neighbor in sorted(regions[node]["connections"]):
            if neighbor in blocked:
                continue
            edge = regions[node].get("connection_costs", {}).get(neighbor)
            if isinstance(edge, (int, float)) and not isinstance(edge, bool) and edge > 0:
                candidate = distance + float(edge)
                if candidate < best.get(neighbor, math.inf):
                    best[neighbor] = candidate
                    heappush(queue, (candidate, path + (neighbor,), neighbor))
    return ()


def cyclic_pairs(nodes, count, offset):
    return [(nodes[index % len(nodes)], nodes[(index + offset) % len(nodes)]) for index in range(count)]


def dynamic_cases(regions, nodes, event_type, count):
    """Find routes where the event affects the baseline and a detour exists."""

    cases = []
    for source in nodes:
        for destination in nodes:
            if source == destination:
                continue
            path = shortest_path(regions, source, destination)
            for affected in path[1:-1]:
                region = regions[affected]
                if event_type == "ELEVATOR_UNAVAILABLE" and "ELEVATOR" not in region["type"].upper():
                    continue
                if (
                    event_type == "EMERGENCY_CORRIDOR_ACTIVE"
                    and "EMERGENCY_CORRIDOR" not in region.get("semantic_tags", ())
                ):
                    continue
                if not shortest_path(regions, source, destination, blocked=(affected,)):
                    continue
                cases.append((source, destination, affected))
                break
            if len(cases) == count:
                return cases
    if not cases:
        raise RuntimeError(f"no valid dynamic benchmark case for {event_type}")
    # A small map can have fewer than five elevator/emergency examples. Cycling
    # keeps category counts fixed while every scenario remains causally valid.
    return [cases[index % len(cases)] for index in range(count)]


def generate(scene):
    regions = scene.data["regions"]
    zones = scene.data["access_zones"]
    public = sorted(
        node for node, region in regions.items()
        if region.get("navigation_status") == "OPEN"
        and not zones[region["access_zone_id"]]["required_permissions"]
        and "STAIR" not in region["type"]
    )
    restricted = sorted(
        node for node, region in regions.items()
        if zones[region["access_zone_id"]]["required_permissions"]
    )
    if len(public) < 12 or not restricted:
        raise RuntimeError("benchmark generation requires public and restricted nodes")
    scenarios = []

    for index, (source, destination) in enumerate(cyclic_pairs(public, 40, 7), 1):
        scenarios.append({
            "id": f"normal-{index:03d}", "category": "normal",
            "source": source, "ordered_destinations": [destination],
            "accessibility_needs": [], "revoke_permissions": [], "events": [],
            "expected_decision": "ALLOW",
        })
    for index, (source, destination) in enumerate(cyclic_pairs(public, 20, 11), 1):
        scenarios.append({
            "id": f"accessible-{index:03d}", "category": "accessible",
            "source": source, "ordered_destinations": [destination],
            "accessibility_needs": ["WHEELCHAIR_ACCESSIBLE", "STEP_FREE"],
            "revoke_permissions": [], "events": [],
            "expected_decision": "ALLOW",
        })
    for index in range(20):
        source = public[index % len(public)]
        via = public[(index + 5) % len(public)]
        destination = public[(index + 13) % len(public)]
        scenarios.append({
            "id": f"multistage-{index + 1:03d}", "category": "multistage",
            "source": source, "ordered_destinations": [via, destination],
            "accessibility_needs": [], "revoke_permissions": [], "events": [],
            "expected_decision": "ALLOW",
        })
    for index in range(20):
        source = public[(index * 2) % len(public)]
        destination = restricted[index % len(restricted)]
        required = zones[regions[destination]["access_zone_id"]]["required_permissions"]
        scenarios.append({
            "id": f"permission-{index + 1:03d}", "category": "permission",
            "source": source, "ordered_destinations": [destination],
            "accessibility_needs": [], "revoke_permissions": list(required), "events": [],
            "expected_decision": "REJECT",
        })
    dynamic_specs = []
    # Elevator-unavailable remains covered by a focused test. On this one-floor
    # map the elevator hall is never an interior node of a replaceable shortest
    # route, so including it here would create an artificial benchmark case.
    for event_type in (
        "NODE_CLOSED", "CONGESTION", "STATUS_EXPIRED", "EMERGENCY_CORRIDOR_ACTIVE"
    ):
        dynamic_specs.extend(
            (event_type, *case) for case in dynamic_cases(regions, public, event_type, 5)
        )
    for index, (event_type, source, destination, affected) in enumerate(dynamic_specs, 1):
        event = {"id": f"dynamic-event-{index:03d}", "type": event_type, "region_id": affected}
        if event_type == "CONGESTION":
            event["severity"] = 0.8
        scenarios.append({
            "id": f"dynamic-{index:03d}", "category": "dynamic",
            "source": source, "ordered_destinations": [destination],
            "accessibility_needs": [], "revoke_permissions": [], "events": [event],
            "expected_decision": "ALLOW",
        })
    assert len(scenarios) == 120
    return {
        "schema_version": "1.0",
        "description": "Deterministic offline comparison; no LLM/API calls.",
        "counts": {"normal": 40, "accessible": 20, "multistage": 20, "permission": 20, "dynamic": 20},
        "scenarios": scenarios,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "benchmarks/scenarios.json")
    args = parser.parse_args()
    provider = NavigationSandboxSceneProvider(
        PROJECT_ROOT / "data/fixtures/medical/hospital_1f.synthetic.json",
        PROJECT_ROOT / "navigation_sandbox",
        PROJECT_ROOT / "data/navigation/hospital_1f.bindings.json",
    )
    document = generate(provider.load_scene())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"generated {len(document['scenarios'])} scenarios: {args.output}")


if __name__ == "__main__":
    main()
