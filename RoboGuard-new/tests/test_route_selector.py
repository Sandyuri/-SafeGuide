"""Tests for deterministic task-profile route selection."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from roboguard.medical.guard_types import ALLOW
from roboguard.medical.pareto_types import ParetoLabel, ParetoPlanResult, ProductState
from roboguard.medical.route_selector import RouteSelector
from roboguard.medical.route_validation_types import RouteValidationResult
from roboguard.medical.task_contracts import RouteContext, TaskSpec


def candidate(name, *, risk, distance, time=None, burden=0, state="COMPLETE"):
    validation = RouteValidationResult(
        "VALID",
        (),
        {
            "distance_m": distance,
            "travel_time_s": distance if time is None else time,
            "risk_score": risk,
            "risk_components": {},
        },
        {"completed_destinations": ["d"], "final_state": "COMPLETED"},
        ("GRAPH-001",),
        "test.v1",
        1,
    )
    return ParetoLabel(
        ProductState("d", state, 1),
        risk,
        distance,
        distance if time is None else time,
        burden,
        ("s", name, "d"),
        validation=validation,
    )


def pareto(*candidates):
    return ParetoPlanResult(
        ALLOW,
        "PARETO_CANDIDATES_READY",
        "ready",
        1,
        tuple(candidates),
        checked_rules=("ROUTE_CONNECTIVITY",),
    )


def task(**changes):
    base = TaskSpec("NAVIGATE_TO_LOCATION", "s", ("d",))
    return replace(base, **changes)


def context():
    return RouteContext(
        "auth-1", "request-1", "ALLOW_TO_ENTER_PLANNING",
        "snapshot-1", "hash-1", "hospital_1f", 1,
        "2026-10-20T08:00:00Z",
    )


def select(result, task_spec=None):
    return RouteSelector().select(
        result,
        task=task_spec or task(),
        context=context(),
        require_validation=True,
    )


def test_routine_profile_selects_safer_route_inside_detour_budget():
    shortest = candidate("short", risk=20, distance=10)
    safer = candidate("safe", risk=0, distance=12)
    result = select(pareto(shortest, safer))
    assert result.allowed
    assert result.task_profile == "ROUTINE"
    assert result.selected_candidate == safer
    assert result.candidate_count == 2


def test_routine_profile_rejects_excessive_detour_from_eligible_set():
    shortest = candidate("short", risk=20, distance=10)
    detour = candidate("detour", risk=0, distance=13)
    result = select(pareto(shortest, detour))
    assert result.selected_candidate == shortest
    excluded = next(item for item in result.comparisons if item.route == detour.route)
    assert excluded.eligible is False
    assert "distance exceeds" in excluded.exclusion_reason


def test_wheelchair_profile_prioritizes_patient_burden():
    shorter = candidate("stairs-like", risk=0, distance=10, burden=5)
    easier = candidate("easy", risk=3, distance=14, burden=0)
    result = select(
        pareto(shorter, easier),
        task(accessibility_needs=("WHEELCHAIR_ACCESSIBLE",)),
    )
    assert result.task_profile == "WHEELCHAIR"
    assert result.selected_candidate == easier


def test_assistance_profile_prioritizes_patient_burden():
    tiring = candidate("tiring", risk=0, distance=10, burden=4)
    calm = candidate("calm", risk=2, distance=14, burden=0)
    result = select(
        pareto(tiring, calm),
        task(accessibility_needs=("ASSISTANCE_REQUIRED",)),
    )
    assert result.task_profile == "MOBILITY_ASSISTED"
    assert result.selected_candidate == calm


def test_emergency_profile_selects_fastest_candidate_under_risk_limit():
    safe = candidate("safe", risk=0, distance=12, time=12)
    fast = candidate("fast", risk=20, distance=10, time=8)
    over_limit = candidate("over", risk=31, distance=8, time=6)
    result = select(
        pareto(safe, fast, over_limit),
        task(urgency="EMERGENCY"),
    )
    assert result.task_profile == "EMERGENCY"
    assert result.selected_candidate == fast
    assert next(
        item for item in result.comparisons if item.route == over_limit.route
    ).eligible is False


def test_carrying_task_uses_cargo_profile():
    shorter = candidate("short", risk=5, distance=10)
    safer = candidate("safe", risk=0, distance=12)
    result = select(
        pareto(shorter, safer),
        task(carrying_item_ids=("package-1",)),
    )
    assert result.task_profile == "CARGO"
    assert result.selected_candidate == safer


def test_incomplete_workflow_cannot_be_selected():
    incomplete = candidate("pending", risk=0, distance=10, state="WAIT_HANDOFF")
    result = select(pareto(incomplete))
    assert result.decision == "PAUSE"
    assert result.reason_code == "WORKFLOW_NOT_COMPLETE"
    assert result.selected_candidate is None


def test_unvalidated_candidate_cannot_be_selected():
    unvalidated = ParetoLabel(
        ProductState("d", "COMPLETE", 1),
        0,
        10,
        10,
        0,
        ("s", "unchecked", "d"),
    )
    result = select(pareto(unvalidated))
    assert result.decision == "PAUSE"
    assert result.reason_code == "CANDIDATE_VALIDATION_REQUIRED"


def test_selection_is_deterministic_and_plan_result_keeps_audit():
    choices = pareto(
        candidate("b", risk=0, distance=10),
        candidate("a", risk=0, distance=10),
    )
    outputs = [select(choices) for _ in range(5)]
    assert {item.selected_candidate.route for item in outputs} == {("s", "a", "d")}
    planned = outputs[0].to_plan_result(checked_rules=choices.checked_rules)
    assert planned.planner == "pareto"
    assert planned.selection_profile == "ROUTINE"
    assert planned.candidate_count == 2
    assert len(planned.candidate_comparisons) == 2


def test_emergency_profile_pauses_when_every_candidate_exceeds_risk_limit():
    result = select(
        pareto(
            candidate("fast", risk=31, distance=8, time=6),
            candidate("slow", risk=35, distance=12, time=10),
        ),
        task(urgency="EMERGENCY"),
    )
    assert result.decision == "PAUSE"
    assert result.reason_code == "NO_CANDIDATE_WITHIN_PROFILE_LIMITS"


def test_configuration_rejects_arbitrary_strategy(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "default_profile": "ROUTINE",
                "profiles": {
                    "ROUTINE": {
                        "strategy": "execute_model_formula",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported route selection strategy"):
        RouteSelector(path)
