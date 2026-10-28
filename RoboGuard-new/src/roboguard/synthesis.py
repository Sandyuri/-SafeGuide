"""Lazy Spot-backed validation of finite robot action traces."""

from __future__ import annotations

import re
from typing import Iterable

ALLOWED_ACTIONS = frozenset({"goto", "replan", "request_human_review", "abort_task"})


def normalize_atom(function: str, argument: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", argument).strip("_")
    if not normalized:
        raise ValueError("an action argument normalizes to an empty atom")
    return f"{function}_{normalized}"


class ControlSynthesis:
    def __init__(self, formulas: Iterable[str]):
        try:
            import spot
        except ImportError as error:
            raise RuntimeError("Spot is not installed; install it from conda-forge") from error
        self.spot = spot
        self.formulas = tuple(formula for formula in formulas if formula and formula != "NONE")
        if not self.formulas:
            raise ValueError("no enforceable LTL constraints")
        cleaned = " & ".join(self.clean_formula(item) for item in self.formulas)
        try:
            self.automaton = spot.translate(cleaned, "Buchi", "complete", "state-based")
        except Exception as error:
            raise ValueError(f"Spot rejected generated LTL: {error}") from error
        self.bdict = self.automaton.get_dict()

    @staticmethod
    def clean_formula(formula: str) -> str:
        pattern = r"\b(" + "|".join(sorted(ALLOWED_ACTIONS)) + r")\(([^()]*)\)"
        return re.sub(
            pattern,
            lambda match: normalize_atom(match.group(1), match.group(2).strip(" '\"")),
            formula,
        )

    def validate_action_sequence(self, plan: list[tuple[str, str]]):
        current = self.automaton.get_init_state_number()
        rendered_steps: list[str] = []
        accepting_history: list[bool] = []
        propositions = set(self.automaton.ap())
        for function, argument in plan:
            if function not in ALLOWED_ACTIONS:
                continue
            rendered = f"{function}({argument})"
            atom = self.spot.formula(normalize_atom(function, argument))
            values = [atom] if atom in propositions else []
            values.extend(self.spot.formula.Not(item) for item in propositions if item != atom)
            valuation = self.spot.formula(" & ".join(str(item) for item in values) or "1")
            next_state = None
            for edge in self.automaton.out(current):
                condition = self.spot.formula(self.spot.bdd_format_formula(self.bdict, edge.cond))
                if self.spot.contains(condition, valuation):
                    next_state = edge.dst
                    break
            if next_state is None:
                results = [(step, True) for step in rendered_steps]
                results.append((rendered, False))
                return False, results
            current = next_state
            rendered_steps.append(rendered)
            accepting_history.append(bool(self.automaton.state_is_accepting(current)))
        if not rendered_steps:
            return True, []
        safe = accepting_history[-1]
        if safe:
            return True, [(step, True) for step in rendered_steps]
        initial_accepting = bool(
            self.automaton.state_is_accepting(self.automaton.get_init_state_number())
        )
        previous = initial_accepting
        failed_index = len(rendered_steps) - 1
        for index, accepting in enumerate(accepting_history):
            if previous and not accepting:
                failed_index = index
                break
            previous = accepting
        return False, [
            (step, index < failed_index)
            for index, step in enumerate(rendered_steps)
        ]
