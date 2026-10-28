"""Immutable de-identified scene view shared by navigation planners."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from roboguard.medical.scene import MedicalSceneError


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return deepcopy(value)


@dataclass(frozen=True)
class PlannerSceneView:
    """Planner payload plus a local-only opaque-reference lookup."""

    payload: Mapping[str, Any]
    _reference_map: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        """Return only the de-identified payload that may be sent to SPINE."""

        return _thaw(self.payload)

    def resolve_reference(self, reference: str) -> str:
        """Resolve a planner reference locally; never send this map to a model."""

        try:
            return self._reference_map[reference]
        except KeyError as error:
            raise MedicalSceneError(f"unknown planner reference: {reference}") from error
