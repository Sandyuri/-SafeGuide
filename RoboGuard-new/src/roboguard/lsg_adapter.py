"""Fail-closed bridge from the Learned SafeGate HTTP contract to RoboGuard."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from jsonschema import Draft202012Validator, FormatChecker

from roboguard.safegate_adapter import AuthorizedTask


class LSGBlocked(RuntimeError):
    """Raised when LSG did not produce one valid navigation authorization."""

    def __init__(self, message: str, *, decision: str | None = None) -> None:
        super().__init__(message)
        self.decision = decision


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_digest(path: Path, *, label: str) -> str:
    try:
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError as error:
        raise LSGBlocked(f"cannot read {label}: {error}") from error


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LSGBlocked(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise LSGBlocked(f"{label} must be a JSON object")
    return value


def _validate(value: Any, schema_path: Path, *, label: str) -> None:
    schema = _read_object(schema_path, label=f"{label} schema")
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(value),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise LSGBlocked(
            f"{label} does not satisfy LSG schema at {location}: {error.message}"
        )


def load_lsg_default_context(lsg_root: str | Path) -> dict[str, Any]:
    """Load the exact trusted context configured by the local LSG package."""

    root = Path(lsg_root).resolve()
    runtime = _read_object(root / "configs" / "runtime.json", label="LSG runtime config")
    relative = runtime.get("default_context")
    if not isinstance(relative, str) or not relative:
        raise LSGBlocked("LSG runtime config has no default_context")
    context_path = (root / relative).resolve()
    if root not in context_path.parents:
        raise LSGBlocked("LSG default_context escapes the LSG package root")
    return _read_object(context_path, label="LSG trusted context")


def load_lsg_expected_provenance(lsg_root: str | Path) -> dict[str, str]:
    """Load the independently trusted model identity from the local package."""

    root = Path(lsg_root).resolve()
    runtime = _read_object(root / "configs" / "runtime.json", label="LSG runtime config")
    registry = _read_object(
        root / "configs" / "model_registry.json", label="LSG model registry"
    )
    result: dict[str, str] = {}
    for role in ("splitter", "gate"):
        backend = runtime.get(f"{role}_backend")
        entry = registry.get(backend) if isinstance(backend, str) else None
        if (
            not isinstance(entry, dict)
            or entry.get("role") != role
            or entry.get("enabled") is not True
        ):
            raise LSGBlocked(f"LSG {role} backend is disabled or invalid")
        expected_hash = entry.get("expected_hash")
        artifact_path = entry.get("artifact_path")
        if (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or not isinstance(artifact_path, str)
            or not artifact_path
        ):
            raise LSGBlocked(f"LSG {role} registry entry is incomplete")
        artifact = (root / artifact_path).resolve()
        if root not in artifact.parents:
            raise LSGBlocked(f"LSG {role} artifact escapes the package root")
        result[f"{role}_checkpoint_sha256"] = expected_hash
        result[f"{role}_manifest_sha256"] = _file_digest(
            artifact / "manifest.json", label=f"LSG {role} manifest"
        )
    return result


def request_lsg_json(
    base_url: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 130.0,
) -> dict[str, Any]:
    """Call the local LSG service without adding a runtime dependency."""

    if timeout <= 0:
        raise ValueError("LSG timeout must be positive")
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    body = None
    headers = {"Accept": "application/json"}
    method = "GET"
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        method = "POST"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
    except HTTPError as error:
        reason = error.read(4096).decode("utf-8", errors="replace")
        raise LSGBlocked(f"LSG HTTP {error.code}: {reason}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise LSGBlocked(f"cannot reach LSG service at {url}: {error}") from error
    if len(raw) > 2 * 1024 * 1024:
        raise LSGBlocked("LSG response is too large")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LSGBlocked("LSG returned invalid JSON") from error
    if not isinstance(value, dict):
        raise LSGBlocked("LSG response must be a JSON object")
    if "error" in value:
        raise LSGBlocked(
            f"LSG error {value.get('error')}: {value.get('reason', 'unknown')}"
        )
    return value


def authorize_lsg_response_for_roboguard(
    *,
    response: dict[str, Any],
    trusted_context: dict[str, Any],
    expected_provenance: dict[str, Any],
    expected_destination_id: str,
    scene_snapshot_id: str,
    lsg_root: str | Path,
    expected_request_id: str | None = None,
    expected_session_id: str | None = None,
    expected_raw_input: str | None = None,
) -> AuthorizedTask:
    """Verify one LSG result and bind it to the current RoboGuard scene.

    LSG's context identifies its language allow-list, while RoboGuard's context
    identifies a live scene snapshot.  This bridge validates the former in full
    before issuing an internal task bound to the latter.
    """

    root = Path(lsg_root).resolve()
    native = response.get("native_result")
    if not isinstance(native, dict):
        raise LSGBlocked("LSG response has no native_result")
    _validate(
        native,
        root / "schemas" / "lsg-orchestration-result.schema.json",
        label="LSG orchestration result",
    )

    decision = native.get("aggregate_decision")
    if decision != "AUTHORIZE":
        plan = native.get("dispatch_plan")
        followups = plan.get("followups") if isinstance(plan, dict) else []
        followups = followups if isinstance(followups, list) else []
        first = followups[0] if followups and isinstance(followups[0], dict) else {}
        message = first.get("message")
        raise LSGBlocked(
            f"LSG {decision}: {native.get('fail_closed_reason') or message or 'blocked'}",
            decision=decision if isinstance(decision, str) else None,
        )
    if native.get("fail_closed_reason") is not None:
        raise LSGBlocked("LSG AUTHORIZE result contains fail_closed_reason")
    if native.get("aggregate_rule") != "REJECT > DEFER > AUTHORIZE":
        raise LSGBlocked("unsupported LSG aggregate rule")
    if native.get("context_snapshot_hash") != _digest(trusted_context):
        raise LSGBlocked("LSG result does not match the trusted context")
    required_provenance = {
        "splitter_checkpoint_sha256",
        "gate_checkpoint_sha256",
        "splitter_manifest_sha256",
        "gate_manifest_sha256",
    }
    if set(expected_provenance) != required_provenance or native.get("provenance") != expected_provenance:
        raise LSGBlocked("LSG service provenance changed during the request")
    if not expected_provenance or not all(
        isinstance(value, str) and len(value) == 64
        for value in expected_provenance.values()
    ):
        raise LSGBlocked("LSG service returned invalid model provenance")
    if expected_request_id is not None and native.get("request_id") != expected_request_id:
        raise LSGBlocked("LSG response request identifier mismatch")
    if expected_session_id is not None and native.get("session_id") != expected_session_id:
        raise LSGBlocked("LSG response session identifier mismatch")
    if expected_raw_input is not None and native.get("raw_input") != expected_raw_input:
        raise LSGBlocked("LSG response raw input mismatch")

    authorizations = native.get("authorizations")
    candidates_batch = native.get("runtime_candidates")
    intents = native.get("parsed_intents")
    if not all(isinstance(value, list) and len(value) == 1 for value in (
        authorizations,
        candidates_batch,
        intents,
    )):
        raise LSGBlocked("RoboGuard navigation requires exactly one LSG intent")
    authorization = authorizations[0]
    candidates = candidates_batch[0]
    _validate(
        authorization,
        root / "schemas" / "lsg-authorization.schema.json",
        label="LSG authorization",
    )
    if (
        authorization.get("decision") != "AUTHORIZE"
        or authorization.get("task_type") != "ROBOT_TASK"
        or authorization.get("status") != "AUTHORIZED_FOR_PLANNING"
        or authorization.get("authorization_scope") != "ALLOW_TO_ENTER_PLANNING"
        or authorization.get("obligations")
        != ["REQUIRE_ROBOGUARD_EXECUTION_CHECK"]
        or authorization.get("fail_closed_reason") is not None
    ):
        raise LSGBlocked("LSG item is not authorized for RoboGuard planning")
    if authorization.get("provenance") != expected_provenance:
        raise LSGBlocked("LSG authorization provenance mismatch")
    if (
        authorization.get("context_snapshot_id") != trusted_context.get("context_id")
        or authorization.get("context_snapshot_hash") != _digest(trusted_context)
        or authorization.get("runtime_candidate_hash") != _digest(candidates)
    ):
        raise LSGBlocked("LSG authorization context or candidate hash mismatch")

    request = authorization.get("evaluated_request")
    if (
        not isinstance(request, dict)
        or request != authorization.get("normalized_request")
        or request.get("request_id") != authorization.get("request_id")
    ):
        raise LSGBlocked("LSG evaluated request mismatch")
    raw_text = request.get("raw_text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise LSGBlocked("LSG evaluated request has no non-empty raw text")
    if raw_text != intents[0]:
        raise LSGBlocked("LSG evaluated request differs from its parsed intent")
    if expected_raw_input is not None and raw_text != expected_raw_input:
        raise LSGBlocked("LSG evaluated request differs from the submitted input")
    action = request.get("action")
    if not isinstance(action, dict) or action.get("type") != "NAVIGATE_TO_LOCATION":
        raise LSGBlocked("LSG did not authorize a navigation action")
    target = action.get("target")
    resource = action.get("resource")
    if (
        not isinstance(target, dict)
        or target.get("kind") != "LOCATION"
        or target.get("id") != expected_destination_id
        or not isinstance(resource, dict)
        or resource.get("kind") != "LOCATION"
    ):
        raise LSGBlocked("LSG target does not match the resolved RoboGuard destination")

    aliases = (trusted_context.get("reference_resolution") or {}).get(
        "location_aliases"
    ) or {}
    trusted = (candidates.get("trusted_locations") or {}).get("candidates") or []
    if (
        len(trusted) != 1
        or set(trusted[0]) != {"alias", "location_id"}
        or aliases.get(trusted[0]["alias"]) != trusted[0]["location_id"]
        or trusted[0]["location_id"] != target["id"]
        or resource.get("id") != trusted[0]["alias"]
    ):
        raise LSGBlocked("LSG destination is not a unique allow-listed candidate")

    plan = native.get("dispatch_plan")
    items = plan.get("items") if isinstance(plan, dict) else None
    if (
        not isinstance(items, list)
        or len(items) != 1
        or not isinstance(items[0], dict)
        or plan.get("dry_run_robot") is not True
        or items[0].get("route") != "SPINE_TO_RoboGuard_DRY_RUN"
        or items[0].get("intent_index") != 1
    ):
        raise LSGBlocked("LSG did not emit exactly one dry-run RoboGuard dispatch")
    item = items[0]
    if (
        item.get("request_id") != authorization.get("request_id")
        or item.get("evaluation_id") != authorization.get("evaluation_id")
        or item.get("target_location_id") != target["id"]
    ):
        raise LSGBlocked("LSG dispatch identifiers do not match the authorization")
    expected_handoff = {
        "schema_version": "lsg.robot-task.v0.1",
        "lsg_version": authorization["lsg_version"],
        "session_id": authorization["session_id"],
        "evaluation_id": authorization["evaluation_id"],
        "request_id": authorization["request_id"],
        "action_type": action["type"],
        "target": target,
        "resource": resource,
        "parameters": action.get("parameters", {}),
        "authorization_scope": authorization["authorization_scope"],
        "obligations": authorization["obligations"],
        "context_id": authorization["context_snapshot_id"],
        "context_hash": authorization["context_snapshot_hash"],
        "runtime_candidate_hash": authorization["runtime_candidate_hash"],
        "provenance": authorization["provenance"],
        "raw_text": request.get("raw_text"),
    }
    if item.get("authorized_task") != expected_handoff:
        raise LSGBlocked("LSG dry-run handoff differs from its authorization")

    parameters = action.get("parameters", {})
    if not isinstance(parameters, dict):
        raise LSGBlocked("LSG navigation parameters must be an object")
    if parameters:
        raise LSGBlocked(
            "LSG navigation parameters are not trusted by the RoboGuard bridge"
        )
    return AuthorizedTask(
        schema_version=authorization["schema_version"],
        evaluation_id=authorization["evaluation_id"],
        request_id=authorization["request_id"],
        action_type=action["type"],
        target=deepcopy(target),
        resource=deepcopy(resource),
        parameters=deepcopy(parameters),
        authorization_scope=authorization["authorization_scope"],
        obligations=tuple(authorization["obligations"]),
        matched_rules=(),
        policy_version=f"LSG/{authorization['lsg_version']}",
        context_id=scene_snapshot_id,
        raw_text=raw_text.strip(),
    )
