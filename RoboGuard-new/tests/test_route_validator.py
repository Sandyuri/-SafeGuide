"""Fault-injection tests for the planner-independent route validator."""

from __future__ import annotations

from copy import deepcopy
import ast
import json
from pathlib import Path

import pytest

from roboguard.medical.route_validator import IndependentRouteValidator


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture()
def validator(tmp_path: Path) -> IndependentRouteValidator:
    nodes = [
        {"id": "A", "access_zone": "public", "navigation_status": "open"},
        {"id": "B", "access_zone": "public", "navigation_status": "open"},
        {"id": "C", "access_zone": "controlled", "navigation_status": "open_with_authorization"},
        {"id": "D", "access_zone": "public", "navigation_status": "open"},
        {"id": "S", "access_zone": "public", "navigation_status": "open", "has_stairs": True},
        {"id": "X", "access_zone": "public", "navigation_status": "open"},
    ]
    edges = [
        {"id": "E-AB", "from": "A", "to": "B", "bidirectional": True, "distance_m": 10, "access_zone": "public", "navigation_status": "open"},
        {"id": "E-BC", "from": "B", "to": "C", "bidirectional": False, "distance_m": 5, "access_zone": "controlled", "navigation_status": "open_with_authorization"},
        {"id": "E-CD", "from": "C", "to": "D", "bidirectional": True, "distance_m": 3, "access_zone": "public", "navigation_status": "open"},
        {"id": "E-AS", "from": "A", "to": "S", "bidirectional": True, "distance_m": 2, "access_zone": "public", "navigation_status": "open", "has_stairs": True, "wheelchair_accessible": False},
        {"id": "E-CX", "from": "C", "to": "X", "bidirectional": True, "distance_m": 2, "access_zone": "public", "navigation_status": "open"},
    ]
    _write(tmp_path / "navigation.json", {"map_id": "test", "nodes": nodes, "edges": edges})
    _write(tmp_path / "access.json", {"zones": [
        {"id": "public", "required_roles": []},
        {"id": "controlled", "required_roles": ["staff"]},
    ]})
    _write(tmp_path / "bindings.json", {"zone_permissions": {
        "public": [], "controlled": ["ENTER_CONTROLLED"]
    }})
    _write(tmp_path / "semantics.json", {
        "defaults": {"semantic_tags": [], "route_policy": "ALLOW_TRANSIT"},
        "nodes": {
            "B": {"semantic_tags": ["NO_STOPPING"]},
            "C": {
                "semantic_tags": [],
                "route_policy": "DESTINATION_ONLY",
                "prohibited_cargo_kinds": ["HAZARDOUS"],
            },
        },
    })
    _write(tmp_path / "risk.json", {"risk_zones": [{
        "risk_zone_id": "emergency-test",
        "type": "EMERGENCY_CORRIDOR",
        "base_risk_per_meter": 999.0,
        "hard_constraint_when_active": True,
        "affected_edges": ["E-AB"],
        "affected_nodes": ["A", "B"],
    }]})
    _write(tmp_path / "manifest.json", {"map_release_id": "test.v1"})
    _write(tmp_path / "rules.json", {
        "nominal_speed_mps": 1.2,
        "metric_tolerance": {"distance_m": 0.001, "travel_time_s": 0.01, "risk_score": 0.001},
        "risk": {
            "edge_risk_per_meter": {
                "EMERGENCY_CORRIDOR": 2,
                "CRITICAL_ACCESS": 3,
            },
            "fixed_node_risk": {
                "NO_STOPPING": 5,
                "CONTROLLED_RESOURCE_AREA": 4,
            },
            "CONGESTION_MULTIPLIER": 10,
        },
        "accessibility": {"default_min_width_m": 0.9, "unknown_width_policy": "allow"},
        "open_statuses": ["open", "open_with_authorization"],
        "task_only_statuses": ["task_only"],
    })
    return IndependentRouteValidator(
        tmp_path,
        navigation_path=tmp_path / "navigation.json",
        access_path=tmp_path / "access.json",
        bindings_path=tmp_path / "bindings.json",
        semantics_path=tmp_path / "semantics.json",
        risk_zones_path=tmp_path / "risk.json",
        manifest_path=tmp_path / "manifest.json",
        rules_path=tmp_path / "rules.json",
    )


def valid_request() -> dict[str, object]:
    return {
        "task": {
            "source": "A",
            "ordered_destinations": ["C"],
            "permissions": ["ENTER_CONTROLLED"],
            "roles": ["staff"],
            "start_time": "2026-10-23T08:00:00Z",
        },
        "route": ["A", "B", "C"],
        "map_version": "test.v1",
        "plan_version": 2,
        "current_plan_version": 2,
        "events": [],
        "planner_reported": {
            "distance_m": 15,
            "travel_time_s": 12.5,
            "risk_score": 25,
        },
    }


def rule_ids(result) -> set[str]:
    return {item.rule_id for item in result.violations}


def test_valid_route_and_independent_metrics(validator):
    request = valid_request()
    result = validator.validate(request)
    assert result.valid
    assert result.recomputed == {
        "distance_m": 15.0,
        "travel_time_s": 12.5,
        "risk_score": 25.0,
        "risk_components": {"emergency_corridor": 20.0, "no_stopping": 5.0},
    }
    assert result.workflow["final_state"] == "COMPLETED"
    assert result.receipt is not None
    assert result.receipt.receipt_id.startswith("sha256:")
    assert result.receipt.route == tuple(request["route"])
    assert result.to_dict()["receipt"]["plan_version"] == request["plan_version"]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda r: r.update(route=[]), "GRAPH-001"),
        (lambda r: r["task"].update(source="B"), "GRAPH-002"),
        (lambda r: r["task"].update(ordered_destinations=["D"]), "GRAPH-003"),
        (lambda r: r.update(route=["A", "UNKNOWN", "C"]), "GRAPH-006"),
        (lambda r: r.update(route=["A", "D", "C"]), "GRAPH-004"),
        (lambda r: (r["task"].update(source="C", ordered_destinations=["A"]), r.update(route=["C", "B", "A"])), "GRAPH-005"),
        (lambda r: r["task"].update(permissions=[]), "ACCESS-001"),
        (lambda r: (r["task"].update(ordered_destinations=["D"]), r.update(route=["A", "B", "C", "D"])), "ACCESS-003"),
        (lambda r: (r["task"].update(ordered_destinations=["S"], wheelchair=True), r.update(route=["A", "S"])), "ACCESSIBILITY-001"),
        (lambda r: (r["task"].update(ordered_destinations=["D", "C"]), r.update(route=["A", "B", "C", "D", "C"])), "WORKFLOW-001"),
        (lambda r: r.update(events=[{"type": "NODE_CLOSED", "node_id": "B"}]), "EVENT-001"),
        (lambda r: r.update(events=[{"type": "EDGE_CLOSED", "edge_id": "E-AB"}]), "EVENT-002"),
        (lambda r: r.update(events=[{"type": "EMERGENCY_CORRIDOR_ACTIVE", "risk_zone_id": "emergency-test"}]), "EVENT-003"),
        (lambda r: r.update(current_plan_version=3), "VERSION-002"),
        (lambda r: r.update(map_version="test.v0"), "VERSION-001"),
        (lambda r: r["planner_reported"].update(distance_m=1), "METRIC-001"),
        (lambda r: r["planner_reported"].update(risk_score=0), "RISK-001"),
    ],
)
def test_fault_injection_rules(validator, mutate, expected):
    request = deepcopy(valid_request())
    mutate(request)
    result = validator.validate(request)
    assert result.decision == "INVALID"
    assert expected in rule_ids(result)


def test_dynamic_event_is_checked_at_estimated_arrival_time(validator):
    future = deepcopy(valid_request())
    future["events"] = [{
        "type": "NODE_CLOSED", "node_id": "B",
        "effective_at": "2026-10-23T08:00:20Z",
    }]
    assert validator.validate(future).valid

    during_route = deepcopy(valid_request())
    during_route["events"] = [{
        "type": "NODE_CLOSED", "node_id": "B",
        "effective_at": "2026-10-23T08:00:05Z",
    }]
    assert "EVENT-001" in rule_ids(validator.validate(during_route))


def test_required_workflow_actions_must_be_reported_in_order(validator):
    request = deepcopy(valid_request())
    request["task"].update({
        "confirmation_destinations": ["C"],
        "require_handoff": True,
        "require_explicit_finish": True,
    })
    assert "WORKFLOW-003" in rule_ids(validator.validate(request))
    request["workflow_trace"] = [
        ["WF-001", "ARRIVE:C", "GO_TO_1", "WAIT_CONFIRM_1", True],
        ["WF-002", "CONFIRM:C", "WAIT_CONFIRM_1", "WAIT_HANDOFF", True],
        ["WF-005", "HANDOFF", "WAIT_HANDOFF", "READY_TO_COMPLETE", True],
        ["WF-006", "FINISH", "READY_TO_COMPLETE", "COMPLETE", True],
    ]
    assert validator.validate(request).valid


def test_dynamic_cost_and_congestion_change_recomputed_metrics(validator):
    request = deepcopy(valid_request())
    request["events"] = [
        {
            "type": "EDGE_COST_CHANGED",
            "edge_id": "E-AB",
            "new_cost": 12,
        },
        {
            "type": "CROWDING",
            "region_ids": ["B", "unused"],
            "severity": 0.5,
        },
    ]
    request["planner_reported"] = {
        "distance_m": 17,
        "travel_time_s": 19.166667,
        "risk_score": 34,
    }
    result = validator.validate(request)
    assert result.valid, result.to_dict()
    assert result.recomputed == {
        "distance_m": 17.0,
        "travel_time_s": 19.166667,
        "risk_score": 34.0,
        "risk_components": {
            "congestion": 5.0,
            "emergency_corridor": 24.0,
            "no_stopping": 5.0,
        },
    }


def test_prohibited_cargo_is_rejected(validator):
    request = deepcopy(valid_request())
    request["task"]["cargo_kinds"] = ["HAZARDOUS"]
    assert "ACCESS-004" in rule_ids(validator.validate(request))


def test_same_input_is_deterministic(validator):
    first = validator.validate(valid_request()).to_dict()
    second = validator.validate(valid_request()).to_dict()
    assert first == second


def test_validator_source_has_no_planner_imports():
    source = (Path(__file__).parents[1] / "src/roboguard/medical/route_validator.py").read_text(encoding="utf-8")
    imported_modules = {
        node.module or ""
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = ("joint_planner", "pareto_planner", "route_selector", "dstar_lite")
    assert not any(
        name in module
        for name in forbidden
        for module in imported_modules
    )


def test_real_hospital_map_smoke_test():
    root = Path(__file__).parents[1]
    validator = IndependentRouteValidator(root)
    request = {
        "task": {"source": "nav-f1-corridor-01", "destination": "nav-f1-corridor-02"},
        "route": ["nav-f1-corridor-01", "nav-f1-waypoint-001", "nav-f1-corridor-02"],
        "map_version": "hospital_1f.v1.0",
        "plan_version": 1,
        "events": [],
        "planner_reported": {"distance_m": 6.603, "travel_time_s": 5.5025, "risk_score": 0},
    }
    result = validator.validate(request)
    assert result.valid, result.to_dict()
