"""Deterministic, configuration-driven selection of one Pareto route."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from roboguard.medical.guard_types import ALLOW, PAUSE
from roboguard.medical.joint_planner import PlanResult, WorkflowTransition
from roboguard.medical.pareto_types import ParetoLabel, ParetoPlanResult
from roboguard.medical.scene import MedicalSceneError
from roboguard.medical.task_contracts import RouteContext, TaskSpec


DEFAULT_PROFILE_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "route_selection_profiles.json"
)
SUPPORTED_STRATEGIES = frozenset(
    {
        "risk_within_distance_ratio",
        "burden_within_distance_ratio",
        "time_within_risk_limit",
    }
)


@dataclass(frozen=True)
class CandidateComparison:
    route: tuple[str, ...]
    risk: float
    distance_m: float
    estimated_time_s: float
    patient_burden: float
    risk_delta: float
    distance_delta_m: float
    time_delta_s: float
    burden_delta: float
    eligible: bool
    selected: bool
    exclusion_reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "route": list(self.route),
            "risk": self.risk,
            "distance_m": self.distance_m,
            "estimated_time_s": self.estimated_time_s,
            "patient_burden": self.patient_burden,
            "risk_delta": self.risk_delta,
            "distance_delta_m": self.distance_delta_m,
            "time_delta_s": self.time_delta_s,
            "burden_delta": self.burden_delta,
            "eligible": self.eligible,
            "selected": self.selected,
            "exclusion_reason": self.exclusion_reason,
        }


@dataclass(frozen=True)
class RouteSelectionResult:
    decision: str
    reason_code: str
    message: str
    task_profile: str
    authorization_id: str
    plan_version: int
    selected_candidate: ParetoLabel | None = None
    selection_reason: str = ""
    candidate_count: int = 0
    comparisons: tuple[CandidateComparison, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "reason_code": self.reason_code,
            "message": self.message,
            "task_profile": self.task_profile,
            "authorization_id": self.authorization_id,
            "plan_version": self.plan_version,
            "selected_candidate": (
                self.selected_candidate.to_dict()
                if self.selected_candidate is not None
                else None
            ),
            "selection_reason": self.selection_reason,
            "candidate_count": self.candidate_count,
            "comparisons": [item.to_dict() for item in self.comparisons],
        }

    def to_plan_result(self, *, checked_rules: tuple[str, ...]) -> PlanResult:
        common = {
            "selection_profile": self.task_profile,
            "selection_reason": self.selection_reason,
            "candidate_count": self.candidate_count,
            "candidate_comparisons": tuple(
                item.to_dict() for item in self.comparisons
            ),
        }
        if self.selected_candidate is None:
            return PlanResult(
                self.decision,
                self.reason_code,
                self.message,
                "pareto",
                self.plan_version,
                replan_allowed=self.decision != ALLOW,
                checked_rules=checked_rules + ("TASK_PROFILE_SELECTION",),
                **common,
            )
        candidate = self.selected_candidate
        trace = tuple(WorkflowTransition(*item) for item in candidate.workflow_trace)
        return PlanResult(
            decision=ALLOW,
            reason_code="PARETO_PLAN_SELECTED",
            message=self.message,
            planner="pareto",
            plan_version=self.plan_version,
            route=candidate.route,
            distance_m=candidate.distance_m,
            estimated_time_s=candidate.estimated_time_s,
            risk_score=candidate.risk,
            patient_burden=candidate.patient_burden,
            risk_components=dict(candidate.risk_components),
            workflow_trace=trace,
            checked_rules=checked_rules + ("TASK_PROFILE_SELECTION",),
            validation_receipt=(
                candidate.validation.receipt
                if candidate.validation is not None
                else None
            ),
            **common,
        )


class RouteSelector:
    """Choose one executable candidate using a trusted task profile."""

    def __init__(self, profile_path: str | Path = DEFAULT_PROFILE_PATH) -> None:
        self.profile_path = Path(profile_path)
        self.schema_version, self.default_profile, self.profiles = self._load_profiles(
            self.profile_path
        )

    @staticmethod
    def _load_profiles(path: Path):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MedicalSceneError(f"cannot load route selection profiles: {path}") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != "1.0":
            raise MedicalSceneError("route selection profile schema_version must be 1.0")
        raw_profiles = payload.get("profiles")
        default = payload.get("default_profile")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            raise MedicalSceneError("route selection profiles must be a non-empty object")
        if default not in raw_profiles:
            raise MedicalSceneError("default route selection profile is missing")
        profiles: dict[str, Mapping[str, object]] = {}
        for name, raw in raw_profiles.items():
            if not isinstance(name, str) or not name or not isinstance(raw, dict):
                raise MedicalSceneError("route selection profile entries are invalid")
            strategy = raw.get("strategy")
            if strategy not in SUPPORTED_STRATEGIES:
                raise MedicalSceneError(f"unsupported route selection strategy: {strategy}")
            config = dict(raw)
            if strategy.endswith("distance_ratio"):
                RouteSelector._number_at_least(
                    config.get("max_distance_ratio"), "max_distance_ratio", 1.0
                )
            if strategy == "time_within_risk_limit":
                RouteSelector._number_at_least(config.get("max_risk"), "max_risk", 0.0)
            profiles[name] = MappingProxyType(config)
        return str(payload["schema_version"]), str(default), MappingProxyType(profiles)

    @staticmethod
    def _number_at_least(value, name: str, minimum: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MedicalSceneError(f"route selection {name} must be numeric")
        result = float(value)
        if not math.isfinite(result) or result < minimum:
            raise MedicalSceneError(f"route selection {name} must be at least {minimum}")
        return result

    def resolve_profile(self, task: TaskSpec) -> str:
        if task.urgency in {"URGENT", "EMERGENCY"}:
            requested = "EMERGENCY"
        elif set(task.accessibility_needs).intersection(
            {"WHEELCHAIR_ACCESSIBLE", "STEP_FREE", "ELEVATOR_REQUIRED"}
        ):
            requested = "WHEELCHAIR"
        elif set(task.accessibility_needs).intersection(
            {"LOW_SENSORY_LOAD", "ASSISTANCE_REQUIRED"}
        ):
            requested = "MOBILITY_ASSISTED"
        elif task.carrying_item_ids:
            requested = "CARGO"
        else:
            requested = self.default_profile
        if requested not in self.profiles:
            raise MedicalSceneError(f"route selection profile is missing: {requested}")
        return requested

    def select(
        self,
        pareto: ParetoPlanResult,
        *,
        task: TaskSpec,
        context: RouteContext,
        require_validation: bool = False,
    ) -> RouteSelectionResult:
        profile = self.resolve_profile(task)
        if pareto.decision != ALLOW:
            return RouteSelectionResult(
                pareto.decision,
                pareto.reason_code,
                pareto.message,
                profile,
                context.authorization_id,
                pareto.plan_version,
                candidate_count=len(pareto.candidates),
            )
        if require_validation and (
            not pareto.candidates
            or any(not candidate.validated for candidate in pareto.candidates)
        ):
            return RouteSelectionResult(
                PAUSE,
                "CANDIDATE_VALIDATION_REQUIRED",
                "RouteSelector accepts only independently validated Pareto candidates.",
                profile,
                context.authorization_id,
                pareto.plan_version,
                candidate_count=len(pareto.candidates),
            )
        complete = tuple(
            candidate
            for candidate in pareto.candidates
            if candidate.state.workflow_state == "COMPLETE"
        )
        if len(complete) != len(pareto.candidates) or not complete:
            return RouteSelectionResult(
                PAUSE,
                "WORKFLOW_NOT_COMPLETE",
                "No candidate can execute until confirmation, handoff, and completion rules finish.",
                profile,
                context.authorization_id,
                pareto.plan_version,
                candidate_count=len(pareto.candidates),
            )

        config = self.profiles[profile]
        strategy = str(config["strategy"])
        exclusions: dict[tuple[str, ...], str] = {}
        if strategy.endswith("distance_ratio"):
            shortest = min(item.distance_m for item in complete)
            limit = shortest * float(config["max_distance_ratio"])
            eligible = tuple(item for item in complete if item.distance_m <= limit + 1e-9)
            for item in complete:
                if item not in eligible:
                    exclusions[item.route] = f"distance exceeds {limit:.3f} m budget"
        elif strategy == "time_within_risk_limit":
            limit = float(config["max_risk"])
            eligible = tuple(item for item in complete if item.risk <= limit + 1e-9)
            for item in complete:
                if item not in eligible:
                    exclusions[item.route] = f"risk exceeds {limit:.3f} limit"
        else:
            raise MedicalSceneError(f"unsupported route selection strategy: {strategy}")
        if not eligible:
            return RouteSelectionResult(
                PAUSE,
                "NO_CANDIDATE_WITHIN_PROFILE_LIMITS",
                f"Pareto candidates exist, but none satisfy profile {profile}.",
                profile,
                context.authorization_id,
                pareto.plan_version,
                selection_reason=f"strategy={strategy}",
                candidate_count=len(complete),
            )

        if strategy == "risk_within_distance_ratio":
            key = lambda item: (
                item.risk, item.patient_burden, item.estimated_time_s,
                item.distance_m, item.route,
            )
        elif strategy == "burden_within_distance_ratio":
            key = lambda item: (
                item.patient_burden, item.risk, item.estimated_time_s,
                item.distance_m, item.route,
            )
        else:
            key = lambda item: (
                item.estimated_time_s, item.risk, item.patient_burden,
                item.distance_m, item.route,
            )
        selected = min(eligible, key=key)
        comparisons = tuple(
            CandidateComparison(
                route=item.route,
                risk=item.risk,
                distance_m=item.distance_m,
                estimated_time_s=item.estimated_time_s,
                patient_burden=item.patient_burden,
                risk_delta=item.risk - selected.risk,
                distance_delta_m=item.distance_m - selected.distance_m,
                time_delta_s=item.estimated_time_s - selected.estimated_time_s,
                burden_delta=item.patient_burden - selected.patient_burden,
                eligible=item in eligible,
                selected=item == selected,
                exclusion_reason=exclusions.get(item.route, ""),
            )
            for item in complete
        )
        reason = (
            f"profile={profile}; strategy={strategy}; selected deterministically from "
            f"{len(eligible)}/{len(complete)} eligible candidates"
        )
        return RouteSelectionResult(
            ALLOW,
            "PARETO_CANDIDATE_SELECTED",
            "A Pareto candidate was selected by the trusted task profile.",
            profile,
            context.authorization_id,
            pareto.plan_version,
            selected,
            reason,
            len(complete),
            comparisons,
        )
