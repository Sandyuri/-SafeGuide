"""Focused tests for constrained A*, workflow state and dynamic replanning."""

from __future__ import annotations

from types import MappingProxyType

from roboguard.medical.joint_planner import (
    DynamicEventSet,
    HospitalRoutePlanner,
    WorkflowAutomaton,
    route_affected_by_events,
)
from roboguard.medical.planner_view import PlannerSceneView


def region(x, y, connections, costs, *, kind="CORRIDOR", tags=(), policy="ALLOW_TRANSIT", permissions=(), status="OPEN"):
    return {
        "type": kind,
        "coords": [x, y, 0],
        "connections": list(connections),
        "connection_costs": dict(costs),
        "area_type": "PUBLIC",
        "required_permissions": list(permissions),
        "navigation_status": status,
        "navigation_status_valid_until": None,
        "prohibited_cargo_kinds": [],
        "semantic_tags": list(tags),
        "route_policy": policy,
        "safe_stop": True,
    }


class Scene:
    snapshot_id = "snapshot-1"
    snapshot_hash = "hash-1"
    data = {
        "current_time": "2026-10-19T08:00:00Z",
        "facility": {"emergency_status": "NONE", "hazards": []},
        "delivery_items": {},
    }


def make_view(*, accessibility=(), permissions=(), destinations=("d",), graph=None):
    if graph is None:
        graph = {
            "s": region(0, 0, ["risky", "safe"], {"risky": 1, "safe": 2}),
            "risky": region(1, 0, ["s", "d"], {"s": 1, "d": 1}, tags=("EMERGENCY_CORRIDOR",)),
            "safe": region(0, 2, ["s", "d"], {"s": 2, "d": 2}),
            "d": region(2, 0, ["risky", "safe"], {"risky": 1, "safe": 2}),
        }
    refs = {key: f"real-{key}" for key in graph}
    return PlannerSceneView(
        {
            "schema_version": "0.2",
            "scene_id": "scene-1",
            "snapshot_id": "snapshot-1",
            "snapshot_hash": "hash-1",
            "evaluation_id": "eval-1",
            "request_id": "request-1",
            "route_context": {"authorization_id": "eval-1", "request_id": "request-1", "map_id": "test", "map_version": 1},
            "task": {
                "type": "NAVIGATE_TO_LOCATION",
                "source_region_ref": "s",
                "destination_region_ref": destinations[-1],
                "ordered_destination_region_refs": list(destinations),
                "accessibility_needs": list(accessibility),
                "carrying_refs": [],
                "urgency": "ROUTINE",
            },
            "regions": graph,
            "robot": {
                "current_region_ref": "s",
                "capabilities": ["GUIDED_NAVIGATION"],
                "access_permissions": list(permissions),
                "operational_status": "READY",
            },
        },
        MappingProxyType(refs),
    )


def events(*items, version=2):
    return DynamicEventSet.from_mapping({"plan_version": version, "events": list(items)})


def test_joint_prefers_lower_medical_risk_before_distance():
    result = HospitalRoutePlanner("joint").plan(scene=Scene(), planner_view=make_view())
    assert result.route == ("s", "safe", "d")
    assert result.risk_score == 0


def test_dijkstra_reproduces_shortest_distance_baseline():
    result = HospitalRoutePlanner("dijkstra").plan(scene=Scene(), planner_view=make_view())
    assert result.route == ("s", "risky", "d")
    assert result.distance_m == 2
    assert result.risk_score == 2
    assert result.risk_components == {"emergency_corridor": 2.0}


def test_astar_matches_dijkstra_distance_without_using_risk_for_selection():
    view = make_view()
    dijkstra = HospitalRoutePlanner("dijkstra").plan(
        scene=Scene(), planner_view=view
    )
    astar = HospitalRoutePlanner("astar").plan(
        scene=Scene(), planner_view=view
    )
    assert astar.planner == "astar"
    assert astar.reason_code == "ASTAR_PLAN_ALLOWED"
    assert astar.route == dijkstra.route == ("s", "risky", "d")
    assert astar.distance_m == dijkstra.distance_m == 2
    assert astar.risk_score == dijkstra.risk_score == 2


def test_astar_scaled_heuristic_remains_distance_optimal():
    graph = {
        "s": region(0, 0, ["a", "b"], {"a": 2, "b": 3}),
        "a": region(1000, 0, ["s", "d"], {"s": 2, "d": 2}),
        "b": region(1, 0, ["s", "d"], {"s": 3, "d": 3}),
        "d": region(2, 0, ["a", "b"], {"a": 2, "b": 3}),
    }
    result = HospitalRoutePlanner("astar").plan(
        scene=Scene(), planner_view=make_view(graph=graph)
    )
    assert result.route == ("s", "a", "d")
    assert result.distance_m == 4


def test_astar_result_is_deterministic():
    planner = HospitalRoutePlanner("astar")
    routes = {
        planner.plan(scene=Scene(), planner_view=make_view()).route
        for _ in range(5)
    }
    assert routes == {("s", "risky", "d")}


def test_generate_then_check_rejects_blocked_shortest_path_without_retry():
    overlay = events({"type": "NODE_BLOCKED", "region_id": "real-risky"})
    result = HospitalRoutePlanner("generate_then_check").plan(scene=Scene(), planner_view=make_view(), events=overlay)
    assert result.decision == "REJECT"
    assert result.reason_code == "DYNAMIC_NODE_BLOCKED"


def test_joint_reroutes_around_dynamic_closure():
    overlay = events({"type": "NODE_CLOSED", "region_id": "real-risky"})
    result = HospitalRoutePlanner("joint").plan(scene=Scene(), planner_view=make_view(), events=overlay)
    assert result.route == ("s", "safe", "d")
    assert result.plan_version == 2


def test_wheelchair_route_never_uses_stairs():
    graph = {
        "s": region(0, 0, ["stairs", "ramp"], {"stairs": 1, "ramp": 2}),
        "stairs": region(1, 0, ["s", "d"], {"s": 1, "d": 1}, kind="STAIR_HALL"),
        "ramp": region(0, 2, ["s", "d"], {"s": 2, "d": 2}),
        "d": region(2, 0, ["stairs", "ramp"], {"stairs": 1, "ramp": 2}),
    }
    result = HospitalRoutePlanner("joint").plan(
        scene=Scene(), planner_view=make_view(accessibility=("WHEELCHAIR_ACCESSIBLE",), graph=graph)
    )
    assert "stairs" not in result.route


def test_step_free_route_never_uses_escalator():
    graph = {
        "s": region(0, 0, ["escalator", "lift"], {"escalator": 1, "lift": 2}),
        "escalator": region(1, 0, ["s", "d"], {"s": 1, "d": 1}, kind="ESCALATOR_HALL"),
        "lift": region(0, 2, ["s", "d"], {"s": 2, "d": 2}, kind="ELEVATOR_HALL"),
        "d": region(2, 0, ["escalator", "lift"], {"escalator": 1, "lift": 2}),
    }
    result = HospitalRoutePlanner("joint").plan(
        scene=Scene(), planner_view=make_view(accessibility=("STEP_FREE",), graph=graph)
    )
    assert result.route == ("s", "lift", "d")


def test_permission_requirement_is_pruned_during_joint_search():
    graph = {
        "s": region(0, 0, ["secure", "public"], {"secure": 1, "public": 2}),
        "secure": region(1, 0, ["s", "d"], {"s": 1, "d": 1}, permissions=("CLINICAL",)),
        "public": region(0, 2, ["s", "d"], {"s": 2, "d": 2}),
        "d": region(2, 0, ["secure", "public"], {"secure": 1, "public": 2}),
    }
    result = HospitalRoutePlanner("joint").plan(scene=Scene(), planner_view=make_view(graph=graph))
    assert result.route == ("s", "public", "d")


def test_joint_result_is_deterministic():
    planner = HospitalRoutePlanner("joint")
    routes = {planner.plan(scene=Scene(), planner_view=make_view()).route for _ in range(5)}
    assert routes == {("s", "safe", "d")}


def test_joint_search_tracks_ordered_waypoints():
    graph = {
        "s": region(0, 0, ["a"], {"a": 1}),
        "a": region(1, 0, ["s", "b", "d"], {"s": 1, "b": 1, "d": 1}),
        "b": region(2, 0, ["a", "d"], {"a": 1, "d": 1}),
        "d": region(3, 0, ["a", "b"], {"a": 1, "b": 1}),
    }
    result = HospitalRoutePlanner("joint").plan(
        scene=Scene(), planner_view=make_view(destinations=("b", "d"), graph=graph)
    )
    assert result.route.index("b") < result.route.index("d")
    assert len(result.workflow_trace) == 2


def test_destination_only_intermediate_waypoint_is_allowed_at_its_stage():
    graph = {
        "s": region(0, 0, ["registration"], {"registration": 1}),
        "registration": region(
            1, 0, ["s", "hall"], {"s": 1, "hall": 1}, policy="DESTINATION_ONLY"
        ),
        "hall": region(2, 0, ["registration", "exam"], {"registration": 1, "exam": 1}),
        "exam": region(3, 0, ["hall"], {"hall": 1}, policy="DESTINATION_ONLY"),
    }
    result = HospitalRoutePlanner("joint").plan(
        scene=Scene(),
        planner_view=make_view(destinations=("registration", "exam"), graph=graph),
    )
    assert result.allowed
    assert result.route == ("s", "registration", "hall", "exam")


def test_scaled_geometric_heuristic_keeps_astar_distance_optimal():
    # Coordinates and stored edge costs can use different scales. An unscaled
    # Euclidean heuristic would overestimate the path through a and return 6 m.
    graph = {
        "s": region(0, 0, ["a", "b"], {"a": 2, "b": 3}),
        "a": region(1000, 0, ["s", "d"], {"s": 2, "d": 2}),
        "b": region(1, 0, ["s", "d"], {"s": 3, "d": 3}),
        "d": region(2, 0, ["a", "b"], {"a": 2, "b": 3}),
    }
    result = HospitalRoutePlanner("joint").plan(
        scene=Scene(), planner_view=make_view(graph=graph)
    )
    assert result.route == ("s", "a", "d")
    assert result.distance_m == 4


def test_workflow_rejects_skipping_first_destination():
    workflow = WorkflowAutomaton(("registration", "exam"))
    assert workflow.arrive("exam") is False
    assert workflow.trace[-1].rule_id == "WF-001"
    assert workflow.trace[-1].action == "ARRIVE:exam"
    assert workflow.trace[-1].from_state == "GO_TO_1"
    assert workflow.trace[-1].to_state == "GO_TO_1"
    assert workflow.trace[-1].accepted is False


def test_workflow_requires_confirmation_before_next_stage():
    workflow = WorkflowAutomaton(
        ("registration", "exam"),
        confirmation_destinations=("registration",),
    )
    assert workflow.arrive("registration") is True
    assert workflow.state == "WAIT_CONFIRM_1"
    assert workflow.arrive("exam") is False
    assert workflow.trace[-1].rule_id == "WF-001"
    assert workflow.confirm("registration") is True
    assert workflow.state == "GO_TO_2"
    assert workflow.arrive("exam") is True
    assert workflow.complete is True


def test_workflow_rejects_wrong_stage_confirmation_with_audit_context():
    workflow = WorkflowAutomaton(
        ("registration", "exam"),
        confirmation_destinations=("registration",),
    )
    workflow.arrive("registration")
    assert workflow.confirm("exam") is False
    rejected = workflow.trace[-1]
    assert rejected.rule_id == "WF-002"
    assert rejected.action == "CONFIRM:exam"
    assert rejected.from_state == "WAIT_CONFIRM_1"
    assert rejected.to_state == "WAIT_CONFIRM_1"
    assert rejected.accepted is False


def test_workflow_rejects_continuation_while_paused():
    workflow = WorkflowAutomaton(("registration",))
    workflow.pause()
    assert workflow.arrive("registration") is False
    assert workflow.trace[-1].rule_id == "WF-003"


def test_replan_invalidates_old_plan_version():
    workflow = WorkflowAutomaton(("registration",), plan_version=1)
    workflow.replan(2)
    assert workflow.arrive("registration", plan_version=1) is False
    assert workflow.arrive("registration", plan_version=2) is True


def test_handoff_is_required_before_workflow_completion():
    workflow = WorkflowAutomaton(("exam",), require_handoff=True)
    assert workflow.arrive("exam") is True
    assert workflow.complete is False
    assert workflow.handoff() is True
    assert workflow.complete is True


def test_finish_is_rejected_until_required_handoff_is_complete():
    workflow = WorkflowAutomaton(
        ("exam",),
        require_handoff=True,
        require_explicit_finish=True,
    )
    assert workflow.arrive("exam") is True
    assert workflow.state == "WAIT_HANDOFF"
    assert workflow.finish() is False
    rejected = workflow.trace[-1]
    assert rejected.rule_id == "WF-006"
    assert rejected.action == "FINISH"
    assert rejected.from_state == "WAIT_HANDOFF"
    assert rejected.accepted is False
    assert workflow.handoff() is True
    assert workflow.state == "READY_TO_COMPLETE"
    assert workflow.finish() is True
    assert workflow.complete is True


def test_workflow_ltlf_matches_confirmation_handoff_and_finish_rules():
    workflow = WorkflowAutomaton(
        ("registration", "exam"),
        confirmation_destinations=("registration",),
        require_handoff=True,
        require_explicit_finish=True,
    )
    formulas = workflow.ltlf_formulas()
    assert "G(arrive_1 -> X(confirm_1))" in formulas
    assert "F(handoff)" in formulas
    assert "G(finish -> handoff_complete)" in formulas
    assert "F(finish)" in formulas


def test_workflow_configuration_rejects_unknown_confirmation_destination():
    try:
        WorkflowAutomaton(
            ("registration", "exam"),
            confirmation_destinations=("pharmacy",),
        )
    except Exception as error:
        assert "not in ordered destinations" in str(error)
    else:
        raise AssertionError("unknown confirmation destination must be rejected")


def test_elevator_event_removes_elevator_node():
    graph = {
        "s": region(0, 0, ["lift", "hall"], {"lift": 1, "hall": 2}),
        "lift": region(1, 0, ["s", "d"], {"s": 1, "d": 1}, kind="ELEVATOR_HALL"),
        "hall": region(0, 2, ["s", "d"], {"s": 2, "d": 2}),
        "d": region(2, 0, ["lift", "hall"], {"lift": 1, "hall": 2}),
    }
    result = HospitalRoutePlanner("joint").plan(
        scene=Scene(), planner_view=make_view(graph=graph),
        events=events({"type": "ELEVATOR_UNAVAILABLE", "region_id": "real-lift"}),
    )
    assert "lift" not in result.route


def test_active_emergency_corridor_is_hard_constraint():
    overlay = events({"type": "EMERGENCY_CORRIDOR_ACTIVE", "region_id": "real-risky"})
    result = HospitalRoutePlanner("joint").plan(scene=Scene(), planner_view=make_view(), events=overlay)
    assert result.route == ("s", "safe", "d")


def test_expired_status_causes_safe_pause_when_no_alternative_exists():
    graph = {
        "s": region(0, 0, ["x"], {"x": 1}),
        "x": region(1, 0, ["s", "d"], {"s": 1, "d": 1}),
        "d": region(2, 0, ["x"], {"x": 1}),
    }
    result = HospitalRoutePlanner("joint").plan(
        scene=Scene(), planner_view=make_view(graph=graph),
        events=events({"type": "STATUS_EXPIRED", "region_id": "real-x"}),
    )
    assert result.decision == "PAUSE"
    assert result.reason_code == "ROUTE_STATUS_EXPIRED"


def test_congestion_is_reported_in_risk_breakdown():
    graph = {
        "s": region(0, 0, ["x"], {"x": 1}),
        "x": region(1, 0, ["s", "d"], {"s": 1, "d": 1}),
        "d": region(2, 0, ["x"], {"x": 1}),
    }
    result = HospitalRoutePlanner("joint").plan(
        scene=Scene(), planner_view=make_view(graph=graph),
        events=events({"type": "CONGESTION", "region_id": "real-x", "severity": 0.5}),
    )
    assert result.risk_components["congestion"] == 5.0


def test_existing_route_is_invalidated_by_new_event():
    view = make_view()
    overlay = events({"type": "NODE_BLOCKED", "region_id": "real-risky"})
    assert route_affected_by_events(("s", "risky", "d"), view, overlay) is True
    assert route_affected_by_events(("s", "safe", "d"), view, overlay) is False


def test_replanner_rejects_non_newer_plan_version():
    view = make_view()
    planner = HospitalRoutePlanner("joint")
    previous = planner.plan(scene=Scene(), planner_view=view)
    result = planner.replan(
        scene=Scene(), planner_view=view, previous=previous,
        events=events({"type": "NODE_BLOCKED", "region_id": "real-safe"}, version=1),
    )
    assert result.decision == "PAUSE"
    assert result.reason_code == "PLAN_VERSION_NOT_NEWER"


def test_replanner_enters_safe_pause_when_all_routes_are_closed():
    graph = {
        "s": region(0, 0, ["x"], {"x": 1}),
        "x": region(1, 0, ["s", "d"], {"s": 1, "d": 1}),
        "d": region(2, 0, ["x"], {"x": 1}),
    }
    view = make_view(graph=graph)
    planner = HospitalRoutePlanner("joint")
    previous = planner.plan(scene=Scene(), planner_view=view)
    result = planner.replan(
        scene=Scene(), planner_view=view, previous=previous,
        events=events({"type": "NODE_BLOCKED", "region_id": "real-x"}),
    )
    assert result.decision == "PAUSE"
    assert result.reason_code == "SAFE_PAUSE_NO_REPLAN_ROUTE"


