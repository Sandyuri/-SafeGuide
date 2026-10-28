"""Public, planner-independent data types for route validation."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Iterable, Mapping


@dataclass(frozen=True)
class RouteViolation:
    """One deterministic explanation of why a proposed route is invalid."""

    rule_id: str
    type: str
    message: str
    node_id: str | None = None
    edge_id: str | None = None
    route_index: int | None = None
    details: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "rule_id": self.rule_id,
            "type": self.type,
            "message": self.message,
        }
        if self.node_id is not None:
            result["node_id"] = self.node_id
        if self.edge_id is not None:
            result["edge_id"] = self.edge_id
        if self.route_index is not None:
            result["route_index"] = self.route_index
        if self.details:
            result["details"] = dict(self.details)
        return result


@dataclass(frozen=True)
class RouteValidationReceipt:
    """Immutable proof that one concrete route passed independent validation."""

    receipt_id: str
    decision: str
    route: tuple[str, ...]
    map_id: str
    map_version: str
    context_map_version: str
    plan_version: int
    event_digest: str
    workflow_trace: tuple[tuple[str, str, str, str, bool], ...]
    checked_rules: tuple[str, ...]
    distance_m: float
    travel_time_s: float
    risk_score: float
    risk_components: tuple[tuple[str, float], ...]

    @classmethod
    def issue(
        cls,
        *,
        route: tuple[str, ...],
        map_id: str,
        map_version: str,
        context_map_version: object,
        plan_version: int,
        events: Iterable[Mapping[str, object]],
        workflow_trace: object,
        checked_rules: tuple[str, ...],
        recomputed: Mapping[str, object],
    ) -> "RouteValidationReceipt":
        components = tuple(
            sorted(
                (str(key), float(value))
                for key, value in dict(recomputed.get("risk_components", {})).items()
            )
        )
        trace = tuple(
            (
                str(item[0]), str(item[1]), str(item[2]), str(item[3]), bool(item[4])
            )
            for item in (workflow_trace or ())
        )
        event_digest = cls.digest_events(events)
        payload = {
            "decision": "VALID",
            "route": list(route),
            "map_id": str(map_id),
            "map_version": str(map_version),
            "context_map_version": str(context_map_version),
            "plan_version": int(plan_version),
            "event_digest": event_digest,
            "workflow_trace": [list(item) for item in trace],
            "checked_rules": list(checked_rules),
            "distance_m": float(recomputed["distance_m"]),
            "travel_time_s": float(recomputed["travel_time_s"]),
            "risk_score": float(recomputed["risk_score"]),
            "risk_components": dict(components),
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return cls(
            receipt_id="sha256:" + hashlib.sha256(canonical).hexdigest(),
            decision="VALID",
            route=tuple(route),
            map_id=str(map_id),
            map_version=str(map_version),
            context_map_version=str(context_map_version),
            plan_version=int(plan_version),
            event_digest=event_digest,
            workflow_trace=trace,
            checked_rules=tuple(checked_rules),
            distance_m=payload["distance_m"],
            travel_time_s=payload["travel_time_s"],
            risk_score=payload["risk_score"],
            risk_components=components,
        )

    @staticmethod
    def digest_events(events: Iterable[Mapping[str, object]]) -> str:
        canonical = json.dumps(
            list(events),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    def is_intact(self) -> bool:
        rebuilt = type(self).issue(
            route=self.route,
            map_id=self.map_id,
            map_version=self.map_version,
            context_map_version=self.context_map_version,
            plan_version=self.plan_version,
            events=(),
            workflow_trace=self.workflow_trace,
            checked_rules=self.checked_rules,
            recomputed={
                "distance_m": self.distance_m,
                "travel_time_s": self.travel_time_s,
                "risk_score": self.risk_score,
                "risk_components": dict(self.risk_components),
            },
        )
        # issue() hashes an empty event list above; preserve the already-bound
        # event digest when checking the canonical receipt payload.
        payload = rebuilt.to_dict()
        payload["event_digest"] = self.event_digest
        payload.pop("receipt_id")
        recomputed = payload.pop("recomputed")
        payload.update(recomputed)
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self.receipt_id == "sha256:" + hashlib.sha256(canonical).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "decision": self.decision,
            "route": list(self.route),
            "map_id": self.map_id,
            "map_version": self.map_version,
            "context_map_version": self.context_map_version,
            "plan_version": self.plan_version,
            "event_digest": self.event_digest,
            "workflow_trace": [list(item) for item in self.workflow_trace],
            "checked_rules": list(self.checked_rules),
            "recomputed": {
                "distance_m": self.distance_m,
                "travel_time_s": self.travel_time_s,
                "risk_score": self.risk_score,
                "risk_components": dict(self.risk_components),
            },
        }


@dataclass(frozen=True)
class RouteValidationResult:
    """Complete output of the independent route validator."""

    decision: str
    violations: tuple[RouteViolation, ...]
    recomputed: Mapping[str, object]
    workflow: Mapping[str, object]
    checked_rules: tuple[str, ...]
    map_version: str
    plan_version: int
    receipt: RouteValidationReceipt | None = None

    @property
    def valid(self) -> bool:
        return self.decision == "VALID"

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "violations": [item.to_dict() for item in self.violations],
            "recomputed": dict(self.recomputed),
            "workflow": dict(self.workflow),
            "checked_rules": list(self.checked_rules),
            "map_version": self.map_version,
            "plan_version": self.plan_version,
            "receipt": self.receipt.to_dict() if self.receipt else None,
        }
