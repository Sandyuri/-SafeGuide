"""Canonical task and route context contracts for hospital navigation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from roboguard.medical.scene import MedicalScene, MedicalSceneError
from roboguard.safegate_adapter import AuthorizedTask


TASK_SPEC_VERSION = "1.0.0"
ROUTE_CONTEXT_VERSION = "1.0.0"
VALID_USER_ROLES = frozenset(
    {
        "GENERAL_PUBLIC",
        "PATIENT",
        "VISITOR",
        "STAFF",
        "NURSE",
        "DOCTOR",
        "TRANSPORTER",
    }
)
VALID_ACCESSIBILITY_NEEDS = frozenset(
    {
        "WHEELCHAIR_ACCESSIBLE",
        "STEP_FREE",
        "ELEVATOR_REQUIRED",
        "LOW_SENSORY_LOAD",
        "ASSISTANCE_REQUIRED",
    }
)
VALID_URGENCY = frozenset({"ROUTINE", "PRIORITY", "URGENT", "EMERGENCY"})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return deepcopy(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return deepcopy(value)


def _required_code(value: Any, *, label: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MedicalSceneError(f"authorized task {label} must be a non-empty string")
    code = value.strip().upper()
    if code not in allowed:
        raise MedicalSceneError(f"authorized task has unsupported {label}: {value!r}")
    return code


def _string_tuple(value: Any, *, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
        raise MedicalSceneError(f"authorized task {label} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise MedicalSceneError(
                f"authorized task {label} must contain non-empty strings"
            )
        result.append(item.strip())
    if len(result) != len(set(result)):
        raise MedicalSceneError(f"authorized task {label} contains duplicates")
    return tuple(result)


def _resolve_location(
    value: Any,
    *,
    label: str,
    aliases: Mapping[str, str],
    regions: Mapping[str, Any],
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MedicalSceneError(f"authorized task {label} must name one location")
    raw = value.strip()
    location_id = aliases.get(raw, raw)
    if location_id not in regions:
        raise MedicalSceneError(f"authorized task {label} is unknown: {raw}")
    return location_id


def _authorized_target_location(task: AuthorizedTask) -> str:
    target = task.target
    if not isinstance(target, dict) or target.get("kind") != "LOCATION":
        raise MedicalSceneError("authorized navigation target must be a LOCATION")
    location_id = target.get("id")
    if not isinstance(location_id, str) or not location_id:
        raise MedicalSceneError("authorized navigation target has no canonical id")
    return location_id


@dataclass(frozen=True)
class TaskSpec:
    """Normalized, trusted navigation task passed between RoboGuard stages."""

    task_type: str
    source_region_id: str
    ordered_destination_ids: tuple[str, ...]
    user_role: str = "GENERAL_PUBLIC"
    accessibility_needs: tuple[str, ...] = ()
    carrying_item_ids: tuple[str, ...] = ()
    urgency: str = "ROUTINE"
    confirmation_destination_ids: tuple[str, ...] = ()
    require_handoff: bool = False
    require_explicit_finish: bool = False
    schema_version: str = TASK_SPEC_VERSION

    @property
    def destination_region_id(self) -> str:
        return self.ordered_destination_ids[-1]

    @property
    def multi_stage(self) -> bool:
        return len(self.ordered_destination_ids) > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_type": self.task_type,
            "source_region_id": self.source_region_id,
            "ordered_destination_ids": list(self.ordered_destination_ids),
            "user_role": self.user_role,
            "accessibility_needs": list(self.accessibility_needs),
            "carrying_item_ids": list(self.carrying_item_ids),
            "urgency": self.urgency,
            "confirmation_destination_ids": list(self.confirmation_destination_ids),
            "require_handoff": self.require_handoff,
            "require_explicit_finish": self.require_explicit_finish,
        }


@dataclass(frozen=True)
class RouteContext:
    """Authorization and world snapshot bound to a navigation task."""

    authorization_id: str
    request_id: str
    authorization_scope: str
    scene_snapshot_id: str
    scene_snapshot_hash: str
    map_id: str
    map_version: int
    current_time: str
    dynamic_events: tuple[Mapping[str, Any], ...] = ()
    schema_version: str = ROUTE_CONTEXT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "authorization_id": self.authorization_id,
            "request_id": self.request_id,
            "authorization_scope": self.authorization_scope,
            "scene_snapshot_id": self.scene_snapshot_id,
            "scene_snapshot_hash": self.scene_snapshot_hash,
            "map_id": self.map_id,
            "map_version": self.map_version,
            "current_time": self.current_time,
            "dynamic_events": [_thaw(item) for item in self.dynamic_events],
        }


def build_task_spec(
    scene: MedicalScene,
    authorized_task: AuthorizedTask,
    *,
    destination_aliases: Mapping[str, str] | None = None,
) -> TaskSpec:
    """Build one validated task contract from the SafeGate authorization."""
    if authorized_task.action_type != "NAVIGATE_TO_LOCATION":
        raise MedicalSceneError(
            f"unsupported navigation task: {authorized_task.action_type}"
        )
    if authorized_task.context_id != scene.snapshot_id:
        raise MedicalSceneError(
            "SafeGate context_id does not match MedicalScene snapshot_id"
        )

    data = scene.data
    regions = data["regions"]
    aliases = destination_aliases or {}
    target_id = _authorized_target_location(authorized_task)
    if target_id not in regions:
        raise MedicalSceneError(f"unknown navigation destination: {target_id}")
    parameters = authorized_task.parameters

    parameter_destination = parameters.get("destination")
    if parameter_destination is not None:
        resolved = _resolve_location(
            parameter_destination,
            label="destination",
            aliases=aliases,
            regions=regions,
        )
        if resolved != target_id:
            raise MedicalSceneError(
                "authorized navigation target and destination parameter do not match"
            )

    source_id = data["robot_state"]["current_region_id"]
    parameter_source = parameters.get("source")
    if parameter_source is not None:
        resolved_source = _resolve_location(
            parameter_source,
            label="source",
            aliases=aliases,
            regions=regions,
        )
        if resolved_source != source_id:
            raise MedicalSceneError(
                "authorized navigation source and current robot region do not match"
            )

    raw_destinations = parameters.get("ordered_destinations")
    if raw_destinations is None:
        ordered_destinations = (target_id,)
    else:
        values = _string_tuple(raw_destinations, label="ordered_destinations")
        if not values:
            raise MedicalSceneError(
                "authorized task ordered_destinations must not be empty"
            )
        ordered_destinations = tuple(
            _resolve_location(
                value,
                label="ordered destination",
                aliases=aliases,
                regions=regions,
            )
            for value in values
        )
        if len(ordered_destinations) != len(set(ordered_destinations)):
            raise MedicalSceneError(
                "authorized task ordered_destinations contains duplicate locations"
            )
        if ordered_destinations[-1] != target_id:
            raise MedicalSceneError(
                "authorized navigation target and final ordered destination do not match"
            )

    raw_carrying = parameters.get("carrying")
    legacy_carrying = parameters.get("carrying_item_ids")
    if raw_carrying is not None and legacy_carrying is not None:
        if tuple(raw_carrying) != tuple(legacy_carrying):
            raise MedicalSceneError(
                "authorized task contains conflicting carrying declarations"
            )
    carrying = _string_tuple(
        raw_carrying if raw_carrying is not None else legacy_carrying,
        label="carrying",
    )
    unknown_items = sorted(set(carrying) - set(data["delivery_items"]))
    if unknown_items:
        raise MedicalSceneError(
            "authorized task carries unknown item(s): " + ", ".join(unknown_items)
        )

    accessibility = list(
        _string_tuple(
            parameters.get("accessibility_needs"),
            label="accessibility_needs",
        )
    )
    wheelchair = parameters.get("wheelchair_accessible")
    if wheelchair is not None and not isinstance(wheelchair, bool):
        raise MedicalSceneError(
            "authorized task wheelchair_accessible must be a boolean"
        )
    if wheelchair and "WHEELCHAIR_ACCESSIBLE" not in accessibility:
        accessibility.append("WHEELCHAIR_ACCESSIBLE")
    accessibility_codes = tuple(
        _required_code(
            item,
            label="accessibility need",
            allowed=VALID_ACCESSIBILITY_NEEDS,
        )
        for item in accessibility
    )

    confirmation_values = _string_tuple(
        parameters.get("confirmation_destinations"),
        label="confirmation_destinations",
    )
    confirmation_destinations = tuple(
        _resolve_location(
            value,
            label="confirmation destination",
            aliases=aliases,
            regions=regions,
        )
        for value in confirmation_values
    )
    unknown_confirmations = sorted(
        set(confirmation_destinations) - set(ordered_destinations)
    )
    if unknown_confirmations:
        raise MedicalSceneError(
            "confirmation destinations must be ordered destinations: "
            + ", ".join(unknown_confirmations)
        )
    require_handoff = parameters.get("require_handoff", False)
    require_finish = parameters.get("require_explicit_finish", False)
    if not isinstance(require_handoff, bool):
        raise MedicalSceneError("authorized task require_handoff must be a boolean")
    if not isinstance(require_finish, bool):
        raise MedicalSceneError(
            "authorized task require_explicit_finish must be a boolean"
        )

    return TaskSpec(
        task_type=authorized_task.action_type,
        source_region_id=source_id,
        ordered_destination_ids=ordered_destinations,
        user_role=_required_code(
            parameters.get("user_role", "GENERAL_PUBLIC"),
            label="user_role",
            allowed=VALID_USER_ROLES,
        ),
        accessibility_needs=accessibility_codes,
        carrying_item_ids=carrying,
        urgency=_required_code(
            parameters.get("urgency", "ROUTINE"),
            label="urgency",
            allowed=VALID_URGENCY,
        ),
        confirmation_destination_ids=confirmation_destinations,
        require_handoff=require_handoff,
        require_explicit_finish=require_finish,
    )


def build_route_context(
    scene: MedicalScene,
    authorized_task: AuthorizedTask,
    *,
    map_id: str = "hospital_1f",
    dynamic_events: Iterable[Mapping[str, Any]] = (),
) -> RouteContext:
    """Bind one task authorization to the exact scene and event snapshot."""
    if authorized_task.context_id != scene.snapshot_id:
        raise MedicalSceneError(
            "SafeGate context_id does not match MedicalScene snapshot_id"
        )
    if not isinstance(map_id, str) or not map_id.strip():
        raise MedicalSceneError("route context map_id must be non-empty")
    frozen_events: list[Mapping[str, Any]] = []
    for event in dynamic_events:
        if not isinstance(event, Mapping):
            raise MedicalSceneError("dynamic events must be objects")
        frozen_events.append(_freeze(event))
    return RouteContext(
        authorization_id=authorized_task.evaluation_id,
        request_id=authorized_task.request_id,
        authorization_scope=authorized_task.authorization_scope,
        scene_snapshot_id=scene.snapshot_id,
        scene_snapshot_hash=scene.snapshot_hash,
        map_id=map_id.strip(),
        map_version=scene.snapshot_version,
        current_time=scene.data["current_time"],
        dynamic_events=tuple(frozen_events),
    )
