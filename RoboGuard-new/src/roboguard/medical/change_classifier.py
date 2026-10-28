"""Classify navigation changes as local repairs or full replans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from roboguard.medical.joint_planner import DynamicEventSet


NO_CHANGE = "NO_CHANGE"
LOCAL_REPAIR = "LOCAL_REPAIR"
FULL_PARETO_REPLAN = "FULL_PARETO_REPLAN"

LOCAL_EVENT_TYPES = frozenset(
    {
        "NODE_CLOSED",
        "NODE_BLOCKED",
        "ELEVATOR_UNAVAILABLE",
        "STATUS_EXPIRED",
        "EDGE_COST_CHANGED",
    }
)
MAJOR_EVENT_TYPES = frozenset(
    {
        "CONGESTION",
        "CROWDING",
        "EMERGENCY_CORRIDOR_ACTIVE",
        "PATIENT_STATUS_CHANGED",
        "MEDICAL_RISK_CHANGED",
        "TARGET_CHANGED",
        "WORKFLOW_CHANGED",
        "PERMISSION_CHANGED",
        "CARGO_CHANGED",
    }
)
MAJOR_FIELDS = frozenset(
    {
        "target",
        "ordered_destinations",
        "confirmation_destinations",
        "require_handoff",
        "require_explicit_finish",
        "patient_status",
        "access_permissions",
        "carrying",
        "urgency",
        "risk_policy",
    }
)


@dataclass(frozen=True)
class ChangeClassification:
    action: str
    reasons: tuple[str, ...] = ()

    @property
    def local(self) -> bool:
        return self.action == LOCAL_REPAIR

    @property
    def requires_full_replan(self) -> bool:
        return self.action == FULL_PARETO_REPLAN


class NavigationChangeClassifier:
    """Fail closed when a change can alter task meaning or medical risk."""

    def classify(
        self,
        events: DynamicEventSet,
        *,
        changed_fields: Iterable[str] = (),
    ) -> ChangeClassification:
        fields = tuple(sorted(set(changed_fields)))
        event_types = tuple(sorted({event.event_type for event in events.events}))
        major_reasons = [f"event:{item}" for item in event_types if item in MAJOR_EVENT_TYPES]
        major_reasons.extend(f"field:{item}" for item in fields if item in MAJOR_FIELDS)
        unknown_events = sorted(
            set(event_types) - LOCAL_EVENT_TYPES - MAJOR_EVENT_TYPES
        )
        unknown_fields = sorted(set(fields) - MAJOR_FIELDS)
        major_reasons.extend(f"unknown_event:{item}" for item in unknown_events)
        major_reasons.extend(f"unknown_field:{item}" for item in unknown_fields)
        if major_reasons:
            return ChangeClassification(FULL_PARETO_REPLAN, tuple(major_reasons))
        local_reasons = [f"event:{item}" for item in event_types if item in LOCAL_EVENT_TYPES]
        if local_reasons:
            return ChangeClassification(LOCAL_REPAIR, tuple(local_reasons))
        return ChangeClassification(NO_CHANGE)
