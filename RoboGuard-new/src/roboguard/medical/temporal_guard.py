"""Mandatory navigation sequencing and supplemental RoboGuard/LTL review."""

from __future__ import annotations

import re
from pathlib import Path

from roboguard.medical.guard_types import ALLOW, PAUSE, REJECT, CheckedRoute, StepResult
from roboguard.medical.navigation_rules import (
    CONTROL_STOPS,
    FORBIDDEN_TRANSIT_POLICIES,
    PHYSICAL_ACTIONS,
    load_navigation_rules,
)
from roboguard.medical.navigation_temporal_graph import build_navigation_temporal_graph
from roboguard.scene import active_scene_json
from roboguard.spine_adapter import SpineAction
from roboguard.synthesis import ALLOWED_ACTIONS, normalize_atom


class MedicalTemporalGuard:
    """Review one approved static route as a complete action trace."""

    def __init__(
        self,
        *,
        mode: str = "hybrid",
        rules_path: str | Path | None = None,
        model: str | None = None,
        guard_factory=None,
        synthesizer_factory=None,
    ) -> None:
        if mode not in {"hybrid", "fixed", "off"}:
            raise ValueError("temporal mode must be hybrid, fixed, or off")
        self.mode = mode
        self.rules = load_navigation_rules(rules_path) if rules_path else load_navigation_rules()
        self.model = model
        self.guard_factory = guard_factory
        self.synthesizer_factory = synthesizer_factory

    def review(self, *, planner_view, actions: list[SpineAction], checked_route: CheckedRoute) -> StepResult:
        mandatory = self._check_mandatory(planner_view, actions, checked_route)
        if mandatory.decision != ALLOW or self.mode == "off":
            return mandatory
        trace = self._project_route(checked_route)
        fixed = tuple(self.rules.get("fixed_ltl", ()))
        if fixed:
            result = self._check_formulas(fixed, trace)
            if result.decision != ALLOW:
                return result
        if self.mode == "fixed":
            return StepResult.allow(
                matched_rules=("MANDATORY_NAVIGATION_RULES",),
                checked_ltl=fixed,
                temporal_trace=tuple((f"{a}({b})", True) for a, b in trace),
            )
        try:
            guard = self._make_guard()
            graph = build_navigation_temporal_graph(planner_view)
            guard.update_context(active_scene_json(graph))
            specs = guard.get_safety_specs()
            matched, formulas = self._validate_generated(specs, graph)
            safe, results = guard.validate_plan(trace)
        except Exception as error:
            return StepResult(
                PAUSE,
                "TEMPORAL_RULE_GENERATION_FAILED",
                f"Medical navigation LTL generation or validation failed: {error}",
                required_evidence=("VALID_MEDICAL_NAVIGATION_LTL",),
            )
        rendered_trace = tuple((str(step), bool(ok)) for step, ok in results)
        if not safe:
            failed_index = next((i for i, (_, ok) in enumerate(results) if not ok), None)
            failed_node = trace[failed_index][1] if failed_index is not None and failed_index < len(trace) else None
            return StepResult(
                REJECT,
                "MEDICAL_TEMPORAL_RULE_VIOLATION",
                f"The approved route violates medical temporal rules at {failed_node or 'an unknown step'}.",
                violated_rules=tuple(matched),
                replan_allowed=True,
                matched_rules=tuple(matched),
                checked_ltl=tuple(fixed) + tuple(formulas),
                failed_route_node=failed_node,
                temporal_trace=rendered_trace,
            )
        return StepResult.allow(
            matched_rules=("MANDATORY_NAVIGATION_RULES", *matched),
            checked_ltl=tuple(fixed) + tuple(formulas),
            temporal_trace=rendered_trace,
        )

    def _make_guard(self):
        if self.guard_factory is not None:
            return self.guard_factory(rules=self.rules["natural_language_rules"], model=self.model)
        from roboguard.roboguard import RoboGuard
        return RoboGuard(rules=self.rules["natural_language_rules"], model=self.model)

    def _check_formulas(self, formulas, trace):
        try:
            factory = self.synthesizer_factory
            if factory is None:
                from roboguard.synthesis import ControlSynthesis
                factory = ControlSynthesis
            safe, results = factory(formulas).validate_action_sequence(trace)
        except Exception as error:
            return StepResult(PAUSE, "FIXED_LTL_VALIDATION_FAILED", str(error), required_evidence=("VALID_FIXED_LTL",))
        if not safe:
            failed_index = next((i for i, (_, ok) in enumerate(results) if not ok), None)
            failed_node = trace[failed_index][1] if failed_index is not None else None
            return StepResult(REJECT, "FIXED_LTL_VIOLATION", "The route violates a fixed medical LTL rule.",
                              violated_rules=("FIXED_MEDICAL_NAVIGATION_LTL",), replan_allowed=True,
                              checked_ltl=tuple(formulas), failed_route_node=failed_node,
                              temporal_trace=tuple((str(x), bool(y)) for x, y in results))
        return StepResult.allow(checked_ltl=tuple(formulas), temporal_trace=tuple((str(x), bool(y)) for x, y in results))

    @staticmethod
    def _check_mandatory(view, actions, route):
        stopped = False
        for action in actions:
            if stopped and action.name in PHYSICAL_ACTIONS:
                return StepResult(REJECT, "PHYSICAL_ACTION_AFTER_CONTROL_STOP",
                                  "No physical action may follow pause, review, replan, or abort.",
                                  violated_rules=("CONTROL_STOP_TERMINATES_OLD_PLAN",), replan_allowed=True)
            if action.name in CONTROL_STOPS:
                stopped = True
        regions = view.payload["regions"]
        destination = view.payload["task"]["destination_region_ref"]
        if not route.regions or route.destination != destination or route.regions[-1] != destination:
            return StepResult(REJECT, "TEMPORAL_ROUTE_BINDING_MISMATCH",
                              "The checked route does not end at the authorized destination.")
        for left, right in zip(route.regions, route.regions[1:]):
            if right not in regions[left]["connections"]:
                return StepResult(REJECT, "TEMPORAL_ROUTE_NOT_CONTIGUOUS", f"Route edge {left} -> {right} is not connected.")
        for node in route.regions[1:]:
            policy = regions[node].get("route_policy", "ALLOW_TRANSIT")
            if policy in FORBIDDEN_TRANSIT_POLICIES:
                return StepResult(REJECT, "MANDATORY_ROUTE_POLICY_VIOLATION", f"Route policy forbids entering {node}.",
                                  violated_rules=(policy,), replan_allowed=True, failed_route_node=node)
            if policy == "DESTINATION_ONLY" and node != destination:
                return StepResult(REJECT, "DESTINATION_ONLY_TRANSIT", f"Route may not use {node} as a shortcut.",
                                  violated_rules=("DESTINATION_ONLY",), replan_allowed=True, failed_route_node=node)
        return StepResult.allow(matched_rules=("MANDATORY_NAVIGATION_RULES",))

    @staticmethod
    def _project_route(route):
        return [("goto", node) for node in route.regions[1:]]

    @staticmethod
    def _validate_generated(specifications, graph):
        if not isinstance(specifications, dict) or not specifications:
            raise ValueError("the model returned no rule groups")
        known = {item["name"] for item in graph["regions"]}
        matched, formulas, atoms = [], [], {}
        pattern = re.compile(r"\b([a-z_][a-z0-9_]*)\(([^()]*)\)")
        for rule, items in specifications.items():
            if not isinstance(items, list):
                raise ValueError(f"constraints for {rule!r} must be a list")
            for item in items:
                formula = item.get("constraint") if isinstance(item, dict) else None
                if not isinstance(formula, str) or not formula.strip():
                    raise ValueError("a generated constraint is empty")
                if formula == "NONE":
                    continue
                for function, raw_argument in pattern.findall(formula):
                    if function not in ALLOWED_ACTIONS:
                        raise ValueError(f"unknown action in generated LTL: {function}")
                    argument = raw_argument.strip().strip("'\"")
                    if function == "goto" and argument not in known:
                        raise ValueError(f"unknown region in generated LTL: {argument}")
                    atom = normalize_atom(function, argument)
                    original = (function, argument)
                    if atom in atoms and atoms[atom] != original:
                        raise ValueError(f"atomic proposition collision: {atom}")
                    atoms[atom] = original
                matched.append(str(rule))
                formulas.append(formula)
        if not formulas:
            raise ValueError("the model generated no enforceable medical constraint")
        return matched, formulas
