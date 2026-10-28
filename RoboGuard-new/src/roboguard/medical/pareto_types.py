"""Deterministic data structures for multi-objective hospital route search.

This module contains no graph-search policy.  It defines immutable product
states, route labels, Pareto dominance, bounded frontier management, and the
auditable result returned by the future Pareto planner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
from types import MappingProxyType
from typing import Iterable, Mapping

from roboguard.medical.guard_types import ALLOW, PAUSE, REJECT
from roboguard.medical.route_validation_types import RouteValidationResult


DEFAULT_DOMINANCE_EPSILON = 1e-9
DEFAULT_MAX_LABELS_PER_STATE = 32
OBJECTIVE_NAMES = ("risk", "distance_m", "estimated_time_s", "patient_burden")


def _nonnegative_finite(value: float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


def _positive_int(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _string_tuple(values: Iterable[str], *, name: str) -> tuple[str, ...]:
    result = tuple(values)
    if not result or not all(isinstance(item, str) and item for item in result):
        raise ValueError(f"{name} must contain non-empty strings")
    return result


def _risk_components(
    values: Mapping[str, float] | Iterable[tuple[str, float]],
) -> tuple[tuple[str, float], ...]:
    items = values.items() if isinstance(values, Mapping) else values
    merged: dict[str, float] = {}
    for key, value in items:
        if not isinstance(key, str) or not key:
            raise ValueError("risk component names must be non-empty strings")
        amount = _nonnegative_finite(value, name=f"risk component {key}")
        merged[key] = merged.get(key, 0.0) + amount
    return tuple(sorted(merged.items()))


@dataclass(frozen=True, order=True)
class ProductState:
    """One node in the map/workflow/version product graph."""

    map_node: str
    workflow_state: str
    plan_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.map_node, str) or not self.map_node:
            raise ValueError("product state map_node must be a non-empty string")
        if not isinstance(self.workflow_state, str) or not self.workflow_state:
            raise ValueError("product state workflow_state must be a non-empty string")
        _positive_int(self.plan_version, name="product state plan_version")

    def to_dict(self) -> dict[str, object]:
        return {
            "map_node": self.map_node,
            "workflow_state": self.workflow_state,
            "plan_version": self.plan_version,
        }


@dataclass(frozen=True)
class ParetoLabel:
    """One immutable route label reaching a product-graph state."""

    state: ProductState
    risk: float
    distance_m: float
    estimated_time_s: float
    patient_burden: float
    route: tuple[str, ...]
    risk_components: tuple[tuple[str, float], ...] = ()
    workflow_trace: tuple[tuple[str, str, str, str, bool], ...] = ()
    planner_metrics: Mapping[str, object] = field(default_factory=dict)
    validation: RouteValidationResult | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, ProductState):
            raise ValueError("label state must be a ProductState")
        object.__setattr__(self, "risk", _nonnegative_finite(self.risk, name="risk"))
        object.__setattr__(
            self,
            "distance_m",
            _nonnegative_finite(self.distance_m, name="distance_m"),
        )
        object.__setattr__(
            self,
            "estimated_time_s",
            _nonnegative_finite(self.estimated_time_s, name="estimated_time_s"),
        )
        object.__setattr__(
            self,
            "patient_burden",
            _nonnegative_finite(self.patient_burden, name="patient_burden"),
        )
        route = _string_tuple(self.route, name="route")
        if route[-1] != self.state.map_node:
            raise ValueError("route must end at the product state's map node")
        object.__setattr__(self, "route", route)
        object.__setattr__(
            self, "risk_components", _risk_components(self.risk_components)
        )
        trace = tuple(self.workflow_trace)
        for item in trace:
            if not isinstance(item, tuple) or len(item) != 5 or not isinstance(item[4], bool):
                raise ValueError("workflow trace entries must be five-item tuples ending in bool")
        object.__setattr__(self, "workflow_trace", trace)
        if self.planner_metrics:
            object.__setattr__(
                self, "planner_metrics", MappingProxyType(dict(self.planner_metrics))
            )

    @property
    def objectives(self) -> tuple[float, float, float, float]:
        return (
            self.risk,
            self.distance_m,
            self.estimated_time_s,
            self.patient_burden,
        )

    @property
    def deterministic_key(self) -> tuple[object, ...]:
        return self.objectives + (self.route, self.state)

    @property
    def candidate_id(self) -> str:
        source = "|".join(
            (*self.route, self.state.workflow_state, str(self.state.plan_version))
        )
        return "pareto-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]

    @property
    def validated(self) -> bool:
        return self.validation is not None and self.validation.valid

    def reported_metrics(self) -> dict[str, object]:
        if self.planner_metrics:
            return dict(self.planner_metrics)
        return {
            "distance_m": self.distance_m,
            "travel_time_s": self.estimated_time_s,
            "risk_score": self.risk,
            "risk_components": dict(self.risk_components),
            "patient_burden": self.patient_burden,
        }

    def dominates(
        self,
        other: "ParetoLabel",
        *,
        epsilon: float = DEFAULT_DOMINANCE_EPSILON,
    ) -> bool:
        if self.state != other.state:
            raise ValueError("dominance is only defined for labels at the same state")
        epsilon = _nonnegative_finite(epsilon, name="dominance epsilon")
        no_worse = all(
            left <= right + epsilon
            for left, right in zip(self.objectives, other.objectives)
        )
        strictly_better = any(
            left < right - epsilon
            for left, right in zip(self.objectives, other.objectives)
        )
        return no_worse and strictly_better

    def equivalent_objectives(
        self,
        other: "ParetoLabel",
        *,
        epsilon: float = DEFAULT_DOMINANCE_EPSILON,
    ) -> bool:
        if self.state != other.state:
            return False
        epsilon = _nonnegative_finite(epsilon, name="dominance epsilon")
        return all(
            abs(left - right) <= epsilon
            for left, right in zip(self.objectives, other.objectives)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "planner": "pareto",
            "state": self.state.to_dict(),
            "route": list(self.route),
            "plan_version": self.state.plan_version,
            "planner_metrics": self.reported_metrics(),
            "validation": self.validation.to_dict() if self.validation else None,
            "selection_metrics": (
                {
                    "distance_m": self.distance_m,
                    "travel_time_s": self.estimated_time_s,
                    "risk_score": self.risk,
                    "risk_components": dict(self.risk_components),
                    "patient_burden": self.patient_burden,
                }
                if self.validated
                else None
            ),
            "workflow_trace": [list(item) for item in self.workflow_trace],
        }


@dataclass(frozen=True)
class LabelInsertResult:
    labels: tuple[ParetoLabel, ...]
    accepted: bool
    pruned_count: int
    reason: str


def _bounded_labels(
    labels: tuple[ParetoLabel, ...], max_labels: int
) -> tuple[ParetoLabel, ...]:
    if len(labels) <= max_labels:
        return labels
    selected: list[ParetoLabel] = []
    # Retain objective extremes before filling remaining capacity in a stable
    # lexicographic order. This prevents the bound from keeping only low-risk
    # labels while deleting the shortest or fastest representative.
    for objective_index in range(len(OBJECTIVE_NAMES)):
        best = min(
            labels,
            key=lambda item: (
                item.objectives[objective_index],
                item.deterministic_key,
            ),
        )
        if best not in selected:
            selected.append(best)
        if len(selected) == max_labels:
            break
    for label in labels:
        if label not in selected:
            selected.append(label)
        if len(selected) == max_labels:
            break
    return tuple(sorted(selected, key=lambda item: item.deterministic_key))


def insert_nondominated_label(
    existing: Iterable[ParetoLabel],
    candidate: ParetoLabel,
    *,
    max_labels: int = DEFAULT_MAX_LABELS_PER_STATE,
    epsilon: float = DEFAULT_DOMINANCE_EPSILON,
) -> LabelInsertResult:
    """Insert one label and return a deterministic bounded Pareto frontier."""

    max_labels = _positive_int(max_labels, name="max_labels")
    epsilon = _nonnegative_finite(epsilon, name="dominance epsilon")
    current = tuple(existing)
    if any(label.state != candidate.state for label in current):
        raise ValueError("all frontier labels must share the candidate state")
    ordered = tuple(sorted(current, key=lambda item: item.deterministic_key))
    if candidate in ordered:
        return LabelInsertResult(ordered, False, 0, "DUPLICATE")

    equivalent = next(
        (label for label in ordered if label.equivalent_objectives(candidate, epsilon=epsilon)),
        None,
    )
    removed_equivalent = 0
    if equivalent is not None:
        if equivalent.deterministic_key <= candidate.deterministic_key:
            return LabelInsertResult(ordered, False, 0, "EQUIVALENT")
        ordered = tuple(label for label in ordered if label is not equivalent)
        removed_equivalent = 1

    if any(label.dominates(candidate, epsilon=epsilon) for label in ordered):
        return LabelInsertResult(ordered, False, 0, "DOMINATED")

    survivors = tuple(
        label for label in ordered if not candidate.dominates(label, epsilon=epsilon)
    )
    removed_by_dominance = len(ordered) - len(survivors)
    combined = tuple(
        sorted(survivors + (candidate,), key=lambda item: item.deterministic_key)
    )
    bounded = _bounded_labels(combined, max_labels)
    removed_by_capacity = len(combined) - len(bounded)
    accepted = candidate in bounded
    reason = "ACCEPTED" if accepted else "CAPACITY_PRUNED"
    if accepted and (removed_by_dominance or removed_by_capacity):
        reason = "ACCEPTED_PRUNED"
    return LabelInsertResult(
        bounded,
        accepted,
        removed_equivalent + removed_by_dominance + removed_by_capacity,
        reason,
    )


@dataclass
class ParetoFrontier:
    """Mutable per-state frontier used by the future graph-search loop."""

    max_labels_per_state: int = DEFAULT_MAX_LABELS_PER_STATE
    epsilon: float = DEFAULT_DOMINANCE_EPSILON
    generated_labels: int = 0
    accepted_labels: int = 0
    pruned_labels: int = 0
    _labels: dict[ProductState, tuple[ParetoLabel, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.max_labels_per_state = _positive_int(
            self.max_labels_per_state, name="max_labels_per_state"
        )
        self.epsilon = _nonnegative_finite(
            self.epsilon, name="dominance epsilon"
        )

    def insert(self, candidate: ParetoLabel) -> LabelInsertResult:
        self.generated_labels += 1
        result = insert_nondominated_label(
            self._labels.get(candidate.state, ()),
            candidate,
            max_labels=self.max_labels_per_state,
            epsilon=self.epsilon,
        )
        self._labels[candidate.state] = result.labels
        if result.accepted:
            self.accepted_labels += 1
        elif result.pruned_count == 0:
            self.pruned_labels += 1
        self.pruned_labels += result.pruned_count
        return result

    def labels_for(self, state: ProductState) -> tuple[ParetoLabel, ...]:
        return self._labels.get(state, ())

    def states(self) -> tuple[ProductState, ...]:
        return tuple(sorted(self._labels))


@dataclass(frozen=True)
class ParetoPlanResult:
    """Auditable multi-candidate result before task-specific selection."""

    decision: str
    reason_code: str
    message: str
    plan_version: int
    candidates: tuple[ParetoLabel, ...] = ()
    selected_candidate: ParetoLabel | None = None
    planner: str = "pareto"
    expanded_states: int = 0
    generated_labels: int = 0
    pruned_labels: int = 0
    checked_rules: tuple[str, ...] = ()
    rejected_candidates: tuple[ParetoLabel, ...] = ()

    def __post_init__(self) -> None:
        if self.decision not in {ALLOW, PAUSE, REJECT}:
            raise ValueError(f"unsupported Pareto decision: {self.decision}")
        _positive_int(self.plan_version, name="plan_version")
        for name in ("expanded_states", "generated_labels", "pruned_labels"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        ordered = tuple(
            sorted(tuple(self.candidates), key=lambda item: item.deterministic_key)
        )
        object.__setattr__(self, "candidates", ordered)
        object.__setattr__(
            self,
            "rejected_candidates",
            tuple(
                sorted(
                    tuple(self.rejected_candidates),
                    key=lambda item: item.deterministic_key,
                )
            ),
        )
        if self.selected_candidate is not None and self.selected_candidate not in ordered:
            raise ValueError("selected_candidate must be present in candidates")
        if self.decision == ALLOW and not ordered:
            raise ValueError("ALLOW Pareto result requires at least one candidate")

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW

    def to_dict(self) -> dict[str, object]:
        selected_index = None
        if self.selected_candidate is not None:
            selected_index = self.candidates.index(self.selected_candidate)
        return {
            "decision": self.decision,
            "reason_code": self.reason_code,
            "message": self.message,
            "planner": self.planner,
            "plan_version": self.plan_version,
            "candidates": [item.to_dict() for item in self.candidates],
            "rejected_candidates": [
                item.to_dict() for item in self.rejected_candidates
            ],
            "selected_candidate_index": selected_index,
            "expanded_states": self.expanded_states,
            "generated_labels": self.generated_labels,
            "pruned_labels": self.pruned_labels,
            "checked_rules": list(self.checked_rules),
        }
