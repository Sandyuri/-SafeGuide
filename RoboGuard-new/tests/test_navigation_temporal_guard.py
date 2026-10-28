"""Navigation-first temporal guard regression tests."""

import json
from types import MappingProxyType, SimpleNamespace

from roboguard.medical.guard_types import ALLOW, PAUSE, REJECT, CheckedRoute
from roboguard.medical.planner_view import PlannerSceneView
from roboguard.medical.temporal_guard import MedicalTemporalGuard
from roboguard.spine_adapter import adapt_spine_plan
from roboguard.generator import ContextualGrounding


def view(*, middle_policy="ALLOW_TRANSIT"):
    regions = {
        "task-source-1": {
            "type": "CORRIDOR", "coords": [0, 0], "connections": ["task-region-1"],
            "connection_costs": {"task-region-1": 1.0}, "area_type": "NORMAL",
            "required_permissions": [], "navigation_status": "OPEN", "semantic_tags": [],
            "route_policy": "ALLOW_TRANSIT", "prohibited_cargo_kinds": [], "safe_stop": True,
        },
        "task-region-1": {
            "type": "CORRIDOR", "coords": [1, 0], "connections": ["task-source-1", "task-destination-1"],
            "connection_costs": {"task-source-1": 1.0, "task-destination-1": 1.0},
            "area_type": "NORMAL", "required_permissions": [], "navigation_status": "OPEN",
            "semantic_tags": ["EMERGENCY_CORRIDOR"], "route_policy": middle_policy,
            "prohibited_cargo_kinds": [], "safe_stop": False,
        },
        "task-destination-1": {
            "type": "ROOM", "coords": [2, 0], "connections": ["task-region-1"],
            "connection_costs": {"task-region-1": 1.0}, "area_type": "NORMAL",
            "required_permissions": [], "navigation_status": "OPEN", "semantic_tags": [],
            "route_policy": "DESTINATION_ONLY", "prohibited_cargo_kinds": [], "safe_stop": True,
        },
    }
    payload = {
        "task": {"type": "NAVIGATE_TO_LOCATION", "destination_region_ref": "task-destination-1"},
        "regions": regions,
        "robot": {"current_region_ref": "task-source-1", "access_permissions": []},
    }
    return PlannerSceneView(payload, MappingProxyType({name: name for name in regions}))


def route():
    return CheckedRoute(0, "task-source-1", "task-destination-1", (
        "task-source-1", "task-region-1", "task-destination-1"
    ))


class AllowingGuard:
    projected = None
    graph = None

    def __init__(self, **_):
        pass

    def update_context(self, graph):
        type(self).graph = graph

    def get_safety_specs(self):
        return {"avoid emergency-only corridors": [{
            "constraint": "G(!goto(task-region-1))", "reasoning": "test"
        }]}

    def validate_plan(self, plan):
        type(self).projected = plan
        return True, [(f"{a}({b})", True) for a, b in plan]


def test_hybrid_expands_every_route_node_and_records_ltl():
    guard = MedicalTemporalGuard(mode="hybrid", guard_factory=AllowingGuard)
    result = guard.review(
        planner_view=view(),
        actions=adapt_spine_plan([("goto", "task-destination-1")]),
        checked_route=route(),
    )
    assert result.decision == ALLOW
    assert AllowingGuard.projected == [
        ("goto", "task-region-1"), ("goto", "task-destination-1")
    ]
    assert result.checked_ltl == ("G(!goto(task-region-1))",)
    assert "task-region-1" in AllowingGuard.graph
    assert "sg-f1-" not in AllowingGuard.graph


def test_mandatory_policy_cannot_be_weakened_by_llm():
    AllowingGuard.projected = None
    guard = MedicalTemporalGuard(mode="hybrid", guard_factory=AllowingGuard)
    result = guard.review(
        planner_view=view(middle_policy="FORBID"),
        actions=adapt_spine_plan([("goto", "task-destination-1")]),
        checked_route=route(),
    )
    assert (result.decision, result.reason_code) == (REJECT, "MANDATORY_ROUTE_POLICY_VIOLATION")
    assert result.failed_route_node == "task-region-1"
    assert AllowingGuard.projected is None


def test_generation_failure_and_unknown_reference_pause():
    class FailingGuard(AllowingGuard):
        def update_context(self, _):
            raise RuntimeError("service unavailable")
    result = MedicalTemporalGuard(mode="hybrid", guard_factory=FailingGuard).review(
        planner_view=view(), actions=adapt_spine_plan([("goto", "task-destination-1")]), checked_route=route()
    )
    assert (result.decision, result.reason_code) == (PAUSE, "TEMPORAL_RULE_GENERATION_FAILED")

    class UnknownGuard(AllowingGuard):
        def get_safety_specs(self):
            return {"bad": [{"constraint": "G(!goto(real-hospital-node))"}]}
    result = MedicalTemporalGuard(mode="hybrid", guard_factory=UnknownGuard).review(
        planner_view=view(), actions=adapt_spine_plan([("goto", "task-destination-1")]), checked_route=route()
    )
    assert (result.decision, result.reason_code) == (PAUSE, "TEMPORAL_RULE_GENERATION_FAILED")


def test_physical_action_after_control_stop_is_rejected():
    result = MedicalTemporalGuard(mode="fixed").review(
        planner_view=view(),
        actions=adapt_spine_plan([
            ("request_human_review", "check"), ("goto", "task-destination-1")
        ]),
        checked_route=route(),
    )
    assert (result.decision, result.reason_code) == (REJECT, "PHYSICAL_ACTION_AFTER_CONTROL_STOP")


def test_temporal_guard_rejects_forbidden_route_and_accepts_alternative():
    planner_view = view()
    payload = planner_view.to_dict()
    regions = payload["regions"]
    regions["task-region-2"] = {
        **regions["task-region-1"],
        "coords": [1, 1],
        "connections": ["task-source-1", "task-destination-1"],
        "connection_costs": {"task-source-1": 2.0, "task-destination-1": 2.0},
    }
    regions["task-source-1"]["connections"].append("task-region-2")
    regions["task-source-1"]["connection_costs"]["task-region-2"] = 2.0
    regions["task-destination-1"]["connections"].append("task-region-2")
    regions["task-destination-1"]["connection_costs"]["task-region-2"] = 2.0
    payload["regions"] = regions
    regions["task-region-1"]["route_policy"] = "FORBID"
    planner_view = PlannerSceneView(payload, planner_view._reference_map)
    actions = adapt_spine_plan([("goto", "task-destination-1")])
    forbidden = CheckedRoute(
        0,
        "task-source-1",
        "task-destination-1",
        ("task-source-1", "task-region-1", "task-destination-1"),
    )
    alternative = CheckedRoute(
        0,
        "task-source-1",
        "task-destination-1",
        ("task-source-1", "task-region-2", "task-destination-1"),
    )

    rejected = MedicalTemporalGuard(mode="off").review(
        planner_view=planner_view,
        actions=actions,
        checked_route=forbidden,
    )
    allowed = MedicalTemporalGuard(mode="off").review(
        planner_view=planner_view,
        actions=actions,
        checked_route=alternative,
    )
    assert (rejected.decision, rejected.reason_code) == (
        REJECT,
        "MANDATORY_ROUTE_POLICY_VIOLATION",
    )
    assert allowed.decision == ALLOW


def test_generated_rule_payload_is_strictly_normalized():
    value = ContextualGrounding.normalize({
        "privacy": [{"ltl_constraint": "G(!goto(task-region-1))", "justification": "avoid"}]
    })
    assert value["privacy"][0] == {
        "constraint": "G(!goto(task-region-1))", "reasoning": "avoid"
    }


def test_non_json_model_response_fails_closed():
    class Completions:
        @staticmethod
        def create(**_):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="not-json")
            )])
    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    grounding = ContextualGrounding(rules=["rule"], model="fake", client=client)
    try:
        grounding.get_specifications(json.dumps({"regions": []}))
    except ValueError as error:
        assert "invalid JSON" in str(error)
    else:
        raise AssertionError("invalid model response must fail closed")
