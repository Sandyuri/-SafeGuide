"""Tests for the unified route execution safety barrier."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from roboguard.medical.dstar_lite import IncrementalNavigationReplanner
from roboguard.medical.guard_types import CheckedRoute
from roboguard.medical.joint_planner import DynamicEventSet
from roboguard.medical.pareto_planner import ParetoRoutePlanner
from roboguard.medical.route_selector import RouteSelector
from roboguard.medical.route_validation_types import (
    RouteValidationReceipt,
    RouteValidationResult,
)
from roboguard.medical.safety_barrier import (
    ExecutionPermit,
    UnifiedSafetyBarrier,
    require_execution_permit,
)
from roboguard.medical.task_contracts import RouteContext, TaskSpec

from test_dstar_lite import repair_graph
from test_joint_planner import Scene, events, make_view


def task():
    return TaskSpec("NAVIGATE_TO_LOCATION", "real-s", ("real-d",))


def context(overlay=None):
    dynamic_events = tuple(
        event.to_dict() for event in (overlay or DynamicEventSet.empty()).events
    )
    return RouteContext(
        "eval-1", "request-1", "ALLOW_TO_ENTER_PLANNING",
        "snapshot-1", "hash-1", "test", 1,
        "2026-10-20T08:00:00Z", dynamic_events,
    )


def selected(view, overlay=None):
    overlay = overlay or DynamicEventSet.empty()
    candidates = ParetoRoutePlanner().plan(
        scene=Scene(), planner_view=view, events=overlay
    )
    validated = []
    for candidate in candidates.candidates:
        recomputed = {
            "distance_m": candidate.distance_m,
            "travel_time_s": candidate.estimated_time_s,
            "risk_score": candidate.risk,
            "risk_components": dict(candidate.risk_components),
        }
        trace = tuple(
            (
                item[0],
                item[1].replace("ARRIVE:d", "ARRIVE:real-d"),
                item[2],
                item[3],
                item[4],
            )
            for item in candidate.workflow_trace
        )
        receipt = RouteValidationReceipt.issue(
            route=tuple(view.resolve_reference(node) for node in candidate.route),
            map_id="test",
            map_version="test.v1",
            context_map_version=1,
            plan_version=overlay.plan_version,
            events=(),
            workflow_trace=trace,
            checked_rules=("GRAPH-001",),
            recomputed=recomputed,
        )
        validated.append(
            replace(
                candidate,
                validation=RouteValidationResult(
                    "VALID",
                    (),
                    recomputed,
                    {"completed_destinations": ["real-d"], "final_state": "COMPLETED"},
                    ("GRAPH-001",),
                    "test.v1",
                    overlay.plan_version,
                    receipt,
                ),
            )
        )
    validated = tuple(validated)
    candidates = replace(
        candidates,
        reason_code="PARETO_CANDIDATES_VALIDATED",
        candidates=validated,
    )
    selection = RouteSelector().select(
        candidates, task=task(), context=context()
    )
    return selection.to_plan_result(checked_rules=candidates.checked_rules)


def review(view, proposed, overlay=None, source="INITIAL_PARETO"):
    return UnifiedSafetyBarrier().review(
        source_type=source,
        proposed=proposed,
        scene=Scene(),
        planner_view=view,
        task=task(),
        context=context(overlay),
        events=overlay or DynamicEventSet.empty(),
    )


def test_barrier_issues_execution_permit_and_hash_certificate():
    view = make_view()
    proposed = selected(view)
    result = review(view, proposed)
    assert result.allowed
    assert result.reason_code == "EXECUTION_PERMIT_ISSUED"
    assert result.permit.route.execution_authorized is True
    assert result.permit.certificate_id.startswith("sha256:")
    assert result.certificate["authorization_id"] == "eval-1"
    assert result.certificate["scene_snapshot_hash"] == "hash-1"
    assert result.certificate["result"]["route"] == list(proposed.route)
    assert "risk_components" in result.certificate["result"]
    require_execution_permit(result.permit.route)
    json.dumps(result.certificate)


def test_barrier_rejects_plan_without_independent_validation_receipt():
    view = make_view()
    proposed = replace(selected(view), validation_receipt=None)
    result = review(view, proposed)
    assert result.decision == "REJECT"
    assert result.reason_code == "VALIDATION_RECEIPT_MISSING"
    assert result.permit is None


def test_barrier_rejects_tampered_validation_receipt():
    view = make_view()
    proposed = selected(view)
    tampered = replace(
        proposed.validation_receipt,
        route=proposed.validation_receipt.route + ("forged",),
    )
    result = review(view, replace(proposed, validation_receipt=tampered))
    assert result.decision == "REJECT"
    assert result.reason_code == "VALIDATION_RECEIPT_CORRUPT"


def test_tampered_distance_is_rejected_by_metric_recomputation():
    view = make_view()
    proposed = replace(selected(view), distance_m=999)
    result = review(view, proposed)
    assert result.decision == "REJECT"
    assert result.reason_code == "VALIDATION_RECEIPT_METRIC_MISMATCH"
    assert result.permit is None


def test_stale_plan_version_is_paused():
    view = make_view()
    overlay = events({"type": "NODE_BLOCKED", "region_id": "unused"}, version=2)
    result = review(view, selected(view), overlay)
    assert result.decision == "PAUSE"
    assert result.reason_code == "PLAN_VERSION_STALE"


def test_new_dynamic_block_is_rechecked_at_barrier():
    view = make_view()
    proposed = selected(view)
    blocked = proposed.route[1]
    overlay = events(
        {"type": "NODE_BLOCKED", "region_id": view.resolve_reference(blocked)},
        version=2,
    )
    result = review(view, replace(proposed, plan_version=2), overlay)
    assert result.decision == "PAUSE"
    assert result.reason_code == "VALIDATION_PLAN_VERSION_MISMATCH"


def test_event_set_must_be_bound_into_route_context():
    view = make_view()
    overlay = events({"type": "NODE_BLOCKED", "region_id": "unused"}, version=2)
    proposed = replace(selected(view), plan_version=2)
    result = UnifiedSafetyBarrier().review(
        source_type="INITIAL_PARETO",
        proposed=proposed,
        scene=Scene(),
        planner_view=view,
        task=task(),
        context=context(),
        events=overlay,
    )
    assert result.decision == "PAUSE"
    assert result.reason_code == "DYNAMIC_EVENT_BINDING_MISMATCH"


def test_incomplete_workflow_trace_cannot_receive_permit():
    view = make_view()
    proposed = replace(selected(view), workflow_trace=())
    result = review(view, proposed)
    assert result.decision == "REJECT"
    assert result.reason_code == "VALIDATION_WORKFLOW_BINDING_MISMATCH"


def test_plan_result_and_unapproved_checked_route_cannot_bypass_barrier():
    view = make_view()
    proposed = selected(view)
    unchecked = CheckedRoute(
        action_index=0,
        source=proposed.route[0],
        destination=proposed.route[-1],
        regions=proposed.route,
        scene_snapshot_hash=Scene.snapshot_hash,
        planner=proposed.planner,
        plan_version=proposed.plan_version,
    )
    assert unchecked.execution_authorized is False
    with pytest.raises(ValueError, match="no unified safety barrier"):
        require_execution_permit(unchecked)
    with pytest.raises(ValueError, match="only be issued"):
        ExecutionPermit(unchecked, "fake")


def test_execution_permit_rejects_stale_plan_version():
    view = make_view()
    result = review(view, selected(view))
    assert result.allowed
    require_execution_permit(result.permit.route, expected_plan_version=1)
    with pytest.raises(ValueError, match="stale plan version"):
        require_execution_permit(result.permit.route, expected_plan_version=2)


def test_synthetic_dstar_repair_fails_closed_against_authoritative_map():
    view = make_view(graph=repair_graph())
    old = selected(view)
    overlay = events({"type": "NODE_BLOCKED", "region_id": "real-x"})
    replanned = IncrementalNavigationReplanner().replan(
        scene=Scene(), planner_view=view, previous=old,
        task=task(), context=context(), events=overlay,
    )
    assert replanned.mode == "FULL_PARETO_REPLAN"
    assert replanned.result.reason_code == "NO_VALIDATED_PARETO_CANDIDATE"
    result = review(
        view, replanned.result, overlay, source="FULL_PARETO_REPLAN"
    )
    assert not result.allowed
    assert result.decision == "PAUSE"


def test_major_change_full_pareto_result_passes_same_barrier():
    view = make_view(graph=repair_graph())
    old = selected(view)
    overlay = events({
        "type": "PATIENT_STATUS_CHANGED",
        "region_id": "real-s",
    })
    replanned = IncrementalNavigationReplanner().replan(
        scene=Scene(), planner_view=view, previous=old,
        task=task(), context=context(), events=overlay,
    )
    assert replanned.mode == "FULL_PARETO_REPLAN"
    result = review(
        view, replanned.result, overlay, source="FULL_PARETO_REPLAN"
    )
    assert not result.allowed
    assert result.decision == "PAUSE"
    assert result.certificate["source_type"] == "FULL_PARETO_REPLAN"
