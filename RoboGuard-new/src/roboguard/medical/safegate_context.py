"""Build SafeGate trusted context from the active hospital scene."""

from __future__ import annotations

from typing import Any

from roboguard.medical.scene import MedicalScene, MedicalSceneError


def build_navigation_orchestration_context(
    scene: MedicalScene,
    *,
    staff_id: str | None = None,
) -> dict[str, Any]:
    """Create the trusted SafeGate facts for a guided-navigation request.

    The destination is deliberately left unresolved here. The navigation
    provider resolves it from its maintained location registry and binds that
    result immediately before SafeGate evaluates the request.
    """

    data = scene.data
    staff = data["staff"]
    if staff_id is None:
        if len(staff) != 1:
            raise MedicalSceneError(
                "navigation orchestration requires --trusted-staff-id when "
                "the scene contains multiple staff members"
            )
        staff_id = next(iter(staff))

    staff_member = staff.get(staff_id)
    if staff_member is None:
        raise MedicalSceneError(
            "trusted staff selector references an unknown staff member"
        )

    return {
        "schema_version": "0.2",
        "context_id": scene.snapshot_id,
        "issued_at": scene.captured_at,
        "provenance": [dict(item) for item in data["provenance"]],
        "reference_resolution": {
            "patient_aliases": {},
            "resource_aliases": {},
            "location_aliases": {},
        },
        "requester": {
            "id": staff_member["id"],
            "identity_status": staff_member["identity_status"],
            "verified_role": staff_member["verified_role"],
            "role_verification_status": staff_member[
                "role_verification_status"
            ],
            "permissions": list(staff_member["permissions"]),
            "relationship_to_patient": "NONE",
        },
        "patient": None,
        "authorization": {
            "patient_data_access": "NOT_APPLICABLE",
            "patient_data_scope_match_status": "NOT_APPLICABLE",
            "authorization_scope": list(staff_member["authorization_scope"]),
            "prescription_status": "NOT_APPLICABLE",
            "medical_order_status": "NOT_APPLICABLE",
            "sensitive_material_access": "NOT_APPLICABLE",
            "sensitive_material_transport": "NOT_APPLICABLE",
            "recipient_access": "NOT_APPLICABLE",
        },
        "environment": {
            "target_location_id": None,
            "target_location_resolution": "UNKNOWN",
            "target_area_type": "UNKNOWN",
            "target_area_access": "UNKNOWN",
        },
        "robot": {"capabilities": list(data["robot_state"]["capabilities"])},
        "medication": None,
        "delivery": None,
        "public_information": None,
    }
