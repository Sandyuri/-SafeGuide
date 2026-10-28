"""Deterministic hospital route planners and dynamic-event overlays.

The three planners intentionally consume the same immutable planner view:

``dijkstra``
    Shortest distance only.  It is the historical baseline.
``astar``
    Shortest distance with an admissible geometric heuristic.  Like Dijkstra,
    risk is reported but does not affect route generation.
``generate_then_check``
    Generate the shortest route, then reject it if a hard rule is violated.
``joint``
    Search over ``(map node, workflow state, plan version)`` while pruning
    hard-rule violations and comparing safe routes by risk, distance and time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from heapq import heappop, heappush
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from roboguard.medical.guard_types import ALLOW, PAUSE, REJECT
from roboguard.medical.planner_view import PlannerSceneView
from roboguard.medical.route_validation_types import RouteValidationReceipt
from roboguard.medical.scene import MedicalScene, MedicalSceneError


SUPPORTED_PLANNERS = ("joint", "dijkstra", "astar", "generate_then_check", "pareto")


def _load_risk_rules() -> tuple[dict[str, float], dict[str, float], float, float]:
    """Load planner weights from the same data file used by the validator.

    Only the rule data is shared.  The planner and validator retain separate
    implementations so a planner calculation bug cannot automatically make a
    route pass independent validation.
    """

    path = Path(__file__).resolve().parents[3] / "config" / "route_validation_rules.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        risk = payload["risk"]
        edge = {
            str(key).upper(): float(value)
            for key, value in risk["edge_risk_per_meter"].items()
        }
        fixed = {
            str(key).upper(): float(value)
            for key, value in risk["fixed_node_risk"].items()
        }
        congestion = float(risk["CONGESTION_MULTIPLIER"])
        nominal_speed = float(payload["nominal_speed_mps"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid route risk configuration {path}: {error}") from error
    if any(not math.isfinite(value) or value < 0 for value in (*edge.values(), *fixed.values(), congestion)):
        raise RuntimeError(f"route risk configuration contains an invalid weight: {path}")
    if not math.isfinite(nominal_speed) or nominal_speed <= 0:
        raise RuntimeError(f"route risk configuration contains an invalid speed: {path}")
    return edge, fixed, congestion, nominal_speed


(
    EDGE_RISK_PER_METER,
    FIXED_NODE_RISK,
    CONGESTION_MULTIPLIER,
    NOMINAL_SPEED_MPS,
) = _load_risk_rules()
EVENT_TYPES = frozenset(
    {
        "NODE_CLOSED",
        "NODE_BLOCKED",
        "EDGE_CLOSED",
        "EDGE_BLOCKED",
        "CONGESTION",
        "CROWDING",
        "EMERGENCY_CORRIDOR_ACTIVE",
        "ELEVATOR_UNAVAILABLE",
        "STATUS_EXPIRED",
        "EDGE_COST_CHANGED",
        "PATIENT_STATUS_CHANGED",
        "MEDICAL_RISK_CHANGED",
        "TARGET_CHANGED",
        "WORKFLOW_CHANGED",
        "PERMISSION_CHANGED",
        "PERMISSION_REVOKED",
        "CARGO_CHANGED",
    }
)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class DynamicEvent:
    event_type: str
    region_ids: tuple[str, ...]
    severity: float = 1.0
    event_id: str = ""
    reason: str = ""
    source_region_id: str | None = None
    target_region_id: str | None = None
    new_cost: float | None = None
    effective_at: str | None = None
    expires_at: str | None = None
    active: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], index: int = 0) -> "DynamicEvent":
        event_type = str(value.get("type", "")).strip().upper()
        event_type = {
            "ELEVATOR_OUTAGE": "ELEVATOR_UNAVAILABLE",
            "ELEVATOR_DISABLED": "ELEVATOR_UNAVAILABLE",
            "CROWDING": "CONGESTION",
        }.get(event_type, event_type)
        if event_type not in EVENT_TYPES:
            raise MedicalSceneError(f"unsupported dynamic event type: {event_type!r}")
        source_region_id = value.get("source_region_id")
        target_region_id = value.get("target_region_id")
        new_cost = value.get("new_cost")
        if event_type == "EDGE_COST_CHANGED":
            if not all(
                isinstance(item, str) and item
                for item in (source_region_id, target_region_id)
            ):
                raise MedicalSceneError(
                    "edge cost event requires source_region_id and target_region_id"
                )
            if isinstance(new_cost, bool) or not isinstance(new_cost, (int, float)):
                raise MedicalSceneError("edge cost event new_cost must be numeric")
            new_cost = float(new_cost)
            if not math.isfinite(new_cost) or new_cost <= 0:
                raise MedicalSceneError(
                    "edge cost event new_cost must be finite and positive"
                )
            raw_ids = [source_region_id, target_region_id]
        elif event_type in {"EDGE_CLOSED", "EDGE_BLOCKED"}:
            edge_id = value.get("edge_id")
            if not isinstance(edge_id, str) or not edge_id:
                raise MedicalSceneError("edge closure event requires edge_id")
            raw_ids = [edge_id]
        elif event_type in {"PERMISSION_CHANGED", "PERMISSION_REVOKED"}:
            permission = value.get("permission", value.get("permission_id"))
            if not isinstance(permission, str) or not permission:
                raise MedicalSceneError("permission event requires permission")
            raw_ids = [permission]
        else:
            raw_ids = value.get("region_ids")
            if raw_ids is None:
                raw_ids = [value.get("region_id")]
            if not isinstance(raw_ids, list) or not raw_ids or not all(
                isinstance(item, str) and item for item in raw_ids
            ):
                raise MedicalSceneError("dynamic event must contain region_id or region_ids")
        severity = value.get("severity", value.get("level", 1.0))
        if isinstance(severity, bool) or not isinstance(severity, (int, float)):
            raise MedicalSceneError("dynamic event severity must be numeric")
        severity = float(severity)
        if not math.isfinite(severity) or severity < 0:
            raise MedicalSceneError("dynamic event severity must be finite and nonnegative")
        effective_at = value.get("effective_at", value.get("starts_at"))
        expires_at = value.get("expires_at", value.get("ends_at"))
        for label, timestamp in (
            ("effective_at", effective_at),
            ("expires_at", expires_at),
        ):
            if timestamp is not None:
                if not isinstance(timestamp, str):
                    raise MedicalSceneError(f"dynamic event {label} must be a timestamp")
                try:
                    _parse_datetime(timestamp)
                except ValueError as error:
                    raise MedicalSceneError(
                        f"dynamic event {label} is not a valid ISO timestamp"
                    ) from error
        active = value.get("active", True)
        if not isinstance(active, bool):
            raise MedicalSceneError("dynamic event active must be boolean")
        return cls(
            event_type=event_type,
            region_ids=tuple(raw_ids),
            severity=severity,
            event_id=str(value.get("id", f"event-{index + 1}")),
            reason=str(value.get("reason", "")),
            source_region_id=source_region_id,
            target_region_id=target_region_id,
            new_cost=new_cost,
            effective_at=effective_at,
            expires_at=expires_at,
            active=active,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.event_id,
            "type": self.event_type,
            "region_ids": list(self.region_ids),
            "severity": self.severity,
            "reason": self.reason,
            "active": self.active,
        }
        if self.effective_at:
            payload["effective_at"] = self.effective_at
        if self.expires_at:
            payload["expires_at"] = self.expires_at
        if self.event_type == "EDGE_COST_CHANGED":
            payload.update(
                {
                    "source_region_id": self.source_region_id,
                    "target_region_id": self.target_region_id,
                    "new_cost": self.new_cost,
                }
            )
        elif self.event_type in {"EDGE_CLOSED", "EDGE_BLOCKED"}:
            payload["edge_id"] = self.region_ids[0]
        elif self.event_type in {"PERMISSION_CHANGED", "PERMISSION_REVOKED"}:
            payload["permission"] = self.region_ids[0]
        elif len(self.region_ids) == 1:
            payload["region_id"] = self.region_ids[0]
        return payload


@dataclass(frozen=True)
class DynamicEventSet:
    events: tuple[DynamicEvent, ...] = ()
    plan_version: int = 1

    @classmethod
    def empty(cls) -> "DynamicEventSet":
        return cls()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DynamicEventSet":
        raw_events = value.get("events", [])
        if not isinstance(raw_events, list):
            raise MedicalSceneError("dynamic event document events must be a list")
        version = value.get("plan_version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise MedicalSceneError("dynamic event plan_version must be a positive integer")
        return cls(
            tuple(DynamicEvent.from_mapping(item, index) for index, item in enumerate(raw_events)),
            version,
        )

    @classmethod
    def from_path(cls, path: str | Path) -> "DynamicEventSet":
        source = Path(path)
        try:
            value = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MedicalSceneError(f"cannot load dynamic events: {source}") from error
        if not isinstance(value, dict):
            raise MedicalSceneError("dynamic event document must be an object")
        return cls.from_mapping(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "plan_version": self.plan_version,
            "events": [event.to_dict() for event in self.events],
        }


@dataclass(frozen=True)
class WorkflowTransition:
    rule_id: str
    action: str
    from_state: str
    to_state: str
    accepted: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "action": self.action,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "accepted": self.accepted,
            "detail": self.detail,
        }


class WorkflowAutomaton:
    """Deterministic workflow for ordered arrivals, confirmation and handoff.

    The automaton accepts only predefined action methods.  LTLf formulas are
    emitted for audit; runtime decisions always come from this state machine.
    """

    def __init__(
        self,
        ordered_destinations: Iterable[str],
        *,
        plan_version: int = 1,
        confirmation_destinations: Iterable[str] = (),
        require_handoff: bool = False,
        require_explicit_finish: bool = False,
    ) -> None:
        self.destinations = tuple(ordered_destinations)
        if not self.destinations:
            raise MedicalSceneError("workflow requires at least one destination")
        confirmations = tuple(confirmation_destinations)
        if len(confirmations) != len(set(confirmations)):
            raise MedicalSceneError("workflow confirmation destinations contain duplicates")
        unknown_confirmations = sorted(set(confirmations) - set(self.destinations))
        if unknown_confirmations:
            raise MedicalSceneError(
                "workflow confirmation destination is not in ordered destinations: "
                + ", ".join(unknown_confirmations)
            )
        self.plan_version = plan_version
        self.confirmation_destinations = frozenset(confirmations)
        self.require_handoff = require_handoff
        self.require_explicit_finish = require_explicit_finish
        self.stage = 0
        self.paused = False
        self.pending_confirmation: str | None = None
        self.handoff_complete = False
        self.finished = False
        self.trace: list[WorkflowTransition] = []

    @property
    def state(self) -> str:
        if self.paused:
            return "PAUSED"
        if self.pending_confirmation is not None:
            return f"WAIT_CONFIRM_{self.stage + 1}"
        if self.stage < len(self.destinations):
            return f"GO_TO_{self.stage + 1}"
        if self.require_handoff and not self.handoff_complete:
            return "WAIT_HANDOFF"
        if self.require_explicit_finish and not self.finished:
            return "READY_TO_COMPLETE"
        return "COMPLETE"

    @property
    def complete(self) -> bool:
        return self.state == "COMPLETE"

    def arrive(self, region_id: str, *, plan_version: int | None = None) -> bool:
        previous = self.state
        if plan_version is not None and plan_version != self.plan_version:
            self.trace.append(WorkflowTransition(
                "WF-004", f"ARRIVE:{region_id}", previous, previous, False,
                "plan version is stale",
            ))
            return False
        if self.paused:
            self.trace.append(WorkflowTransition(
                "WF-003", f"ARRIVE:{region_id}", previous, previous, False,
                "paused workflow cannot continue",
            ))
            return False
        expected = self.destinations[self.stage] if self.stage < len(self.destinations) else None
        if region_id != expected:
            self.trace.append(WorkflowTransition(
                "WF-001", f"ARRIVE:{region_id}", previous, previous, False,
                f"expected {expected}",
            ))
            return False
        if region_id in self.confirmation_destinations:
            self.pending_confirmation = region_id
        else:
            self.stage += 1
        self.trace.append(WorkflowTransition(
            "WF-001", f"ARRIVE:{region_id}", previous, self.state, True,
            (
                "ordered destination reached; confirmation required"
                if self.pending_confirmation is not None
                else "ordered destination reached"
            ),
        ))
        return True

    def confirm(self, region_id: str, *, plan_version: int | None = None) -> bool:
        """Confirm completion of the current clinical or registration stage."""

        previous = self.state
        action = f"CONFIRM:{region_id}"
        if plan_version is not None and plan_version != self.plan_version:
            self.trace.append(WorkflowTransition(
                "WF-004", action, previous, previous, False,
                "plan version is stale",
            ))
            return False
        if self.paused:
            self.trace.append(WorkflowTransition(
                "WF-003", action, previous, previous, False,
                "paused workflow cannot continue",
            ))
            return False
        if self.pending_confirmation != region_id:
            self.trace.append(WorkflowTransition(
                "WF-002", action, previous, previous, False,
                f"expected confirmation for {self.pending_confirmation}",
            ))
            return False
        self.pending_confirmation = None
        self.stage += 1
        self.trace.append(WorkflowTransition(
            "WF-002", action, previous, self.state, True,
            "stage confirmation accepted",
        ))
        return True

    def pause(self) -> None:
        previous = self.state
        self.paused = True
        self.trace.append(WorkflowTransition("WF-003", "PAUSE", previous, self.state, True))

    def replan(self, new_version: int) -> None:
        if new_version <= self.plan_version:
            raise MedicalSceneError("replanned version must be newer than the current version")
        previous = self.state
        self.plan_version = new_version
        self.paused = False
        self.trace.append(WorkflowTransition(
            "WF-004", f"REPLAN:{new_version}", previous, self.state, True,
            "older plan versions are invalid",
        ))

    def handoff(self) -> bool:
        previous = self.state
        accepted = (
            self.stage == len(self.destinations)
            and self.pending_confirmation is None
            and not self.paused
        )
        if accepted:
            self.handoff_complete = True
        self.trace.append(WorkflowTransition(
            "WF-005", "HANDOFF", previous, self.state, accepted,
            "handoff is only valid after all destinations" if not accepted else "handoff complete",
        ))
        return accepted

    def finish(self) -> bool:
        """Finish only after ordered stages and any required handoff."""

        previous = self.state
        accepted = (
            self.stage == len(self.destinations)
            and self.pending_confirmation is None
            and not self.paused
            and (not self.require_handoff or self.handoff_complete)
        )
        if accepted:
            self.finished = True
        self.trace.append(WorkflowTransition(
            "WF-006", "FINISH", previous, self.state, accepted,
            (
                "workflow complete"
                if accepted
                else "finish requires all stages, confirmations, and handoff"
            ),
        ))
        return accepted

    def ltlf_formulas(self) -> tuple[str, ...]:
        formulas = [f"F(arrive_{index + 1})" for index in range(len(self.destinations))]
        formulas.extend(
            f"(!arrive_{index + 1}) U arrive_{index}"
            for index in range(1, len(self.destinations))
        )
        formulas.extend(
            f"G(arrive_{index + 1} -> X(confirm_{index + 1}))"
            for index, destination in enumerate(self.destinations)
            if destination in self.confirmation_destinations
        )
        formulas.append("G(paused -> X(paused | replan))")
        if self.require_handoff:
            formulas.append("F(handoff)")
            formulas.append("G(finish -> handoff_complete)")
        if self.require_explicit_finish:
            formulas.append("F(finish)")
        return tuple(formulas)


@dataclass(frozen=True)
class PlanResult:
    decision: str
    reason_code: str
    message: str
    planner: str
    plan_version: int
    route: tuple[str, ...] = ()
    distance_m: float = 0.0
    estimated_time_s: float = 0.0
    risk_score: float = 0.0
    risk_components: Mapping[str, float] = field(default_factory=dict)
    workflow_trace: tuple[WorkflowTransition, ...] = ()
    checked_rules: tuple[str, ...] = ()
    failed_node: str | None = None
    replan_allowed: bool = False
    expanded_states: int = 0
    selection_profile: str = ""
    selection_reason: str = ""
    candidate_count: int = 0
    candidate_comparisons: tuple[Mapping[str, Any], ...] = ()
    patient_burden: float = 0.0
    validation_receipt: RouteValidationReceipt | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason_code": self.reason_code,
            "message": self.message,
            "planner": self.planner,
            "plan_version": self.plan_version,
            "route": list(self.route),
            "distance_m": round(self.distance_m, 6),
            "estimated_time_s": round(self.estimated_time_s, 6),
            "risk_score": round(self.risk_score, 6),
            "patient_burden": round(self.patient_burden, 6),
            "risk_components": dict(self.risk_components),
            "workflow_trace": [item.to_dict() for item in self.workflow_trace],
            "checked_rules": list(self.checked_rules),
            "failed_node": self.failed_node,
            "replan_allowed": self.replan_allowed,
            "expanded_states": self.expanded_states,
            "selection_profile": self.selection_profile,
            "selection_reason": self.selection_reason,
            "candidate_count": self.candidate_count,
            "candidate_comparisons": [dict(item) for item in self.candidate_comparisons],
            "validation_receipt": (
                self.validation_receipt.to_dict()
                if self.validation_receipt is not None
                else None
            ),
        }


class HospitalRoutePlanner:
    """Run comparable baseline, post-check and joint route planners."""

    def __init__(self, planner: str = "joint") -> None:
        if planner not in SUPPORTED_PLANNERS:
            raise ValueError(f"unsupported planner: {planner}")
        self.planner = planner

    def plan(
        self,
        *,
        scene: MedicalScene,
        planner_view: PlannerSceneView,
        events: DynamicEventSet | None = None,
    ) -> PlanResult:
        overlay = events or DynamicEventSet.empty()
        if self.planner == "pareto":
            raise ValueError(
                "pareto requires ParetoRoutePlanner plus RouteSelector; use the navigation pipeline"
            )
        facility = scene.data.get("facility", {})
        if facility.get("emergency_status", "NONE") != "NONE":
            return PlanResult(
                PAUSE,
                "FACILITY_EMERGENCY_ACTIVE",
                "Navigation is paused while the facility emergency state is active or unknown.",
                self.planner,
                overlay.plan_version,
            )
        if facility.get("hazards"):
            return PlanResult(
                PAUSE,
                "FACILITY_HAZARD_REVIEW_REQUIRED",
                "A reported facility hazard requires route review.",
                self.planner,
                overlay.plan_version,
            )
        source = planner_view.payload["robot"]["current_region_ref"]
        destinations = tuple(
            planner_view.payload["task"].get(
                "ordered_destination_region_refs",
                (planner_view.payload["task"]["destination_region_ref"],),
            )
        )
        if self.planner == "joint":
            return self._joint(scene, planner_view, source, destinations, overlay)
        if self.planner == "astar":
            return self._astar_distance_only(
                planner_view, source, destinations, overlay
            )
        generated = self._distance_only(
            planner_view, source, destinations, overlay
        )
        if not generated.allowed:
            return generated
        if self.planner == "dijkstra":
            return generated
        violation = self._first_violation(
            scene, planner_view, generated.route, overlay, destinations
        )
        if violation:
            decision, reason, message, node = violation
            return PlanResult(
                decision, reason, message, self.planner, overlay.plan_version,
                route=generated.route,
                distance_m=generated.distance_m,
                estimated_time_s=generated.estimated_time_s,
                risk_score=generated.risk_score,
                risk_components=generated.risk_components,
                failed_node=node,
                replan_allowed=True,
                checked_rules=self._checked_rules(),
                expanded_states=generated.expanded_states,
            )
        return PlanResult(
            **{**generated.__dict__, "planner": self.planner, "checked_rules": self._checked_rules()}
        )

    def replan(
        self,
        *,
        scene: MedicalScene,
        planner_view: PlannerSceneView,
        previous: PlanResult,
        events: DynamicEventSet,
    ) -> PlanResult:
        """Invalidate an affected old version and plan against new events."""

        if events.plan_version <= previous.plan_version:
            return PlanResult(
                PAUSE,
                "PLAN_VERSION_NOT_NEWER",
                "Replanning requires a newer plan version.",
                self.planner,
                previous.plan_version,
                route=previous.route,
                replan_allowed=True,
            )
        if previous.allowed and not route_affected_by_events(
            previous.route, planner_view, events
        ):
            return PlanResult(
                **{**previous.__dict__, "plan_version": events.plan_version}
            )
        result = self.plan(scene=scene, planner_view=planner_view, events=events)
        if result.decision == REJECT:
            return PlanResult(
                PAUSE,
                "SAFE_PAUSE_NO_REPLAN_ROUTE",
                "The old route is invalid and no safe replacement is available.",
                self.planner,
                events.plan_version,
                failed_node=result.failed_node,
                replan_allowed=True,
                expanded_states=result.expanded_states,
                checked_rules=result.checked_rules,
            )
        return result

    @staticmethod
    def _checked_rules() -> tuple[str, ...]:
        return (
            "ROUTE_CONNECTIVITY",
            "ROUTE_REGION_AVAILABLE",
            "ROUTE_ACCESS_PERMISSION",
            "ROUTE_CARGO_COMPATIBILITY",
            "ROUTE_ACCESSIBILITY",
            "ROUTE_POLICY",
            "ROUTE_MEDICAL_RISK",
            "WORKFLOW_ORDER",
            "PLAN_VERSION_CURRENT",
        )

    def _distance_only(self, view, source, destinations, overlay) -> PlanResult:
        regions = view.payload["regions"]
        plan_version = overlay.plan_version
        route: list[str] = [source]
        total = 0.0
        expanded = 0
        current = source
        workflow = WorkflowAutomaton(destinations, plan_version=plan_version)
        for target in destinations:
            pending = [(0.0, (current,), current)]
            best = {current: 0.0}
            segment = None
            while pending:
                distance, path, node = heappop(pending)
                expanded += 1
                if distance != best.get(node):
                    continue
                if node == target:
                    segment = path
                    total += distance
                    break
                for neighbor in sorted(regions[node]["connections"]):
                    edge = regions[node].get("connection_costs", {}).get(neighbor)
                    if not self._valid_edge(edge) or neighbor not in regions:
                        continue
                    candidate = distance + float(edge)
                    if candidate < best.get(neighbor, math.inf):
                        best[neighbor] = candidate
                        heappush(pending, (candidate, path + (neighbor,), neighbor))
            if segment is None:
                return PlanResult(
                    REJECT, "ROUTE_NOT_FOUND", f"No route connects {current} to {target}.",
                    self.planner, plan_version, replan_allowed=False,
                    expanded_states=expanded,
                )
            route.extend(segment[1:])
            workflow.arrive(target, plan_version=plan_version)
            current = target
        risk_components = self._route_risk_components(view, route, overlay)
        elapsed = self._path_elapsed(view, route, overlay)
        return PlanResult(
            ALLOW, "PLAN_ALLOWED", "Distance-only route generated.", self.planner,
            plan_version, tuple(route), total, elapsed,
            sum(risk_components.values()), risk_components,
            tuple(workflow.trace), (), expanded_states=expanded,
        )

    def _astar_distance_only(
        self, view, source, destinations, overlay
    ) -> PlanResult:
        """Generate a shortest route with A* and no safety-rule pruning.

        The heuristic is scaled by the minimum edge-cost/geometric-distance
        ratio, so it never overestimates the remaining stored map cost.  This
        makes the result distance-equivalent to Dijkstra while usually
        expanding fewer states on maps with useful coordinates.
        """

        regions = view.payload["regions"]
        plan_version = overlay.plan_version
        heuristic_scale = self._heuristic_scale(regions)
        route: list[str] = [source]
        total = 0.0
        expanded = 0
        current = source
        workflow = WorkflowAutomaton(destinations, plan_version=plan_version)
        for target in destinations:
            start_h = self._heuristic(
                regions, current, (target,), 0, heuristic_scale
            )
            pending = [(start_h, 0.0, (current,), current)]
            best = {current: 0.0}
            segment = None
            while pending:
                _, distance, path, node = heappop(pending)
                expanded += 1
                if distance != best.get(node):
                    continue
                if node == target:
                    segment = path
                    total += distance
                    break
                for neighbor in sorted(regions[node]["connections"]):
                    edge = regions[node].get("connection_costs", {}).get(neighbor)
                    if not self._valid_edge(edge) or neighbor not in regions:
                        continue
                    candidate = distance + float(edge)
                    if candidate >= best.get(neighbor, math.inf):
                        continue
                    best[neighbor] = candidate
                    heuristic = self._heuristic(
                        regions, neighbor, (target,), 0, heuristic_scale
                    )
                    heappush(
                        pending,
                        (candidate + heuristic, candidate, path + (neighbor,), neighbor),
                    )
            if segment is None:
                return PlanResult(
                    REJECT,
                    "ROUTE_NOT_FOUND",
                    f"No route connects {current} to {target}.",
                    self.planner,
                    plan_version,
                    replan_allowed=False,
                    expanded_states=expanded,
                )
            route.extend(segment[1:])
            workflow.arrive(target, plan_version=plan_version)
            current = target
        risk_components = self._route_risk_components(view, route, overlay)
        elapsed = self._path_elapsed(view, route, overlay)
        return PlanResult(
            ALLOW,
            "ASTAR_PLAN_ALLOWED",
            "Distance-optimal A* route generated.",
            self.planner,
            plan_version,
            tuple(route),
            total,
            elapsed,
            sum(risk_components.values()),
            risk_components,
            tuple(workflow.trace),
            (),
            expanded_states=expanded,
        )

    def _joint(self, scene, view, source, destinations, overlay) -> PlanResult:
        regions = view.payload["regions"]
        initial_stage = 0
        while initial_stage < len(destinations) and source == destinations[initial_stage]:
            initial_stage += 1
        # Lexicographic constrained A*: medical risk first, then distance and time.
        # The geometry scale makes the straight-line distance admissible even if
        # map edge weights are not stored in metres.
        heuristic_scale = self._heuristic_scale(regions)
        start_state = (source, initial_stage, overlay.plan_version)
        initial_h = self._heuristic(
            regions, source, destinations, initial_stage, heuristic_scale
        )
        pending = [(0.0, initial_h, initial_h, (source,), start_state, 0.0, 0.0, {})]
        best: dict[tuple[str, int, int], tuple[float, float, float]] = {
            start_state: (0.0, 0.0, 0.0)
        }
        expanded = 0
        paused_violation = None
        while pending:
            risk, estimated_distance, estimated_time, path, state, travelled, elapsed, components = heappop(pending)
            node, stage, version = state
            known = best.get(state)
            if known is None or (risk, travelled, elapsed) != known:
                continue
            expanded += 1
            if stage == len(destinations):
                workflow = WorkflowAutomaton(destinations, plan_version=version)
                for destination in destinations:
                    workflow.arrive(destination, plan_version=version)
                return PlanResult(
                    ALLOW,
                    "JOINT_PLAN_ALLOWED",
                    "Route and workflow satisfy all hard constraints.",
                    self.planner,
                    version,
                    path,
                    travelled,
                    elapsed,
                    risk,
                    dict(sorted(components.items())),
                    tuple(workflow.trace),
                    self._checked_rules(),
                    expanded_states=expanded,
                )
            for neighbor in sorted(regions[node]["connections"]):
                if neighbor not in regions:
                    continue
                edge = regions[node].get("connection_costs", {}).get(neighbor)
                if not self._valid_edge(edge):
                    continue
                expected_destination = destinations[stage]
                violation = self._node_violation(
                    scene, view, neighbor, overlay, expected_destination
                )
                if violation:
                    if violation[0] == PAUSE and paused_violation is None:
                        paused_violation = violation
                    continue
                next_stage = stage
                if next_stage < len(destinations) and neighbor == destinations[next_stage]:
                    next_stage += 1
                next_state = (neighbor, next_stage, version)
                edge_distance = float(edge)
                node_components = self._risk_components(
                    regions[neighbor],
                    overlay,
                    neighbor,
                    self._real_id(view, neighbor),
                    edge_distance=edge_distance,
                    edge_risk_rates=regions[node].get(
                        "connection_risk_rates", {}
                    ).get(neighbor),
                )
                next_components = dict(components)
                for key, value in node_components.items():
                    next_components[key] = next_components.get(key, 0.0) + value
                next_risk = sum(next_components.values())
                congestion = self._congestion(
                    overlay, neighbor, self._real_id(view, neighbor)
                )
                next_distance = travelled + edge_distance
                next_elapsed = elapsed + edge_distance * (1.0 + congestion)
                score = (next_risk, next_distance, next_elapsed)
                if score >= best.get(next_state, (math.inf, math.inf, math.inf)):
                    continue
                best[next_state] = score
                heuristic = self._heuristic(
                    regions, neighbor, destinations, next_stage, heuristic_scale
                )
                heappush(
                    pending,
                    (
                        next_risk,
                        next_distance + heuristic,
                        next_elapsed + heuristic / NOMINAL_SPEED_MPS,
                        path + (neighbor,),
                        next_state,
                        next_distance,
                        next_elapsed,
                        next_components,
                    ),
                )
        if paused_violation:
            decision, reason, message, node = paused_violation
            return PlanResult(
                decision, reason, message, self.planner, overlay.plan_version,
                failed_node=node, replan_allowed=True,
                checked_rules=self._checked_rules(), expanded_states=expanded,
            )
        return PlanResult(
            REJECT, "NO_ALLOWED_ROUTE", "No route satisfies all spatial and workflow constraints.",
            self.planner, overlay.plan_version, replan_allowed=True,
            checked_rules=self._checked_rules(), expanded_states=expanded,
        )

    def _first_violation(self, scene, view, route, overlay, destinations):
        stage = 0
        # The robot may leave its current region even if that region is marked
        # destination-only. Every entered node is checked against the current
        # workflow destination.
        for node in route[1:]:
            destination = destinations[min(stage, len(destinations) - 1)]
            violation = self._node_violation(scene, view, node, overlay, destination)
            if violation:
                return violation
            if stage < len(destinations) and node == destinations[stage]:
                stage += 1
        return None

    def _node_violation(self, scene, view, node, overlay, destination):
        region = view.payload["regions"][node]
        real_id = self._real_id(view, node)
        event_types = {
            event.event_type
            for event in overlay.events
            if event.active and (real_id in event.region_ids or node in event.region_ids)
        }
        if event_types.intersection({"NODE_CLOSED", "NODE_BLOCKED"}):
            return REJECT, "DYNAMIC_NODE_BLOCKED", f"Dynamic event blocks {node}.", node
        if "ELEVATOR_UNAVAILABLE" in event_types and "ELEVATOR" in region["type"].upper():
            return REJECT, "ELEVATOR_UNAVAILABLE", f"Elevator is unavailable at {node}.", node
        if (
            "EMERGENCY_CORRIDOR_ACTIVE" in event_types
            and view.payload["task"].get("urgency") != "EMERGENCY"
        ):
            return REJECT, "EMERGENCY_CORRIDOR_ACTIVE", f"Active emergency corridor blocks {node}.", node
        status = region.get("navigation_status", "OPEN")
        if status in {"UNKNOWN", "CONFLICTED"}:
            return PAUSE, "ROUTE_STATUS_UNKNOWN", f"Navigation status is unreliable for {node}.", node
        if status == "BLOCKED":
            return REJECT, "ROUTE_REGION_BLOCKED", f"Region {node} is blocked.", node
        valid_until = region.get("navigation_status_valid_until")
        if "STATUS_EXPIRED" in event_types or (
            valid_until and _parse_datetime(scene.data["current_time"]) >= _parse_datetime(valid_until)
        ):
            return PAUSE, "ROUTE_STATUS_EXPIRED", f"Navigation status expired for {node}.", node
        permissions = set(view.payload["robot"]["access_permissions"])
        if not set(region.get("required_permissions", ())).issubset(permissions):
            return REJECT, "ROUTE_ACCESS_DENIED", f"Robot lacks permission for {node}.", node
        cargo_kinds = self._cargo_kinds(scene, view)
        conflict = cargo_kinds.intersection(region.get("prohibited_cargo_kinds", ()))
        if conflict:
            return REJECT, "ROUTE_CARGO_RESTRICTED", f"Cargo is prohibited at {node}: {sorted(conflict)}.", node
        needs = set(view.payload["task"].get("accessibility_needs", ()))
        vertical_barriers = {"STAIR_HALL", "ESCALATOR_HALL", "STAIR", "ESCALATOR"}
        if (
            needs.intersection({"WHEELCHAIR_ACCESSIBLE", "STEP_FREE"})
            and region["type"].upper() in vertical_barriers
        ):
            return REJECT, "ROUTE_NOT_ACCESSIBLE", f"Step-free task cannot enter {node}.", node
        policy = region.get("route_policy", "ALLOW_TRANSIT")
        if policy == "FORBID":
            return REJECT, "ROUTE_POLICY_FORBIDS_ENTRY", f"Route policy forbids {node}.", node
        if policy == "DESTINATION_ONLY" and node != destination:
            return REJECT, "ROUTE_POLICY_DESTINATION_ONLY", f"{node} cannot be used for transit.", node
        if policy == "EMERGENCY_ONLY_ACTIVE" and view.payload["task"].get("urgency") != "EMERGENCY":
            return REJECT, "ROUTE_POLICY_EMERGENCY_ONLY", f"{node} is reserved for emergencies.", node
        return None

    @staticmethod
    def _risk_components(
        region,
        overlay,
        node,
        real_id=None,
        *,
        edge_distance=0.0,
        edge_risk_rates=None,
    ) -> dict[str, float]:
        tags = {str(tag).upper() for tag in region.get("semantic_tags", ())}
        components: dict[str, float] = {}
        for tag, weight in FIXED_NODE_RISK.items():
            if tag in tags:
                components[tag.lower()] = weight
        rates = edge_risk_rates
        if rates is None:
            rates = {
                tag.lower(): rate
                for tag, rate in EDGE_RISK_PER_METER.items()
                if tag in tags
            }
        for component, rate in rates.items():
            value = float(rate) * float(edge_distance)
            if value:
                components[str(component).lower()] = value
        congestion = HospitalRoutePlanner._congestion(overlay, node, real_id)
        if congestion:
            components["congestion"] = congestion * CONGESTION_MULTIPLIER
        return components

    @staticmethod
    def _congestion(overlay, node, real_id=None) -> float:
        return sum(
            event.severity
            for event in overlay.events
            if event.active
            and event.event_type in {"CONGESTION", "CROWDING"}
            and (node in event.region_ids or real_id in event.region_ids)
        )

    @staticmethod
    def _cargo_kinds(scene, view) -> set[str]:
        kinds = set()
        for reference in view.payload["task"].get("carrying_refs", ()):
            try:
                item_id = view.resolve_reference(reference)
            except Exception:
                continue
            item = scene.data["delivery_items"].get(item_id)
            if item:
                kinds.add(item["kind"])
        return kinds

    @staticmethod
    def _real_id(view, reference: str) -> str:
        try:
            return view.resolve_reference(reference)
        except Exception:
            return reference

    @staticmethod
    def _valid_edge(value) -> bool:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and value > 0
        )

    @staticmethod
    def _heuristic_scale(regions) -> float:
        """Return the largest safe conversion from map geometry to edge cost."""

        ratios = []
        for node, region in regions.items():
            left = region.get("coords", ())
            if len(left) < 2:
                continue
            for neighbor in region.get("connections", ()):
                if neighbor not in regions:
                    continue
                edge = region.get("connection_costs", {}).get(neighbor)
                right = regions[neighbor].get("coords", ())
                if not HospitalRoutePlanner._valid_edge(edge) or len(right) < 2:
                    continue
                geometric = math.dist(
                    tuple(float(item) for item in left[:2]),
                    tuple(float(item) for item in right[:2]),
                )
                if geometric > 0:
                    ratios.append(float(edge) / geometric)
        return min(ratios) if ratios else 0.0

    @staticmethod
    def _heuristic(regions, node, destinations, stage, scale=1.0) -> float:
        if stage >= len(destinations):
            return 0.0
        waypoints = (node,) + tuple(destinations[stage:])
        lower_bound = 0.0
        for left_id, right_id in zip(waypoints, waypoints[1:]):
            left = regions[left_id].get("coords", ())
            right = regions[right_id].get("coords", ())
            if len(left) < 2 or len(right) < 2:
                continue
            lower_bound += math.dist(
                tuple(float(item) for item in left[:2]),
                tuple(float(item) for item in right[:2]),
            )
        return lower_bound * scale

    def _route_risk_components(self, view, route, overlay) -> dict[str, float]:
        components: dict[str, float] = {}
        regions = view.payload["regions"]
        for previous, node in zip(route, route[1:]):
            edge_distance = float(regions[previous]["connection_costs"][node])
            additions = self._risk_components(
                regions[node],
                overlay,
                node,
                self._real_id(view, node),
                edge_distance=edge_distance,
                edge_risk_rates=regions[previous].get(
                    "connection_risk_rates", {}
                ).get(node),
            )
            for key, value in additions.items():
                components[key] = components.get(key, 0.0) + value
        return dict(sorted(components.items()))

    def _path_elapsed(self, view, route, overlay) -> float:
        regions = view.payload["regions"]
        elapsed = 0.0
        for left, right in zip(route, route[1:]):
            distance = float(regions[left]["connection_costs"][right])
            congestion = self._congestion(
                overlay, right, self._real_id(view, right)
            )
            elapsed += distance * (1.0 + congestion) / NOMINAL_SPEED_MPS
        return elapsed

def route_affected_by_events(
    route: Iterable[str],
    planner_view: PlannerSceneView,
    events: DynamicEventSet,
) -> bool:
    """Return whether an existing route must be invalidated and replanned."""

    disruptive = {
        "NODE_CLOSED",
        "NODE_BLOCKED",
        "CONGESTION",
        "CROWDING",
        "EMERGENCY_CORRIDOR_ACTIVE",
        "ELEVATOR_UNAVAILABLE",
        "STATUS_EXPIRED",
        "EDGE_COST_CHANGED",
    }
    identities = set(route)
    for reference in tuple(identities):
        try:
            identities.add(planner_view.resolve_reference(reference))
        except Exception:
            pass
    return any(
        event.event_type in disruptive and identities.intersection(event.region_ids)
        for event in events.events
    )
