"""Explainable transition costs for candidate-generating planners."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from typing import Any, Mapping

from roboguard.medical.scene import MedicalSceneError


def _load_cost_rules() -> tuple[dict[str, float], dict[str, float], float, float]:
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
        raise RuntimeError(f"invalid planning cost configuration {path}: {error}") from error
    values = (*edge.values(), *fixed.values(), congestion)
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise RuntimeError(f"planning cost configuration contains an invalid weight: {path}")
    if not math.isfinite(nominal_speed) or nominal_speed <= 0:
        raise RuntimeError(f"planning cost configuration contains an invalid speed: {path}")
    return edge, fixed, congestion, nominal_speed


(
    EDGE_RISK_PER_METER,
    FIXED_NODE_RISK,
    CONGESTION_MULTIPLIER,
    NOMINAL_SPEED_MPS,
) = _load_cost_rules()


@dataclass(frozen=True)
class TransitionCost:
    """The four objective increments caused by one map transition."""

    distance_m: float
    travel_time_s: float
    risk: float
    risk_components: tuple[tuple[str, float], ...]
    patient_burden: float


class PlanningCostModel:
    """Compute route-search costs without depending on another planner."""

    def transition_cost(
        self,
        *,
        view: Any,
        node: str,
        region: Mapping[str, Any],
        edge_distance: float,
        edge_risk_rates: Mapping[str, float] | None,
        overlay: Any,
        current_time: str | None = None,
        elapsed_before_s: float = 0.0,
    ) -> TransitionCost:
        distance = self._nonnegative_number(edge_distance, name="edge_distance")
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
        for component, raw_rate in rates.items():
            rate = self._nonnegative_number(
                raw_rate, name=f"risk rate {component} for {node}"
            )
            exposure = rate * distance
            if exposure:
                components[str(component).lower()] = exposure

        arrival_time_s = elapsed_before_s + distance / NOMINAL_SPEED_MPS
        congestion = self.congestion(
            overlay,
            node,
            self.real_id(view, node),
            current_time=current_time,
            arrival_time_s=arrival_time_s,
        )
        if congestion:
            components["congestion"] = congestion * CONGESTION_MULTIPLIER
        burden = self._nonnegative_number(
            region.get("patient_burden", 0.0),
            name=f"patient_burden for {node}",
        )
        return TransitionCost(
            distance_m=distance,
            travel_time_s=(distance / NOMINAL_SPEED_MPS) * (1.0 + congestion),
            risk=sum(components.values()),
            risk_components=tuple(sorted(components.items())),
            patient_burden=burden,
        )

    def edge_distance(
        self,
        view: Any,
        left: str,
        right: str,
        base_cost: float,
        overlay: Any,
    ) -> float:
        cost = self._positive_number(base_cost, name=f"edge {left}->{right}")
        real_left = self.real_id(view, left)
        real_right = self.real_id(view, right)
        for event in overlay.events:
            if (
                event.event_type == "EDGE_COST_CHANGED"
                and event.source_region_id in {left, real_left}
                and event.target_region_id in {right, real_right}
            ):
                cost = self._positive_number(
                    event.new_cost, name=f"dynamic edge {left}->{right}"
                )
        return cost

    @staticmethod
    def congestion(
        overlay: Any,
        node: str,
        real_id: str | None = None,
        *,
        current_time: str | None = None,
        arrival_time_s: float = 0.0,
    ) -> float:
        return sum(
            event.severity
            for event in overlay.events
            if event.active
            and event.event_type in {"CONGESTION", "CROWDING"}
            and (node in event.region_ids or real_id in event.region_ids)
            and PlanningCostModel._event_active(
                event, current_time=current_time, arrival_time_s=arrival_time_s
            )
        )

    @staticmethod
    def _event_active(
        event: Any,
        *,
        current_time: str | None,
        arrival_time_s: float,
    ) -> bool:
        if current_time is None:
            return True
        when = datetime.fromisoformat(current_time.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        when = when.astimezone(timezone.utc) + timedelta(seconds=arrival_time_s)
        begins = (
            datetime.fromisoformat(event.effective_at.replace("Z", "+00:00"))
            if event.effective_at
            else None
        )
        ends = (
            datetime.fromisoformat(event.expires_at.replace("Z", "+00:00"))
            if event.expires_at
            else None
        )
        if begins is not None and begins.tzinfo is None:
            begins = begins.replace(tzinfo=timezone.utc)
        if ends is not None and ends.tzinfo is None:
            ends = ends.replace(tzinfo=timezone.utc)
        return (begins is None or begins <= when) and (ends is None or when <= ends)

    @staticmethod
    def real_id(view: Any, reference: str) -> str:
        try:
            return view.resolve_reference(reference)
        except Exception:
            return reference

    @staticmethod
    def _nonnegative_number(value: Any, *, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MedicalSceneError(f"{name} must be numeric")
        result = float(value)
        if not math.isfinite(result) or result < 0:
            raise MedicalSceneError(f"{name} must be finite and nonnegative")
        return result

    @classmethod
    def _positive_number(cls, value: Any, *, name: str) -> float:
        result = cls._nonnegative_number(value, name=name)
        if result <= 0:
            raise MedicalSceneError(f"{name} must be positive")
        return result
