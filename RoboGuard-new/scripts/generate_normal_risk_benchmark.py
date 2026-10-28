"""Generate a focused benchmark with 60 normal and 60 risk-detour cases."""

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


COUNT_PER_CATEGORY = 60
MAX_DISTANCE_RATIO = 1.25
EXCLUDED_POLICIES = {"FORBID", "EMERGENCY_ONLY_ACTIVE", "MEDICATION_DELIVERY_ONLY"}


def _fixed_risk_weights() -> dict[str, float]:
    payload = json.loads(
        (PROJECT_ROOT / "config/route_validation_rules.json").read_text(
            encoding="utf-8-sig"
        )
    )
    return {
        str(key).upper(): float(value)
        for key, value in payload["risk"]["fixed_node_risk"].items()
    }


def _allowed_nodes(scene) -> set[str]:
    result = set()
    for node_id, region in scene.data["regions"].items():
        zone = scene.data["access_zones"][region["access_zone_id"]]
        if region.get("navigation_status") != "OPEN":
            continue
        if zone["id"] != "public" or zone.get("required_permissions"):
            continue
        if region.get("route_policy", "ALLOW_TRANSIT") in EXCLUDED_POLICIES:
            continue
        result.add(node_id)
    return result


def _searchable_general_nodes(scene) -> set[str]:
    """Nodes visible to legacy shortest-path planners before role validation."""

    result = set()
    for node_id, region in scene.data["regions"].items():
        if region.get("navigation_status") != "OPEN":
            continue
        if region.get("route_policy", "ALLOW_TRANSIT") in EXCLUDED_POLICIES:
            continue
        result.add(node_id)
    return result


def _endpoints(scene, allowed: set[str]) -> list[str]:
    result = []
    for node_id in sorted(allowed):
        region = scene.data["regions"][node_id]
        if node_id.startswith("nav-f1-waypoint-"):
            continue
        if "STAIR" in region["type"].upper() or "ESCALATOR" in region["type"].upper():
            continue
        result.append(node_id)
    return result


def _transition_risk(regions, left: str, right: str, distance: float, fixed) -> float:
    rates = regions[left].get("connection_risk_rates", {}).get(right, {})
    risk = sum(float(value) * distance for value in rates.values())
    tags = {str(value).upper() for value in regions[right].get("semantic_tags", ())}
    risk += sum(weight for tag, weight in fixed.items() if tag in tags)
    return risk


def _best_path(
    regions,
    allowed: set[str],
    source: str,
    destination: str,
    fixed: dict[str, float],
    *,
    risk_first: bool,
):
    start_score = (0.0, 0.0)
    queue = [(start_score, (source,), source, 0.0, 0.0)]
    best = {source: start_score}
    while queue:
        score, path, node, distance, risk = heappop(queue)
        if score != best.get(node):
            continue
        if node == destination:
            return path, distance, risk
        for neighbor in sorted(regions[node]["connections"]):
            if neighbor not in allowed:
                continue
            policy = regions[neighbor].get("route_policy", "ALLOW_TRANSIT")
            if policy == "DESTINATION_ONLY" and neighbor != destination:
                continue
            edge = regions[node].get("connection_costs", {}).get(neighbor)
            if (
                isinstance(edge, bool)
                or not isinstance(edge, (int, float))
                or not math.isfinite(edge)
                or edge <= 0
            ):
                continue
            next_distance = distance + float(edge)
            next_risk = risk + _transition_risk(
                regions, node, neighbor, float(edge), fixed
            )
            next_score = (
                (next_risk, next_distance)
                if risk_first
                else (next_distance, next_risk)
            )
            if next_score < best.get(neighbor, (math.inf, math.inf)):
                best[neighbor] = next_score
                heappush(
                    queue,
                    (next_score, path + (neighbor,), neighbor, next_distance, next_risk),
                )
    return (), math.inf, math.inf


def _scenario(identifier: str, category: str, source: str, destination: str, reference):
    return {
        "id": identifier,
        "category": category,
        "source": source,
        "ordered_destinations": [destination],
        "accessibility_needs": [],
        "revoke_permissions": [],
        "events": [],
        "expected_decision": "ALLOW",
        "reference": reference,
    }


def _evenly_spaced(values, count):
    """Select deterministic quantiles without favoring one end of the pool."""

    if count == 1:
        return [values[0]]
    return [values[round(index * (len(values) - 1) / (count - 1))] for index in range(count)]


def generate(scene):
    regions = scene.data["regions"]
    allowed = _allowed_nodes(scene)
    searchable = _searchable_general_nodes(scene)
    endpoints = _endpoints(scene, allowed)
    fixed = _fixed_risk_weights()
    normal_candidates = []
    risk_candidates = []

    for source in endpoints:
        for destination in endpoints:
            if source == destination:
                continue
            shortest_path, shortest_distance, shortest_risk = _best_path(
                regions, allowed, source, destination, fixed, risk_first=False
            )
            if not shortest_path or shortest_distance < 5.0:
                continue
            unrestricted_path, unrestricted_distance, _ = _best_path(
                regions, searchable, source, destination, fixed, risk_first=False
            )
            # Do not admit a case where the legacy shortest route is an invalid
            # shortcut through a non-public or task-specific subgraph. Such a
            # case measures topology isolation rather than route optimization.
            if (
                not unrestricted_path
                or any(node not in allowed for node in unrestricted_path)
                or abs(unrestricted_distance - shortest_distance) > 1e-9
            ):
                continue
            safe_path, safe_distance, safe_risk = _best_path(
                regions, allowed, source, destination, fixed, risk_first=True
            )
            if not safe_path:
                continue
            reference = {
                "shortest_distance_m": round(shortest_distance, 6),
                "shortest_risk": round(shortest_risk, 6),
                "shortest_path": list(shortest_path),
                "lowest_risk_distance_m": round(safe_distance, 6),
                "lowest_risk": round(safe_risk, 6),
                "lowest_risk_path": list(safe_path),
                "max_distance_ratio": MAX_DISTANCE_RATIO,
            }
            if shortest_risk <= 1e-9:
                normal_candidates.append(
                    (shortest_distance, source, destination, reference)
                )
                continue
            ratio = safe_distance / shortest_distance
            if safe_risk < shortest_risk - 1e-9 and ratio <= MAX_DISTANCE_RATIO + 1e-9:
                risk_candidates.append(
                    (
                        -(shortest_risk - safe_risk),
                        ratio,
                        source,
                        destination,
                        reference,
                    )
                )

    if len(normal_candidates) < COUNT_PER_CATEGORY:
        raise RuntimeError(
            f"only {len(normal_candidates)} valid normal cases; need {COUNT_PER_CATEGORY}"
        )
    if len(risk_candidates) < COUNT_PER_CATEGORY:
        raise RuntimeError(
            f"only {len(risk_candidates)} valid risk-detour cases; need {COUNT_PER_CATEGORY}"
        )

    # Deterministic quantile sampling prevents cherry-picking only the shortest
    # normal cases or only the largest risk reductions.
    selected_normal = _evenly_spaced(sorted(normal_candidates), COUNT_PER_CATEGORY)
    selected_risk = _evenly_spaced(sorted(risk_candidates), COUNT_PER_CATEGORY)

    scenarios = []
    for index, (_, source, destination, reference) in enumerate(selected_normal, 1):
        scenarios.append(
            _scenario(
                f"normal-focused-{index:03d}",
                "normal",
                source,
                destination,
                reference,
            )
        )
    for index, (_, _, source, destination, reference) in enumerate(selected_risk, 1):
        scenarios.append(
            _scenario(
                f"risk-detour-{index:03d}",
                "risk_detour",
                source,
                destination,
                reference,
            )
        )

    return {
        "schema_version": "1.0",
        "description": (
            "Focused paired comparison: 60 public general-navigation cases and "
            "60 cases where the distance-shortest route has risk and a lower-risk "
            "route exists within a 1.25 distance ratio. Medication-delivery-only "
            "and non-public topology is excluded."
        ),
        "counts": {"normal": COUNT_PER_CATEGORY, "risk_detour": COUNT_PER_CATEGORY},
        "candidate_counts": {
            "normal": len(normal_candidates),
            "risk_detour": len(risk_candidates),
        },
        "scenarios": scenarios,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "benchmarks/normal_risk_scenarios.json",
    )
    args = parser.parse_args()
    provider = NavigationSandboxSceneProvider(
        PROJECT_ROOT / "data/fixtures/medical/hospital_1f.synthetic.json",
        PROJECT_ROOT / "navigation_sandbox",
        PROJECT_ROOT / "data/navigation/hospital_1f.bindings.json",
    )
    document = generate(provider.load_scene())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(document["counts"], ensure_ascii=False))
    print(json.dumps(document["candidate_counts"], ensure_ascii=False))
    print(f"generated {len(document['scenarios'])} scenarios: {args.output}")


if __name__ == "__main__":
    main()
