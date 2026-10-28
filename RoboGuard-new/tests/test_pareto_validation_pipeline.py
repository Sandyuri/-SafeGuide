"""Integration tests for validation between Pareto search and selection."""

from dataclasses import replace

from roboguard.medical.joint_planner import DynamicEventSet
from roboguard.medical.pareto_planner import ParetoRoutePlanner
from roboguard.medical.pareto_types import ParetoPlanResult
from roboguard.medical.route_selector import RouteSelector
from roboguard.medical.task_contracts import build_route_context, build_task_spec
from roboguard.navigation import _validate_pareto_candidates, build_navigation_view

from test_task_contracts import authorized_task, provider_and_scene


def inputs():
    provider, scene = provider_and_scene()
    task = authorized_task(scene, provider.resolve_location("药房"))
    return (
        scene,
        build_navigation_view(scene, task),
        build_task_spec(scene, task),
        build_route_context(scene, task),
    )


def test_validated_candidates_expose_both_metric_sources():
    scene, view, task, context = inputs()
    raw = ParetoRoutePlanner().plan(scene=scene, planner_view=view)
    checked = _validate_pareto_candidates(
        raw,
        planner_view=view,
        task=task,
        context=context,
        events=DynamicEventSet.empty(),
    )
    assert checked.allowed
    assert all(candidate.validated for candidate in checked.candidates)
    payload = checked.candidates[0].to_dict()
    assert payload["candidate_id"].startswith("pareto-")
    assert payload["planner_metrics"]
    assert payload["validation"]["decision"] == "VALID"
    assert payload["validation"]["receipt"]["receipt_id"].startswith("sha256:")
    assert payload["selection_metrics"] == {
        **payload["validation"]["recomputed"],
        "patient_burden": checked.candidates[0].patient_burden,
    }


def test_selected_plan_preserves_immutable_validation_receipt():
    scene, view, task, context = inputs()
    raw = ParetoRoutePlanner().plan(scene=scene, planner_view=view)
    checked = _validate_pareto_candidates(
        raw,
        planner_view=view,
        task=task,
        context=context,
        events=DynamicEventSet.empty(),
    )
    selected = RouteSelector().select(
        checked, task=task, context=context, require_validation=True
    ).to_plan_result(checked_rules=checked.checked_rules)

    assert selected.validation_receipt is not None
    assert selected.validation_receipt == checked.candidates[0].validation.receipt
    assert selected.to_dict()["validation_receipt"]["receipt_id"].startswith("sha256:")


def test_metric_tampering_removes_candidate_before_selection():
    scene, view, task, context = inputs()
    raw = ParetoRoutePlanner().plan(scene=scene, planner_view=view)
    tampered = replace(raw.candidates[0], risk=999)
    proposal = ParetoPlanResult(
        raw.decision,
        raw.reason_code,
        raw.message,
        raw.plan_version,
        (tampered,),
        checked_rules=raw.checked_rules,
    )
    checked = _validate_pareto_candidates(
        proposal,
        planner_view=view,
        task=task,
        context=context,
        events=DynamicEventSet.empty(),
    )
    assert checked.decision == "PAUSE"
    assert checked.reason_code == "NO_VALIDATED_PARETO_CANDIDATE"
    assert checked.candidates == ()
    assert checked.rejected_candidates[0].validation.decision == "INVALID"
    assert any(
        violation.rule_id == "RISK-001"
        for violation in checked.rejected_candidates[0].validation.violations
    )
