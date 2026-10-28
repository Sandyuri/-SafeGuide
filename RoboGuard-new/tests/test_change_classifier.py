"""Tests for local-versus-major navigation change classification."""

from roboguard.medical.change_classifier import (
    FULL_PARETO_REPLAN,
    LOCAL_REPAIR,
    NO_CHANGE,
    NavigationChangeClassifier,
)
from roboguard.medical.joint_planner import DynamicEventSet


def event(event_type):
    return DynamicEventSet.from_mapping(
        {
            "plan_version": 2,
            "events": [{"type": event_type, "region_id": "node-1"}],
        }
    )


def test_local_obstacle_changes_use_dstar_lite():
    result = NavigationChangeClassifier().classify(event("NODE_BLOCKED"))
    assert result.action == LOCAL_REPAIR
    assert result.local


def test_medical_risk_change_requires_full_pareto_search():
    result = NavigationChangeClassifier().classify(
        event("MEDICAL_RISK_CHANGED")
    )
    assert result.action == FULL_PARETO_REPLAN
    assert result.requires_full_replan


def test_target_and_workflow_fields_require_full_replan():
    result = NavigationChangeClassifier().classify(
        DynamicEventSet(events=(), plan_version=2),
        changed_fields=("target", "require_handoff"),
    )
    assert result.action == FULL_PARETO_REPLAN
    assert result.reasons == ("field:require_handoff", "field:target")


def test_empty_change_set_does_nothing():
    result = NavigationChangeClassifier().classify(
        DynamicEventSet(events=(), plan_version=2)
    )
    assert result.action == NO_CHANGE


def test_unknown_change_fails_closed_to_full_replan():
    result = NavigationChangeClassifier().classify(
        DynamicEventSet(events=(), plan_version=2),
        changed_fields=("unrecognized_state",),
    )
    assert result.action == FULL_PARETO_REPLAN
    assert result.reasons == ("unknown_field:unrecognized_state",)
