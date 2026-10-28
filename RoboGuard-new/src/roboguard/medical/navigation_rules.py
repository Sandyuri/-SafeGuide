"""Configuration and immutable mandatory rules for navigation review."""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[3] / "config" / "medical_navigation_rules.json"
CONTROL_STOPS = frozenset({"request_human_review", "abort_task", "clarify", "replan"})
PHYSICAL_ACTIONS = frozenset({"goto", "inspect", "map_region", "explore_region"})
FORBIDDEN_TRANSIT_POLICIES = frozenset({"FORBID", "EMERGENCY_ONLY_ACTIVE"})


def load_navigation_rules(path: str | Path = DEFAULT_RULES_PATH) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("medical navigation rules must be a JSON object")
    natural = value.get("natural_language_rules")
    fixed = value.get("fixed_ltl", [])
    if not isinstance(natural, list) or not all(isinstance(x, str) and x.strip() for x in natural):
        raise ValueError("natural_language_rules must contain non-empty strings")
    if not isinstance(fixed, list) or not all(isinstance(x, str) and x.strip() for x in fixed):
        raise ValueError("fixed_ltl must contain non-empty strings")
    return value
