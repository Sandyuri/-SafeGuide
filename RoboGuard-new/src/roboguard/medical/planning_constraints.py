"""Hard constraints used while route planners generate candidates.

This module deliberately belongs to the planning side of the architecture.
It helps planners avoid producing obviously illegal routes, but it is not a
substitute for the independent post-planning route validator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from typing import Any

from roboguard.medical.guard_types import PAUSE, REJECT


Violation = tuple[str, str, str, str]


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event_active(event: Any, current_time: str, arrival_time_s: float) -> bool:
    if not event.active:
        return False
    when = _parse_datetime(current_time) + timedelta(seconds=arrival_time_s)
    begins = _parse_datetime(event.effective_at) if event.effective_at else None
    ends = _parse_datetime(event.expires_at) if event.expires_at else None
    return (begins is None or begins <= when) and (ends is None or when <= ends)


class PlanningConstraintEvaluator:
    """Evaluate hard constraints during candidate generation.

    The return shape is kept compatible with existing planners:
    ``(decision, reason_code, message, failed_node)``.
    """

    @staticmethod
    def valid_edge(value: Any) -> bool:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and value > 0
        )

    def node_violation(
        self,
        scene: Any,
        view: Any,
        node: str,
        overlay: Any,
        destination: str,
        arrival_time_s: float = 0.0,
    ) -> Violation | None:
        region = view.payload["regions"][node]
        real_id = self.real_id(view, node)
        event_types = {
            event.event_type
            for event in overlay.events
            if _event_active(event, scene.data["current_time"], arrival_time_s)
            and (real_id in event.region_ids or node in event.region_ids)
        }
        if event_types.intersection({"NODE_CLOSED", "NODE_BLOCKED"}):
            return REJECT, "DYNAMIC_NODE_BLOCKED", f"Dynamic event blocks {node}.", node
        if "ELEVATOR_UNAVAILABLE" in event_types and "ELEVATOR" in region["type"].upper():
            return REJECT, "ELEVATOR_UNAVAILABLE", f"Elevator is unavailable at {node}.", node
        if (
            "EMERGENCY_CORRIDOR_ACTIVE" in event_types
            and view.payload["task"].get("urgency") != "EMERGENCY"
        ):
            return (
                REJECT,
                "EMERGENCY_CORRIDOR_ACTIVE",
                f"Active emergency corridor blocks {node}.",
                node,
            )

        status = region.get("navigation_status", "OPEN")
        if status in {"UNKNOWN", "CONFLICTED"}:
            return PAUSE, "ROUTE_STATUS_UNKNOWN", f"Navigation status is unreliable for {node}.", node
        if status == "BLOCKED":
            return REJECT, "ROUTE_REGION_BLOCKED", f"Region {node} is blocked.", node
        valid_until = region.get("navigation_status_valid_until")
        if "STATUS_EXPIRED" in event_types or (
            valid_until
            and _parse_datetime(scene.data["current_time"]) >= _parse_datetime(valid_until)
        ):
            return PAUSE, "ROUTE_STATUS_EXPIRED", f"Navigation status expired for {node}.", node

        permissions = set(view.payload["robot"]["access_permissions"])
        if not set(region.get("required_permissions", ())).issubset(permissions):
            return REJECT, "ROUTE_ACCESS_DENIED", f"Robot lacks permission for {node}.", node
        conflict = self.cargo_kinds(scene, view).intersection(
            region.get("prohibited_cargo_kinds", ())
        )
        if conflict:
            return (
                REJECT,
                "ROUTE_CARGO_RESTRICTED",
                f"Cargo is prohibited at {node}: {sorted(conflict)}.",
                node,
            )

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
            return (
                REJECT,
                "ROUTE_POLICY_DESTINATION_ONLY",
                f"{node} cannot be used for transit.",
                node,
            )
        if (
            policy == "EMERGENCY_ONLY_ACTIVE"
            and view.payload["task"].get("urgency") != "EMERGENCY"
        ):
            return (
                REJECT,
                "ROUTE_POLICY_EMERGENCY_ONLY",
                f"{node} is reserved for emergencies.",
                node,
            )
        return None

    @staticmethod
    def cargo_kinds(scene: Any, view: Any) -> set[str]:
        kinds: set[str] = set()
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
    def real_id(view: Any, reference: str) -> str:
        try:
            return view.resolve_reference(reference)
        except Exception:
            return reference

    @staticmethod
    def checked_rules() -> tuple[str, ...]:
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
