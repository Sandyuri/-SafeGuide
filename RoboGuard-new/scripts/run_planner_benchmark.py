"""Benchmark all route planners through the production safety pipeline."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from statistics import mean
import sys
from time import perf_counter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT_ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from roboguard.medical.joint_planner import (
    DynamicEventSet,
    route_affected_by_events,
)
from roboguard.medical.providers import NavigationSandboxSceneProvider
from roboguard.medical.safety_barrier import require_execution_permit
from roboguard.medical.scene import MedicalScene
from roboguard.medical.temporal_guard import MedicalTemporalGuard
from roboguard.navigation import NavigationExecutionGuard, build_navigation_view
from roboguard.safegate_adapter import AuthorizedTask
from roboguard.spine_adapter import SpineAction


PLANNERS = ("dijkstra", "astar", "generate_then_check", "joint", "pareto")


def percentile(values, probability):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * probability)))
    return ordered[index]


def scenario_scene(base_scene, scenario):
    data = base_scene.to_dict()
    data["robot_state"]["current_region_id"] = scenario["source"]
    revoked = set(scenario.get("revoke_permissions", ()))
    data["robot_state"]["access_permissions"] = [
        item for item in data["robot_state"]["access_permissions"] if item not in revoked
    ]
    data["snapshot"]["id"] = f"benchmark-{scenario['id']}"
    data["snapshot"]["version"] += 1
    return MedicalScene.from_mapping(data)


def task_for(scene, scenario):
    destinations = list(scenario["ordered_destinations"])
    return AuthorizedTask(
        schema_version="0.2",
        evaluation_id=f"benchmark-eval-{scenario['id']}",
        request_id=f"benchmark-request-{scenario['id']}",
        action_type="NAVIGATE_TO_LOCATION",
        target={"kind": "LOCATION", "id": destinations[-1]},
        resource=None,
        parameters={
            "ordered_destinations": destinations,
            "accessibility_needs": list(scenario.get("accessibility_needs", ())),
            "user_role": "PATIENT",
            "urgency": "ROUTINE",
        },
        authorization_scope="ALLOW_TO_ENTER_PLANNING",
        obligations=("REQUIRE_ROBOGUARD_EXECUTION_CHECK",),
        matched_rules=(),
        policy_version="0.2.2",
        context_id=scene.snapshot_id,
        raw_text="offline benchmark",
    )


def actions_for(view):
    """Build the same authorized SPINE action shape used by the real entry point."""

    destinations = view.payload["task"].get(
        "ordered_destination_region_refs",
        (view.payload["task"]["destination_region_ref"],),
    )
    return [SpineAction("goto", (destination,)) for destination in destinations]


def run_pipeline(*, scene, task, view, planner_name, events):
    """Run planning, independent validation, barrier review and permit checking."""

    started = perf_counter()
    result = NavigationExecutionGuard(
        temporal_guard=MedicalTemporalGuard(mode="off"),
        planner=planner_name,
        events=events,
    ).review_plan(
        scene=scene,
        authorized_task=task,
        planner_view=view,
        actions=actions_for(view),
    )
    elapsed_ms = (perf_counter() - started) * 1000

    route = result.checked_routes[0] if result.checked_routes else None
    permit_issued = False
    if result.allowed and route is not None:
        try:
            require_execution_permit(
                route,
                expected_plan_version=events.plan_version,
            )
        except ValueError:
            permit_issued = False
        else:
            permit_issued = True
    return result, route, permit_issued, elapsed_ms


def run_one(base_scene, scenario, planner_name):
    scene = scenario_scene(base_scene, scenario)
    task = task_for(scene, scenario)
    view = build_navigation_view(scene, task)
    overlay = DynamicEventSet.from_mapping({
        "plan_version": 2 if scenario.get("events") else 1,
        "events": scenario.get("events", []),
    })
    initial_route = None
    initial_affected = False
    initial_pipeline_ms = 0.0
    if scenario.get("events"):
        _, initial_route, _, initial_pipeline_ms = run_pipeline(
            scene=scene,
            task=task,
            view=view,
            planner_name=planner_name,
            events=DynamicEventSet.empty(),
        )
        initial_affected = initial_route is not None and route_affected_by_events(
            initial_route.regions, view, overlay
        )
    result, route, permit_issued, elapsed_ms = run_pipeline(
        scene=scene,
        task=task,
        view=view,
        planner_name=planner_name,
        events=overlay,
    )
    executable = result.allowed and permit_issued
    validation_receipt = bool(
        route
        and route.safety_certificate
        and route.safety_certificate.get("validation_receipt")
    )
    return {
        "scenario_id": scenario["id"],
        "category": scenario["category"],
        "planner": planner_name,
        "decision": result.decision,
        "reason_code": result.reason_code,
        "success": int(executable),
        "expected_decision": scenario.get("expected_decision", "ALLOW"),
        "expected_outcome": int(result.decision == scenario.get("expected_decision", "ALLOW")),
        "unsafe_release": 0,
        "pipeline_integrity_failure": int(result.allowed and not permit_issued),
        "validation_receipt": int(validation_receipt),
        "execution_permit": int(permit_issued),
        "safety_certificate_id": route.safety_certificate_id if route else "",
        "distance_m": round(route.distance_m, 6) if route else 0.0,
        "risk_score": round(route.risk_score, 6) if route else 0.0,
        "pipeline_ms": round(elapsed_ms, 6),
        "initial_pipeline_ms": round(initial_pipeline_ms, 6),
        "plan_version": route.plan_version if route else overlay.plan_version,
        "initial_route_affected": int(initial_affected),
        "route_changed": int(bool(
            initial_route and route and initial_route.regions != route.regions
        )),
        "route": "|".join(route.regions) if route else "",
    }


def average(values):
    values = list(values)
    return mean(values) if values else 0.0


def summarize(rows):
    summary = {"schema_version": "2.0", "scenario_count": len({row["scenario_id"] for row in rows}), "planners": {}}
    for planner in PLANNERS:
        selected = [row for row in rows if row["planner"] == planner]
        dynamic = [row for row in selected if row["category"] == "dynamic"]
        summary["planners"][planner] = {
            "runs": len(selected),
            "success_rate": average(row["success"] for row in selected),
            "legal_task_success_rate": average(
                row["success"] for row in selected if row["expected_decision"] == "ALLOW"
            ),
            "expected_outcome_accuracy": average(row["expected_outcome"] for row in selected),
            "unsafe_release_rate": average(row["unsafe_release"] for row in selected),
            "pipeline_integrity_failure_rate": average(
                row["pipeline_integrity_failure"] for row in selected
            ),
            "validation_receipt_rate": average(
                row["validation_receipt"] for row in selected if row["success"]
            ),
            "execution_permit_rate": average(
                row["execution_permit"] for row in selected if row["decision"] == "ALLOW"
            ),
            "dynamic_success_rate": average(row["success"] for row in dynamic),
            "mean_pipeline_ms": average(row["pipeline_ms"] for row in selected),
            "p95_pipeline_ms": percentile([row["pipeline_ms"] for row in selected], 0.95),
            "mean_allowed_distance_m": average(
                row["distance_m"] for row in selected if row["success"]
            ),
            "mean_allowed_risk": average(
                row["risk_score"] for row in selected if row["success"]
            ),
        }
    by_scenario = {}
    for row in rows:
        by_scenario.setdefault(row["scenario_id"], {})[row["planner"]] = row
    summary["paired_vs_dijkstra"] = {}
    for planner in PLANNERS[1:]:
        paired = [
            values for values in by_scenario.values()
            if values["dijkstra"]["success"] and values[planner]["success"]
            and values[planner]["expected_decision"] == "ALLOW"
        ]
        reductions = [
            values["dijkstra"]["risk_score"] - values[planner]["risk_score"]
            for values in paired
        ]
        distance_changes = [
            values[planner]["distance_m"] - values["dijkstra"]["distance_m"]
            for values in paired
        ]
        time_changes = [
            values[planner]["pipeline_ms"] - values["dijkstra"]["pipeline_ms"]
            for values in paired
        ]
        summary["paired_vs_dijkstra"][planner] = {
            "comparable_allowed_runs": len(paired),
            "lower_risk_rate": average(value > 0 for value in reductions),
            "equal_or_lower_risk_rate": average(value >= 0 for value in reductions),
            "mean_risk_reduction": average(reductions),
            "mean_distance_change_m": average(distance_changes),
            "mean_pipeline_time_change_ms": average(time_changes),
        }
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenarios", type=Path, default=PROJECT_ROOT / "benchmarks/scenarios.json")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "artifacts/benchmark")
    args = parser.parse_args()
    document = json.loads(args.scenarios.read_text(encoding="utf-8"))
    provider = NavigationSandboxSceneProvider(
        PROJECT_ROOT / "data/fixtures/medical/hospital_1f.synthetic.json",
        PROJECT_ROOT / "navigation_sandbox",
        PROJECT_ROOT / "data/navigation/hospital_1f.bindings.json",
    )
    base_scene = provider.load_scene()
    rows = [run_one(base_scene, scenario, planner) for scenario in document["scenarios"] for planner in PLANNERS]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "planner_comparison.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = summarize(rows)
    json_path = args.output_dir / "planner_comparison.summary.json"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"CSV: {csv_path}")
    print(f"JSON: {json_path}")


if __name__ == "__main__":
    main()
