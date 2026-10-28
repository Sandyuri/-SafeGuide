"""Translate validated SPINE plans into RoboGuard safety actions."""

from dataclasses import dataclass
from typing import Any, Iterable, Sequence


SPINE_ACTIONS = {
    "answer",
    "clarify",
    "extend_map",
    "explore_region",
    "goto",
    "inspect",
    "map_region",
    "replan",
    "request_human_review",
    "set_labels",
    "abort_task",
    "handoff",
    "pickup",
    "verify_recipient",
    "verify_resource",
    "return_resource",
    "administer_medication",
    "modify_order",
    "calculate_dose",
    "recommend_alternative",
}

REGION_NAVIGATION_ACTIONS = {"goto", "map_region", "explore_region"}
MEDICAL_EXECUTION_ACTIONS = {
    "verify_resource",
    "pickup",
    "verify_recipient",
    "handoff",
    "return_resource",
}
SINGLE_ARGUMENT_ACTIONS = MEDICAL_EXECUTION_ACTIONS - {"handoff"}
CONTROL_ACTIONS = {"request_human_review", "abort_task"}


def _clean_text(value: Any) -> str:
    return str(value).strip().strip("'\"").strip()


def _normalise_args(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (tuple, list)):
        return tuple(_clean_text(item) if isinstance(item, str) else item for item in value)
    return (_clean_text(value) if isinstance(value, str) else value,)


@dataclass(frozen=True)
class SpineAction:
    """Canonical representation of one SPINE behavior invocation."""

    name: str
    args: tuple[Any, ...]

    @property
    def target(self) -> str | None:
        if not self.args:
            return None
        return _clean_text(self.args[0])

    def display(self) -> str:
        rendered = ", ".join(str(arg) for arg in self.args)
        return f"{self.name}({rendered})"

    def guard_action(self) -> tuple[str, str] | None:
        """Return the physical/sensing effect that RoboGuard must check.

        SPINE's ``map_region`` and ``explore_region`` navigate before mapping,
        so both are checked as ``goto``. Administrative and conversational
        actions have no direct physical effect and return ``None``.
        """

        target = self.target
        if self.name in REGION_NAVIGATION_ACTIONS:
            if target is None:
                raise ValueError(f"{self.name} requires a region target")
            return ("goto", target)
        if self.name == "inspect":
            if target is None:
                raise ValueError("inspect requires an object target")
            return ("inspect", target)
        return None


def adapt_spine_plan(
    plan: Iterable[Sequence[Any]],
    *,
    allow_unknown: bool = False,
) -> list[SpineAction]:
    """Convert SPINE's ``[(name, argument), ...]`` output."""

    adapted: list[SpineAction] = []
    for index, step in enumerate(plan):
        if not isinstance(step, (tuple, list)) or len(step) != 2:
            raise ValueError(f"plan step {index} must be a (name, arguments) pair")
        raw_name, raw_args = step
        name = _clean_text(raw_name).lower()
        if name not in SPINE_ACTIONS and not allow_unknown:
            raise ValueError(f"unsupported SPINE action at step {index}: {name}")
        args = _normalise_args(raw_args)
        if name in SINGLE_ARGUMENT_ACTIONS and len(args) != 1:
            raise ValueError(f"{name} at step {index} requires exactly one object")
        if name == "handoff" and len(args) != 2:
            raise ValueError(
                f"handoff at step {index} requires a resource and a recipient"
            )
        if name in CONTROL_ACTIONS and (len(args) != 1 or not _clean_text(args[0])):
            raise ValueError(f"{name} at step {index} requires one non-empty reason")
        adapted.append(SpineAction(name=name, args=args))
    return adapted


