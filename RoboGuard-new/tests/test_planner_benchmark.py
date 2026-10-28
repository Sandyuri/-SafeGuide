"""Regression tests for the end-to-end five-planner benchmark."""

import json
from pathlib import Path

from roboguard.medical.providers import NavigationSandboxSceneProvider
from roboguard.medical.joint_planner import DynamicEventSet
from roboguard.medical.safety_barrier import require_execution_permit
from roboguard.medical.temporal_guard import MedicalTemporalGuard
from roboguard.navigation import NavigationExecutionGuard, build_navigation_view
from scripts.run_planner_benchmark import (
    PLANNERS,
    actions_for,
    run_one,
    scenario_scene,
    summarize,
    task_for,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_all_five_planners_use_validation_and_execution_permits():
    provider = NavigationSandboxSceneProvider(
        PROJECT_ROOT / "data/fixtures/medical/hospital_1f.synthetic.json",
        PROJECT_ROOT / "navigation_sandbox",
        PROJECT_ROOT / "data/navigation/hospital_1f.bindings.json",
    )
    base_scene = provider.load_scene()
    scenarios = json.loads(
        (PROJECT_ROOT / "benchmarks/scenarios.json").read_text(encoding="utf-8")
    )["scenarios"]
    scenario = next(item for item in scenarios if item["expected_decision"] == "ALLOW")

    rows = [run_one(base_scene, scenario, planner) for planner in PLANNERS]

    assert PLANNERS == (
        "dijkstra",
        "astar",
        "generate_then_check",
        "joint",
        "pareto",
    )
    assert {row["planner"] for row in rows} == set(PLANNERS)
    assert all(row["success"] == 1 for row in rows)
    assert all(row["validation_receipt"] == 1 for row in rows)
    assert all(row["execution_permit"] == 1 for row in rows)
    assert all(row["pipeline_integrity_failure"] == 0 for row in rows)
    assert all(row["safety_certificate_id"].startswith("sha256:") for row in rows)

    summary = summarize(rows)
    assert set(summary["planners"]) == set(PLANNERS)
    assert set(summary["paired_vs_dijkstra"]) == set(PLANNERS[1:])


def test_dstar_lite_is_used_for_a_valid_local_repair():
    provider = NavigationSandboxSceneProvider(
        PROJECT_ROOT / "data/fixtures/medical/hospital_1f.synthetic.json",
        PROJECT_ROOT / "navigation_sandbox",
        PROJECT_ROOT / "data/navigation/hospital_1f.bindings.json",
    )
    base_scene = provider.load_scene()
    scenarios = json.loads(
        (PROJECT_ROOT / "benchmarks/scenarios.json").read_text(encoding="utf-8")
    )["scenarios"]
    scenario = next(item for item in scenarios if item["id"] == "normal-003")
    scene = scenario_scene(base_scene, scenario)
    task = task_for(scene, scenario)
    view = build_navigation_view(scene, task)
    actions = actions_for(view)

    baseline = NavigationExecutionGuard(
        temporal_guard=MedicalTemporalGuard(mode="off"),
        planner="pareto",
    ).review_plan(
        scene=scene,
        authorized_task=task,
        planner_view=view,
        actions=actions,
    )
    events = DynamicEventSet.from_mapping(
        {
            "plan_version": 2,
            "events": [
                {"type": "NODE_BLOCKED", "region_id": "nav-f1-spine-01"}
            ],
        }
    )
    replanned = NavigationExecutionGuard(
        temporal_guard=MedicalTemporalGuard(mode="off"),
        planner="pareto",
        events=events,
    ).review_plan(
        scene=scene,
        authorized_task=task,
        planner_view=view,
        actions=actions,
        previous_plan=baseline.planning_result,
    )

    assert replanned.allowed
    route = replanned.checked_routes[0]
    assert route.planner == "dstar_lite"
    assert route.safety_certificate["source_type"] == "DSTAR_LITE_REPAIR"
    assert "nav-f1-spine-01" not in {
        view.resolve_reference(item) for item in route.regions
    }
    require_execution_permit(route, expected_plan_version=2)
