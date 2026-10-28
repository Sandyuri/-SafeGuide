"""Tests for multi-label Pareto product-graph search."""

from __future__ import annotations

import ast
from pathlib import Path

from roboguard.medical.joint_planner import DynamicEventSet, HospitalRoutePlanner
from roboguard.medical.pareto_planner import ParetoRoutePlanner
from roboguard.medical.planning_cost_model import PlanningCostModel
from roboguard.navigation import build_navigation_view

from test_joint_planner import Scene, events, make_view, region
from test_task_contracts import authorized_task, provider_and_scene


def routes(result):
    return {candidate.route for candidate in result.candidates}


def test_pareto_has_no_dependency_on_hospital_route_planner_internals():
    source = Path(ParetoRoutePlanner.__module__.replace(".", "/") + ".py")
    source = Path(__file__).parents[1] / "src" / source
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "HospitalRoutePlanner" not in imported_names
    assert not any(
        isinstance(node, ast.Attribute)
        and node.attr in {
            "_valid_edge",
            "_node_violation",
            "_risk_components",
            "_congestion",
            "_real_id",
            "_checked_rules",
        }
        for node in ast.walk(tree)
    )


def test_planning_cost_model_exposes_explainable_transition_components():
    view = make_view()
    overlay = events(
        {"type": "CONGESTION", "region_id": "real-risky", "severity": 0.5}
    )
    cost = PlanningCostModel().transition_cost(
        view=view,
        node="risky",
        region=view.payload["regions"]["risky"],
        edge_distance=2,
        edge_risk_rates={"emergency_corridor": 2},
        overlay=overlay,
    )
    assert cost.distance_m == 2
    assert cost.travel_time_s == 2.5
    assert cost.risk == 9
    assert dict(cost.risk_components) == {
        "congestion": 5,
        "emergency_corridor": 4,
    }


def test_returns_shortest_and_safer_non_dominated_routes():
    result = ParetoRoutePlanner().plan(scene=Scene(), planner_view=make_view())
    assert result.allowed
    assert routes(result) == {
        ("s", "risky", "d"),
        ("s", "safe", "d"),
    }
    by_route = {candidate.route: candidate for candidate in result.candidates}
    assert by_route[("s", "risky", "d")].distance_m == 2
    assert by_route[("s", "risky", "d")].risk == 2
    assert by_route[("s", "safe", "d")].distance_m == 4
    assert by_route[("s", "safe", "d")].risk == 0
    assert result.selected_candidate is None


def test_dominated_route_is_removed_from_final_candidates():
    graph = {
        "s": region(0, 0, ["best", "worse"], {"best": 1, "worse": 2}),
        "best": region(1, 0, ["s", "d"], {"s": 1, "d": 1}),
        "worse": region(0, 2, ["s", "d"], {"s": 2, "d": 2}),
        "d": region(2, 0, ["best", "worse"], {"best": 1, "worse": 2}),
    }
    result = ParetoRoutePlanner().plan(
        scene=Scene(), planner_view=make_view(graph=graph)
    )
    assert routes(result) == {("s", "best", "d")}
    assert result.pruned_labels > 0


def test_hard_constraints_are_pruned_before_candidate_generation():
    graph = {
        "s": region(0, 0, ["secure", "public"], {"secure": 1, "public": 2}),
        "secure": region(
            1, 0, ["s", "d"], {"s": 1, "d": 1}, permissions=("CLINICAL",)
        ),
        "public": region(0, 2, ["s", "d"], {"s": 2, "d": 2}),
        "d": region(2, 0, ["secure", "public"], {"secure": 1, "public": 2}),
    }
    result = ParetoRoutePlanner().plan(
        scene=Scene(), planner_view=make_view(graph=graph)
    )
    assert routes(result) == {("s", "public", "d")}


def test_multi_stage_route_records_confirmation_handoff_and_finish():
    graph = {
        "s": region(0, 0, ["registration"], {"registration": 1}),
        "registration": region(
            1, 0, ["s", "hall"], {"s": 1, "hall": 1}, policy="DESTINATION_ONLY"
        ),
        "hall": region(2, 0, ["registration", "exam"], {"registration": 1, "exam": 1}),
        "exam": region(3, 0, ["hall"], {"hall": 1}, policy="DESTINATION_ONLY"),
    }
    view = make_view(destinations=("registration", "exam"), graph=graph)
    view.payload["task"]["confirmation_destination_region_refs"] = ["registration"]
    view.payload["task"]["require_handoff"] = True
    view.payload["task"]["require_explicit_finish"] = True
    result = ParetoRoutePlanner().plan(scene=Scene(), planner_view=view)
    assert routes(result) == {("s", "registration", "hall", "exam")}
    candidate = result.candidates[0]
    assert candidate.state.workflow_state == "COMPLETE"
    actions = [item[1] for item in candidate.workflow_trace]
    assert actions == [
        "ARRIVE:registration",
        "CONFIRM:registration",
        "ARRIVE:exam",
        "HANDOFF",
        "FINISH",
    ]


def test_patient_burden_creates_a_distinct_tradeoff_candidate():
    graph = {
        "s": region(0, 0, ["short", "easy"], {"short": 1, "easy": 2}),
        "short": region(1, 0, ["s", "d"], {"s": 1, "d": 1}),
        "easy": region(0, 2, ["s", "d"], {"s": 2, "d": 2}),
        "d": region(2, 0, ["short", "easy"], {"short": 1, "easy": 2}),
    }
    graph["short"]["patient_burden"] = 5
    result = ParetoRoutePlanner().plan(
        scene=Scene(), planner_view=make_view(graph=graph)
    )
    assert routes(result) == {
        ("s", "short", "d"),
        ("s", "easy", "d"),
    }


def test_dynamic_closure_never_appears_in_candidates():
    overlay = events({"type": "NODE_CLOSED", "region_id": "real-risky"})
    result = ParetoRoutePlanner().plan(
        scene=Scene(), planner_view=make_view(), events=overlay
    )
    assert routes(result) == {("s", "safe", "d")}
    assert result.plan_version == 2


def test_future_dynamic_closure_is_evaluated_at_candidate_arrival_time():
    future = events({
        "type": "NODE_CLOSED",
        "region_id": "real-risky",
        "effective_at": "2026-10-19T08:00:20Z",
    })
    result = ParetoRoutePlanner().plan(
        scene=Scene(), planner_view=make_view(), events=future
    )
    assert ("s", "risky", "d") in routes(result)

    active = events({
        "type": "NODE_CLOSED",
        "region_id": "real-risky",
        "effective_at": "2026-10-19T08:00:00Z",
    })
    result = ParetoRoutePlanner().plan(
        scene=Scene(), planner_view=make_view(), events=active
    )
    assert ("s", "risky", "d") not in routes(result)


def test_results_are_deterministic_and_bounded():
    planner = ParetoRoutePlanner(max_labels_per_state=4, max_final_candidates=1)
    snapshots = []
    for _ in range(5):
        result = planner.plan(scene=Scene(), planner_view=make_view())
        snapshots.append(result.to_dict())
        assert len(result.candidates) == 1
    assert all(snapshot == snapshots[0] for snapshot in snapshots)


def test_existing_single_route_planners_remain_unchanged():
    assert HospitalRoutePlanner("dijkstra").plan(
        scene=Scene(), planner_view=make_view()
    ).route == ("s", "risky", "d")
    assert HospitalRoutePlanner("joint").plan(
        scene=Scene(), planner_view=make_view()
    ).route == ("s", "safe", "d")


def test_status_expiry_returns_pause_when_no_candidate_exists():
    graph = {
        "s": region(0, 0, ["x"], {"x": 1}),
        "x": region(1, 0, ["s", "d"], {"s": 1, "d": 1}),
        "d": region(2, 0, ["x"], {"x": 1}),
    }
    overlay = DynamicEventSet.from_mapping(
        {
            "plan_version": 2,
            "events": [{"type": "STATUS_EXPIRED", "region_id": "real-x"}],
        }
    )
    result = ParetoRoutePlanner().plan(
        scene=Scene(), planner_view=make_view(graph=graph), events=overlay
    )
    assert result.decision == "PAUSE"
    assert result.reason_code == "ROUTE_STATUS_EXPIRED"
    assert result.candidates == ()


def test_current_hospital_map_has_a_bounded_reproducible_frontier():
    provider, scene = provider_and_scene()
    destination = provider.resolve_location("药房")
    view = build_navigation_view(scene, authorized_task(scene, destination))
    planner = ParetoRoutePlanner(max_labels_per_state=16, max_final_candidates=8)
    first = planner.plan(scene=scene, planner_view=view)
    second = planner.plan(scene=scene, planner_view=view)
    assert first.allowed
    assert 1 <= len(first.candidates) <= 8
    assert first.to_dict() == second.to_dict()
    assert all(
        candidate.state.workflow_state == "COMPLETE"
        for candidate in first.candidates
    )
