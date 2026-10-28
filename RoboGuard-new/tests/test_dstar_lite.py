"""Tests for incremental D* Lite route repair and Pareto escalation."""

from __future__ import annotations

from roboguard.medical.dstar_lite import DStarLite, IncrementalNavigationReplanner
from roboguard.medical.joint_planner import DynamicEventSet, PlanResult
from roboguard.medical.task_contracts import RouteContext, TaskSpec

from test_joint_planner import Scene, events, make_view, region


def context():
    return RouteContext(
        "auth-1", "request-1", "ALLOW_TO_ENTER_PLANNING",
        "snapshot-1", "hash-1", "test", 1,
        "2026-10-20T08:00:00Z",
    )


def task(**kwargs):
    values = {
        "task_type": "NAVIGATE_TO_LOCATION",
        "source_region_id": "real-s",
        "ordered_destination_ids": ("real-d",),
    }
    values.update(kwargs)
    return TaskSpec(**values)


def previous(route, distance, *, risk=0, burden=0, version=1):
    return PlanResult(
        decision="ALLOW",
        reason_code="PARETO_PLAN_SELECTED",
        message="baseline",
        planner="pareto",
        plan_version=version,
        route=route,
        distance_m=distance,
        estimated_time_s=distance,
        risk_score=risk,
        patient_burden=burden,
    )


def repair_graph(*, risky_detour=False):
    tags = ("EMERGENCY_CORRIDOR",) if risky_detour else ()
    return {
        "s": region(0, 0, ["a"], {"a": 1}),
        "a": region(1, 0, ["s", "x", "b"], {"s": 1, "x": 1, "b": 0.7}),
        "x": region(2, 0, ["a", "d"], {"a": 1, "d": 1}),
        "b": region(2, 1, ["a", "d"], {"a": 0.7, "d": 1.7}, tags=tags),
        "d": region(3, 0, ["x", "b"], {"x": 1, "b": 1.7}),
    }


def test_dstar_lite_repairs_changed_edge_cost_incrementally():
    graph = {
        "s": region(0, 0, ["a", "b"], {"a": 1, "b": 2}),
        "a": region(1, 0, ["s", "d"], {"s": 1, "d": 1}),
        "b": region(0, 2, ["s", "d"], {"s": 2, "d": 2}),
        "d": region(2, 0, ["a", "b"], {"a": 1, "b": 2}),
    }
    view = make_view(graph=graph)
    planner = DStarLite(scene=Scene(), planner_view=view, start="s", goal="d")
    assert planner.extract_route() == ("s", "a", "d")
    overlay = DynamicEventSet.from_mapping(
        {
            "plan_version": 2,
            "events": [{
                "type": "EDGE_COST_CHANGED",
                "source_region_id": "real-a",
                "target_region_id": "real-d",
                "new_cost": 10,
            }],
        }
    )
    repaired = planner.repair(overlay)
    assert repaired.route == ("s", "b", "d")
    assert repaired.distance_m == 4
    assert repaired.repair_expanded_states > 0
    assert planner.g and planner.rhs and planner.predecessors and planner.successors


def test_synthetic_local_repair_fails_closed_against_authoritative_map():
    view = make_view(graph=repair_graph())
    result = IncrementalNavigationReplanner().replan(
        scene=Scene(),
        planner_view=view,
        previous=previous(("s", "a", "x", "d"), 3),
        task=task(),
        context=context(),
        events=events({"type": "NODE_BLOCKED", "region_id": "real-x"}),
    )
    assert result.mode == "FULL_PARETO_REPLAN"
    assert not result.result.allowed
    assert result.result.reason_code == "NO_VALIDATED_PARETO_CANDIDATE"
    assert result.preserved_prefix == ("s", "a")


def test_local_repair_that_increases_risk_escalates_to_pareto():
    view = make_view(graph=repair_graph(risky_detour=True))
    result = IncrementalNavigationReplanner().replan(
        scene=Scene(),
        planner_view=view,
        previous=previous(("s", "a", "x", "d"), 3),
        task=task(),
        context=context(),
        events=events({"type": "NODE_BLOCKED", "region_id": "real-x"}),
    )
    assert result.mode == "FULL_PARETO_REPLAN"
    assert result.fallback_reason == "D* Lite repair increases medical risk"


def test_major_patient_change_skips_dstar_and_runs_full_pareto():
    view = make_view(graph=repair_graph())
    result = IncrementalNavigationReplanner().replan(
        scene=Scene(),
        planner_view=view,
        previous=previous(("s", "a", "x", "d"), 3),
        task=task(),
        context=context(),
        events=events({
            "type": "PATIENT_STATUS_CHANGED",
            "region_id": "real-s",
        }),
    )
    assert result.mode == "FULL_PARETO_REPLAN"
    assert result.dstar_repair_expanded_states == 0
    assert "major change" in result.fallback_reason


def test_no_alternative_route_enters_safe_pause():
    graph = {
        "s": region(0, 0, ["x"], {"x": 1}),
        "x": region(1, 0, ["s", "d"], {"s": 1, "d": 1}),
        "d": region(2, 0, ["x"], {"x": 1}),
    }
    result = IncrementalNavigationReplanner().replan(
        scene=Scene(),
        planner_view=make_view(graph=graph),
        previous=previous(("s", "x", "d"), 2),
        task=task(),
        context=context(),
        events=events({"type": "NODE_BLOCKED", "region_id": "real-x"}),
    )
    assert result.mode == "FULL_PARETO_REPLAN"
    assert result.result.decision == "PAUSE"
    assert result.result.reason_code == "SAFE_PAUSE_NO_REPLAN_ROUTE"


def test_old_plan_version_cannot_be_repaired():
    view = make_view(graph=repair_graph())
    result = IncrementalNavigationReplanner().replan(
        scene=Scene(),
        planner_view=view,
        previous=previous(("s", "a", "x", "d"), 3, version=2),
        task=task(),
        context=context(),
        events=events({"type": "NODE_BLOCKED", "region_id": "real-x"}, version=2),
    )
    assert result.mode == "SAFE_PAUSE"
    assert result.result.reason_code == "PLAN_VERSION_NOT_NEWER"
