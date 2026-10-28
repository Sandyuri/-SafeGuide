"""Shared result and predicted-state types for medical plan review."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

ALLOW = "ALLOW"
PAUSE = "PAUSE"
REJECT = "REJECT"


@dataclass(frozen=True)
class CheckedRoute:
    """One concrete route approved for a navigation action."""

    action_index: int
    source: str
    destination: str
    regions: tuple[str, ...]
    route_version: str = "1.0.0"
    scene_snapshot_hash: str = ""
    semantic_tags_checked: tuple[str, ...] = ()
    checked_properties: tuple[str, ...] = (
        "GRAPH_CONNECTIVITY",
        "REGION_ACCESS",
        "AREA_STATUS",
        "FACILITY_EMERGENCY",
        "KNOWN_HAZARDS",
    )
    unchecked_properties: tuple[str, ...] = (
        "DOOR_STATE",
        "PASSAGE_CLEARANCE",
        "DYNAMIC_OBSTACLES",
    )
    distance_m: float = 0.0
    risk_score: float = 0.0
    patient_burden: float = 0.0
    estimated_time_s: float = 0.0
    plan_version: int = 1
    planner: str = "dijkstra"
    risk_components: tuple[tuple[str, float], ...] = ()
    workflow_trace: tuple[tuple[str, str, str, str, bool], ...] = ()
    selection_profile: str = ""
    selection_reason: str = ""
    candidate_count: int = 0
    candidate_comparisons: tuple[dict[str, object], ...] = ()
    execution_authorized: bool = False
    safety_certificate_id: str = ""
    safety_certificate: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewResult:
    """Auditable three-state result from any medical guard."""

    decision: str
    reason_code: str
    message: str
    failed_step: int | None = None
    checked_routes: tuple[CheckedRoute, ...] = ()
    violated_rules: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    replan_allowed: bool = False
    matched_natural_language_rules: tuple[str, ...] = ()
    checked_ltl: tuple[str, ...] = ()
    failed_route_node: str | None = None
    temporal_trace: tuple[tuple[str, bool], ...] = ()
    planning_result: Any | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW


@dataclass(frozen=True)
class StepResult:
    decision: str
    reason_code: str
    message: str
    required_evidence: tuple[str, ...] = ()
    violated_rules: tuple[str, ...] = ()
    replan_allowed: bool = False
    route: CheckedRoute | None = None
    matched_rules: tuple[str, ...] = ()
    checked_ltl: tuple[str, ...] = ()
    failed_route_node: str | None = None
    temporal_trace: tuple[tuple[str, bool], ...] = ()

    @classmethod
    def allow(
        cls,
        *,
        route: CheckedRoute | None = None,
        matched_rules: tuple[str, ...] = (),
        checked_ltl: tuple[str, ...] = (),
        temporal_trace: tuple[tuple[str, bool], ...] = (),
    ) -> "StepResult":
        return cls(
            ALLOW,
            "STEP_ALLOWED",
            "The step passed.",
            route=route,
            matched_rules=matched_rules,
            checked_ltl=checked_ltl,
            temporal_trace=temporal_trace,
        )


