"""Validation and immutable representation of a trusted medical scene."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ModuleNotFoundError:  # Keep deterministic offline benchmarks dependency-light.
    Draft202012Validator = None
    FormatChecker = None


DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "schemas"
    / "medical"
    / "medical_scene.schema.json"
)


class MedicalSceneError(ValueError):
    """Raised when medical scene data is structurally or semantically invalid."""


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return deepcopy(value)


def _validate_schema(data: Mapping[str, Any], schema_path: Path) -> None:
    if Draft202012Validator is None:
        # Detailed semantic and reference checks below still run.  Full JSON
        # Schema validation is restored automatically when the declared
        # jsonschema dependency is installed.
        return
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MedicalSceneError(f"cannot load medical scene schema: {error}") from error

    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(data),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        details = []
        for error in errors:
            path = ".".join(str(part) for part in error.absolute_path) or "<root>"
            details.append(f"{path}: {error.message}")
        raise MedicalSceneError("schema validation failed: " + "; ".join(details))


def _validate_ids(collection_name: str, collection: Mapping[str, Any]) -> None:
    for key, item in collection.items():
        if item["id"] != key:
            raise MedicalSceneError(
                f"{collection_name}.{key}.id must equal its collection key"
            )


def _validate_semantics(data: Mapping[str, Any]) -> None:
    patients = data["patients"]
    staff = data["staff"]
    medications = data["medications"]
    orders = data["medication_orders"]
    documents = data.get("documents", {})
    delivery_items = data["delivery_items"]
    regions = data["regions"]
    access_zones = data["access_zones"]

    for name, collection in (
        ("patients", patients),
        ("staff", staff),
        ("medications", medications),
        ("medication_orders", orders),
        ("documents", documents),
        ("delivery_items", delivery_items),
        ("regions", regions),
        ("access_zones", access_zones),
    ):
        _validate_ids(name, collection)

    for patient_id, patient in patients.items():
        if patient["region_id"] not in regions:
            raise MedicalSceneError(
                f"patient {patient_id} references unknown region {patient['region_id']}"
            )

    for staff_id, staff_member in staff.items():
        unknown_patients = sorted(
            set(staff_member["assigned_patient_ids"]) - set(patients)
        )
        if unknown_patients:
            raise MedicalSceneError(
                f"staff {staff_id} references unknown assigned patient(s): "
                f"{unknown_patients}"
            )

    for medication_id, medication in medications.items():
        if medication["storage_region_id"] not in regions:
            raise MedicalSceneError(
                f"medication {medication_id} references unknown storage region "
                f"{medication['storage_region_id']}"
            )

    for order_id, order in orders.items():
        if order["patient_id"] not in patients:
            raise MedicalSceneError(
                f"order {order_id} references unknown patient {order['patient_id']}"
            )
        if order["medication_id"] not in medications:
            raise MedicalSceneError(
                f"order {order_id} references unknown medication {order['medication_id']}"
            )
        window = order["delivery_window"]
        if _parse_datetime(window["start"]) >= _parse_datetime(window["end"]):
            raise MedicalSceneError(f"order {order_id} has an invalid delivery window")

    active_packages_by_order: dict[str, list[str]] = {}
    active_packages_by_document: dict[str, list[str]] = {}
    for item_id, item in delivery_items.items():
        kind = item["kind"]
        if kind == "MEDICATION_PACKAGE":
            order_id = item["medication_order_id"]
            medication_id = item["medication_id"]
            if order_id not in orders:
                raise MedicalSceneError(
                    f"delivery item {item_id} references unknown order {order_id}"
                )
            if medication_id not in medications:
                raise MedicalSceneError(
                    f"delivery item {item_id} references unknown medication "
                    f"{medication_id}"
                )
            if orders[order_id]["medication_id"] != medication_id:
                raise MedicalSceneError(
                    f"delivery item {item_id} medication does not match its order"
                )
            active_key = order_id
            active_index = active_packages_by_order
        elif kind == "DOCUMENT_PACKAGE":
            document_id = item["document_id"]
            if document_id not in documents:
                raise MedicalSceneError(
                    f"delivery item {item_id} references unknown document {document_id}"
                )
            if item["recipient_id"] not in staff:
                raise MedicalSceneError(
                    f"delivery item {item_id} references unknown recipient "
                    f"{item['recipient_id']}"
                )
            if item["destination_region_id"] not in regions:
                raise MedicalSceneError(
                    f"delivery item {item_id} references unknown destination "
                    f"{item['destination_region_id']}"
                )
            active_key = document_id
            active_index = active_packages_by_document
        else:
            raise MedicalSceneError(f"unsupported delivery item kind: {kind}")
        region_id = item["current_region_id"]
        holder_id = item["holder_robot_id"]
        if region_id is not None and region_id not in regions:
            raise MedicalSceneError(
                f"delivery item {item_id} references unknown region {region_id}"
            )
        if holder_id is not None and holder_id != data["robot_state"]["id"]:
            raise MedicalSceneError(
                f"delivery item {item_id} references unknown holder {holder_id}"
            )
        if item["status"] == "READY_FOR_PICKUP":
            if region_id is None or holder_id is not None:
                raise MedicalSceneError(
                    f"ready delivery item {item_id} must be at a region and unheld"
                )
            active_index.setdefault(active_key, []).append(item_id)
        elif item["status"] == "IN_TRANSIT":
            if region_id is not None or holder_id != data["robot_state"]["id"]:
                raise MedicalSceneError(
                    f"in-transit delivery item {item_id} must be held by the robot"
                )
            active_index.setdefault(active_key, []).append(item_id)

    for order_id, item_ids in active_packages_by_order.items():
        if len(item_ids) > 1:
            raise MedicalSceneError(
                f"order {order_id} has multiple active delivery items: {item_ids}"
            )
    for document_id, item_ids in active_packages_by_document.items():
        if len(item_ids) > 1:
            raise MedicalSceneError(
                f"document {document_id} has multiple active delivery items: {item_ids}"
            )

    for region_id, region in regions.items():
        if region["access_zone_id"] not in access_zones:
            raise MedicalSceneError(
                f"region {region_id} references unknown access zone "
                f"{region['access_zone_id']}"
            )
        unknown = sorted(set(region["connections"]) - set(regions))
        if unknown:
            raise MedicalSceneError(
                f"region {region_id} references unknown connection(s): {unknown}"
            )
        unknown_costs = sorted(
            set(region.get("connection_costs", {}))
            - set(region["connections"])
        )
        if unknown_costs:
            raise MedicalSceneError(
                f"region {region_id} has costs for non-connections: {unknown_costs}"
            )

    robot_region = data["robot_state"]["current_region_id"]
    if robot_region not in regions:
        raise MedicalSceneError(f"robot references unknown region {robot_region}")
    robot_roles = data["robot_state"].get("roles", [])
    if (
        not isinstance(robot_roles, list)
        or any(not isinstance(role, str) or not role.strip() for role in robot_roles)
        or len(robot_roles) != len(set(robot_roles))
    ):
        raise MedicalSceneError("robot_state.roles must contain unique non-empty strings")

    current_time = _parse_datetime(data["current_time"])
    snapshot = data["snapshot"]
    captured_at = _parse_datetime(snapshot["captured_at"])
    if captured_at > current_time:
        raise MedicalSceneError("snapshot.captured_at cannot be after current_time")
    source = data["source"]
    if _parse_datetime(source["retrieved_at"]) > current_time:
        raise MedicalSceneError("source.retrieved_at cannot be after current_time")
    if source.get("expires_at") and current_time >= _parse_datetime(source["expires_at"]):
        raise MedicalSceneError("medical scene source data has expired")
    for item in data["provenance"]:
        if _parse_datetime(item["observed_at"]) > captured_at:
            raise MedicalSceneError(
                "provenance.observed_at cannot be after snapshot.captured_at"
            )


@dataclass(frozen=True)
class MedicalScene:
    """A validated, deeply immutable snapshot from a trusted provider."""

    _data: Mapping[str, Any]

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        schema_path: str | Path = DEFAULT_SCHEMA_PATH,
    ) -> "MedicalScene":
        if not isinstance(data, Mapping):
            raise MedicalSceneError("medical scene must be a mapping")
        isolated = deepcopy(dict(data))
        _validate_schema(isolated, Path(schema_path))
        _validate_semantics(isolated)
        return cls(_freeze(isolated))

    @property
    def scene_id(self) -> str:
        return self._data["scene_id"]

    @property
    def source_type(self) -> str:
        return self._data["source"]["type"]

    @property
    def snapshot_id(self) -> str:
        return self._data["snapshot"]["id"]

    @property
    def snapshot_version(self) -> int:
        return self._data["snapshot"]["version"]

    @property
    def captured_at(self) -> str:
        return self._data["snapshot"]["captured_at"]

    @property
    def snapshot_hash(self) -> str:
        canonical = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def data(self) -> Mapping[str, Any]:
        return self._data

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self._data)
