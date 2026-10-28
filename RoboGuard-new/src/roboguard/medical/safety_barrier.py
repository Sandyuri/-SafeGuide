"""Unified execution safety barrier for every navigation route source."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Mapping

from roboguard.medical.guard_types import ALLOW, PAUSE, REJECT, CheckedRoute, StepResult
from roboguard.medical.joint_planner import (
    DynamicEventSet,
    PlanResult,
    WorkflowAutomaton,
)
from roboguard.medical.planner_view import PlannerSceneView
from roboguard.medical.route_validation_types import RouteValidationReceipt
from roboguard.medical.scene import MedicalScene
from roboguard.medical.task_contracts import RouteContext, TaskSpec


_PERMIT_TOKEN = object()
ALLOWED_ROUTE_SOURCES = frozenset(
    {
        "INITIAL_PARETO",
        "DSTAR_LITE_REPAIR",
        "FULL_PARETO_REPLAN",
        "JOINT_ASTAR",
        "ASTAR_BASELINE",
        "GENERATE_THEN_CHECK",
        "DIJKSTRA_BASELINE",
    }
)


@dataclass(frozen=True, init=False)
class ExecutionPermit:
    """Unforgeable-by-construction wrapper issued only by this barrier."""

    route: CheckedRoute
    certificate_id: str

    def __init__(self, route: CheckedRoute, certificate_id: str, *, _token=None):
        if _token is not _PERMIT_TOKEN:
            raise ValueError("ExecutionPermit can only be issued by UnifiedSafetyBarrier")
        object.__setattr__(self, "route", route)
        object.__setattr__(self, "certificate_id", certificate_id)


@dataclass(frozen=True)
class SafetyBarrierResult:
    decision: str
    reason_code: str
    message: str
    source_type: str
    checked_rules: tuple[str, ...]
    certificate: Mapping[str, object]
    permit: ExecutionPermit | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW and self.permit is not None

    def to_step_result(self) -> StepResult:
        if self.permit is None:
            return StepResult(
                self.decision,
                self.reason_code,
                self.message,
                violated_rules=(self.reason_code,),
                replan_allowed=self.decision != ALLOW,
            )
        route = self.permit.route
        return StepResult.allow(
            route=route,
            checked_ltl=WorkflowAutomaton([route.destination]).ltlf_formulas(),
            temporal_trace=tuple(
                (item[1], item[4]) for item in route.workflow_trace
            ),
        )


class UnifiedSafetyBarrier:
    """Bind a valid independent receipt, then issue the execution permit."""

    CHECKS = (
        "VALIDATION_RECEIPT_VALID",
        "VALIDATION_ROUTE_BINDING",
        "AUTHORIZATION_BINDING",
        "SCENE_HASH_BINDING",
        "MAP_VERSION_BINDING",
        "PLAN_VERSION_CURRENT",
        "DYNAMIC_EVENT_BINDING",
        "VALIDATION_METRIC_BINDING",
        "VALIDATION_WORKFLOW_BINDING",
    )

    def review(
        self,
        *,
        source_type: str,
        proposed: PlanResult,
        scene: MedicalScene,
        planner_view: PlannerSceneView,
        task: TaskSpec,
        context: RouteContext,
        events: DynamicEventSet,
    ) -> SafetyBarrierResult:
        if source_type not in ALLOWED_ROUTE_SOURCES:
            return self._deny(
                REJECT, "UNKNOWN_ROUTE_SOURCE", "Route source is not trusted.",
                source_type, proposed, scene, context,
            )
        if not proposed.allowed or not proposed.route:
            decision = proposed.decision if proposed.decision in {PAUSE, REJECT} else REJECT
            return self._deny(
                decision, proposed.reason_code, proposed.message,
                source_type, proposed, scene, context,
            )
        view_context = planner_view.payload.get("route_context", {})
        if (
            context.authorization_id != planner_view.payload.get("evaluation_id")
            or context.request_id != planner_view.payload.get("request_id")
            or view_context.get("authorization_id") != context.authorization_id
            or context.authorization_scope != "ALLOW_TO_ENTER_PLANNING"
        ):
            return self._deny(
                REJECT, "AUTHORIZATION_BINDING_MISMATCH",
                "Route is not bound to the active SafeGate authorization.",
                source_type, proposed, scene, context,
            )
        if (
            context.scene_snapshot_id != scene.snapshot_id
            or context.scene_snapshot_hash != scene.snapshot_hash
            or planner_view.payload.get("snapshot_hash") != scene.snapshot_hash
        ):
            return self._deny(
                REJECT, "SCENE_HASH_MISMATCH",
                "Route and authorization do not use the active scene snapshot.",
                source_type, proposed, scene, context,
            )
        if proposed.plan_version != events.plan_version:
            return self._deny(
                PAUSE, "PLAN_VERSION_STALE",
                "Route plan version does not match current dynamic events.",
                source_type, proposed, scene, context,
            )
        context_events = json.loads(
            json.dumps(list(context.dynamic_events), default=dict)
        )
        active_events = events.to_dict()["events"]
        if context_events != active_events:
            return self._deny(
                PAUSE, "DYNAMIC_EVENT_BINDING_MISMATCH",
                "Route context does not contain the active dynamic event set.",
                source_type, proposed, scene, context,
            )
        receipt = proposed.validation_receipt
        if receipt is None or receipt.decision != "VALID":
            return self._deny(
                REJECT,
                "VALIDATION_RECEIPT_MISSING",
                "An independently validated route receipt is required.",
                source_type, proposed, scene, context,
            )
        if not receipt.is_intact():
            return self._deny(
                REJECT,
                "VALIDATION_RECEIPT_CORRUPT",
                "The independent validation receipt failed its integrity check.",
                source_type, proposed, scene, context,
            )
        if (
            receipt.map_id != context.map_id
            or receipt.context_map_version != str(context.map_version)
            or not receipt.map_version
        ):
            return self._deny(
                REJECT,
                "VALIDATION_MAP_BINDING_MISMATCH",
                "The validation receipt is not bound to the active map version.",
                source_type, proposed, scene, context,
            )
        if receipt.plan_version != proposed.plan_version:
            return self._deny(
                PAUSE,
                "VALIDATION_PLAN_VERSION_MISMATCH",
                "The validation receipt is not bound to this plan version.",
                source_type, proposed, scene, context,
            )
        receipt_events = self._receipt_events(events)
        if receipt.event_digest != RouteValidationReceipt.digest_events(receipt_events):
            return self._deny(
                PAUSE,
                "VALIDATION_EVENT_BINDING_MISMATCH",
                "The validation receipt does not contain the active event set.",
                source_type, proposed, scene, context,
            )
        try:
            validated_route = tuple(
                planner_view.resolve_reference(node) for node in proposed.route
            )
        except Exception:
            return self._deny(
                REJECT,
                "VALIDATION_ROUTE_BINDING_MISMATCH",
                "The proposed route contains an unresolved reference.",
                source_type, proposed, scene, context,
            )
        if validated_route != receipt.route:
            return self._deny(
                REJECT,
                "VALIDATION_ROUTE_BINDING_MISMATCH",
                "The validation receipt belongs to a different route.",
                source_type, proposed, scene, context,
            )
        risk_components = dict(receipt.risk_components)
        claimed = (
            ("distance", proposed.distance_m, receipt.distance_m),
            ("risk", proposed.risk_score, receipt.risk_score),
            ("time", proposed.estimated_time_s, receipt.travel_time_s),
        )
        mismatch = next(
            (
                name for name, expected, actual in claimed
                if not math.isclose(expected, actual, rel_tol=1e-9, abs_tol=1e-6)
            ),
            None,
        )
        if mismatch or dict(proposed.risk_components) != risk_components:
            return self._deny(
                REJECT, "VALIDATION_RECEIPT_METRIC_MISMATCH",
                f"Plan {mismatch or 'risk components'} do not match the validation receipt.",
                source_type, proposed, scene, context,
            )
        actual_trace = self._canonical_workflow_trace(
            proposed.workflow_trace, planner_view
        )
        if actual_trace != receipt.workflow_trace:
            return self._deny(
                REJECT, "VALIDATION_WORKFLOW_BINDING_MISMATCH",
                "The plan workflow differs from the independently validated trace.",
                source_type, proposed, scene, context,
            )

        certificate = self._certificate(
            ALLOW, "EXECUTION_PERMIT_ISSUED", source_type, proposed,
            scene, context, extra={
                "checked_rules": list(self.CHECKS),
                "validation_receipt": receipt.to_dict(),
            },
        )
        certificate_id = str(certificate["certificate_id"])
        route = tuple(proposed.route)
        checked = CheckedRoute(
            action_index=0,
            source=route[0],
            destination=route[-1],
            regions=route,
            scene_snapshot_hash=scene.snapshot_hash,
            checked_properties=self.CHECKS,
            unchecked_properties=("DOOR_STATE", "PASSAGE_CLEARANCE", "LIVE_SENSOR_OBSTACLES"),
            distance_m=receipt.distance_m,
            risk_score=receipt.risk_score,
            patient_burden=proposed.patient_burden,
            estimated_time_s=receipt.travel_time_s,
            plan_version=events.plan_version,
            planner=proposed.planner,
            risk_components=tuple(sorted(risk_components.items())),
            workflow_trace=actual_trace,
            selection_profile=proposed.selection_profile,
            selection_reason=proposed.selection_reason,
            candidate_count=proposed.candidate_count,
            candidate_comparisons=tuple(
                dict(item) for item in proposed.candidate_comparisons
            ),
            execution_authorized=True,
            safety_certificate_id=certificate_id,
            safety_certificate=certificate,
        )
        permit = ExecutionPermit(checked, certificate_id, _token=_PERMIT_TOKEN)
        return SafetyBarrierResult(
            ALLOW,
            "EXECUTION_PERMIT_ISSUED",
            "The route passed the unified execution safety barrier.",
            source_type,
            self.CHECKS,
            certificate,
            permit,
        )

    @staticmethod
    def _receipt_events(events: DynamicEventSet) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for event in events.events:
            payload = event.to_dict()
            if len(event.region_ids) == 1:
                payload["region_id"] = event.region_ids[0]
            result.append(payload)
        return result

    @staticmethod
    def _canonical_workflow_trace(trace, view: PlannerSceneView):
        result = []
        for item in trace:
            action = item.action
            for prefix in ("ARRIVE:", "CONFIRM:"):
                if action.startswith(prefix):
                    action = prefix + view.resolve_reference(
                        action.removeprefix(prefix)
                    )
            result.append(
                (item.rule_id, action, item.from_state, item.to_state, item.accepted)
            )
        return tuple(result)

    def _deny(self, decision, reason, message, source, proposed, scene, context):
        certificate = self._certificate(
            decision, reason, source, proposed, scene, context,
            extra={"checked_rules": list(self.CHECKS)},
        )
        return SafetyBarrierResult(
            decision, reason, message, source, self.CHECKS, certificate
        )

    @staticmethod
    def _certificate(decision, reason, source, proposed, scene, context, *, extra):
        payload = {
            "schema_version": "1.0",
            "decision": decision,
            "reason_code": reason,
            "source_type": source,
            "authorization_id": context.authorization_id,
            "request_id": context.request_id,
            "scene_snapshot_id": scene.snapshot_id,
            "scene_snapshot_hash": scene.snapshot_hash,
            "map_id": context.map_id,
            "map_version": context.map_version,
            "plan_version": proposed.plan_version,
            "result": proposed.to_dict(),
            **extra,
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return {
            **payload,
            "certificate_id": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        }


def require_execution_permit(
    route: CheckedRoute,
    *,
    expected_plan_version: int | None = None,
) -> None:
    """Reject any route that did not pass the unified safety barrier."""

    if not route.execution_authorized or not route.safety_certificate_id:
        raise ValueError("route has no unified safety barrier execution permit")
    if (
        expected_plan_version is not None
        and route.plan_version != expected_plan_version
    ):
        raise ValueError(
            "execution permit belongs to a stale plan version: "
            f"expected {expected_plan_version}, got {route.plan_version}"
        )
