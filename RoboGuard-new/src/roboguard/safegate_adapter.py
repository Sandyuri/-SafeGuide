"""Fail-closed adapter for terminal SafeGate orchestration results."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any

try:
    from jsonschema import Draft202012Validator, FormatChecker
    from jsonschema.exceptions import SchemaError, ValidationError
    from referencing import Registry, Resource
except ModuleNotFoundError:  # Offline planner benchmarks only need AuthorizedTask.
    Draft202012Validator = None
    FormatChecker = None
    Registry = None
    Resource = None
    SchemaError = ValidationError = Exception

SUPPORTED_SCHEMA_VERSION = "0.2"
SUPPORTED_OBLIGATIONS = frozenset({"REQUIRE_ROBOGUARD_EXECUTION_CHECK"})
REQUIRED_PLANNING_OBLIGATION = "REQUIRE_ROBOGUARD_EXECUTION_CHECK"
ROBOT_ACTION_TYPES = frozenset(
    {"NAVIGATE_TO_LOCATION", "DELIVER_DOCUMENT", "DELIVER_MEDICATION"}
)

class SafeGateBlocked(RuntimeError):
    """Raised when a request cannot safely enter SPINE planning."""

    def __init__(self, message: str, *, decision: str | None = None) -> None:
        super().__init__(message)
        self.decision = decision

@dataclass(frozen=True)
class SafeGateDecision:
    schema_version: str
    evaluation_id: str
    request_id: str
    decision: str
    reason_code: str
    authorization_scope: str
    matched_rules: tuple[dict[str, Any], ...]
    unmet_requirements: tuple[str, ...]
    obligations: tuple[str, ...]
    summary: str
    evaluated_at: str
    policy_version: str
    audit: dict[str, Any]

@dataclass(frozen=True)
class AuthorizedTask:
    """Canonical task boundary established by the evaluated request."""

    schema_version: str
    evaluation_id: str
    request_id: str
    action_type: str
    target: dict[str, Any] | None
    resource: dict[str, Any] | None
    parameters: dict[str, Any]
    authorization_scope: str
    obligations: tuple[str, ...]
    matched_rules: tuple[dict[str, Any], ...]
    policy_version: str
    context_id: str
    raw_text: str

@dataclass(frozen=True)
class SafeGateAuthorization:
    task: AuthorizedTask
    decision: SafeGateDecision

def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SafeGateBlocked(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise SafeGateBlocked(f"{label} must be a JSON object")
    return value

def _schema_registry(schema_dir: Path) -> Registry:
    if Registry is None or Resource is None:
        raise SafeGateBlocked(
            "SafeGate schema validation requires the declared jsonschema dependency"
        )
    resources: list[tuple[str, Resource[Any]]] = []
    try:
        for path in schema_dir.glob("*.json"):
            schema = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(schema, dict):
                raise SafeGateBlocked(f"SafeGate schema must be an object: {path}")
            resource = Resource.from_contents(schema)
            schema_id = schema.get("$id")
            if isinstance(schema_id, str):
                resources.append((schema_id, resource))
            resources.append((path.resolve().as_uri(), resource))
    except (OSError, json.JSONDecodeError) as error:
        raise SafeGateBlocked(f"cannot load SafeGate schemas: {error}") from error
    return Registry().with_resources(resources)

def _validate_schema(
    value: dict[str, Any],
    *,
    schema_path: Path,
    registry: Registry,
    label: str,
) -> None:
    if Draft202012Validator is None:
        raise SafeGateBlocked(
            "SafeGate schema validation requires the declared jsonschema dependency"
        )
    schema = _load_json_object(schema_path, label=f"{label} schema")
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(
            schema,
            registry=registry,
            format_checker=FormatChecker(),
        ).validate(value)
    except (SchemaError, ValidationError) as error:
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise SafeGateBlocked(
            f"{label} does not satisfy SafeGate v0.2 schema at {location}: "
            f"{error.message}"
        ) from error

def _validate_planning_admission(audit: dict[str, Any]) -> tuple[str, ...]:
    decision_name = audit.get("decision")
    if decision_name != "AUTHORIZE":
        raise SafeGateBlocked(
            f"{decision_name}: {audit.get('reason_code')}: {audit.get('summary')}",
            decision=decision_name,
        )
    if audit.get("authorization_scope") != "ALLOW_TO_ENTER_PLANNING":
        raise SafeGateBlocked(
            "SafeGate AUTHORIZE decision does not allow entry to planning"
        )

    value = audit.get("obligations")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SafeGateBlocked("SafeGate obligations must be a list of strings")
    obligations = tuple(value)
    if REQUIRED_PLANNING_OBLIGATION not in obligations:
        raise SafeGateBlocked(
            f"SafeGate decision is missing {REQUIRED_PLANNING_OBLIGATION}"
        )
    unknown = sorted(set(obligations) - SUPPORTED_OBLIGATIONS)
    if unknown:
        raise SafeGateBlocked(
            "unsupported SafeGate obligation(s): " + ", ".join(unknown)
        )
    return obligations

def _parse_decision(
    audit: dict[str, Any],
    *,
    evaluation_id: str,
    request_id: str,
    policy_version: str,
    obligations: tuple[str, ...],
) -> SafeGateDecision:
    return SafeGateDecision(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        evaluation_id=evaluation_id,
        request_id=request_id,
        decision=audit["decision"],
        reason_code=audit["reason_code"],
        authorization_scope=audit["authorization_scope"],
        matched_rules=tuple(deepcopy(audit["matched_rules"])),
        unmet_requirements=tuple(audit["unmet_requirements"]),
        obligations=obligations,
        summary=audit["summary"],
        evaluated_at=audit["evaluated_at"],
        policy_version=policy_version,
        audit=deepcopy(audit),
    )

def _build_authorized_task(
    payload: dict[str, Any], decision: SafeGateDecision
) -> AuthorizedTask:
    request = payload["request"]
    action = request["action"]
    if not isinstance(action, dict):
        raise SafeGateBlocked("authorized SafeGate request has no canonical action")
    action_type = action.get("type")
    if action_type not in ROBOT_ACTION_TYPES:
        raise SafeGateBlocked(
            f"unsupported robot planning action type: {action_type!r}"
        )

    trusted_context = payload["trusted_context"]
    return AuthorizedTask(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        evaluation_id=decision.evaluation_id,
        request_id=decision.request_id,
        action_type=action_type,
        target=deepcopy(action.get("target")),
        resource=deepcopy(action.get("resource")),
        parameters=deepcopy(action.get("parameters", {})),
        authorization_scope=decision.authorization_scope,
        obligations=decision.obligations,
        matched_rules=tuple(deepcopy(decision.matched_rules)),
        policy_version=decision.policy_version,
        context_id=trusted_context["context_id"],
        raw_text=request["raw_text"].strip(),
    )

def authorize_orchestration_result_for_spine(
    *,
    result: dict[str, Any],
    trusted_context: dict[str, Any],
    safegate_root: str | Path,
) -> SafeGateAuthorization:
    """Convert one terminal v0.3 robot authorization for SPINE.

    The AuthorizedTask is built only from the request that SafeGate Core
    actually evaluated and the trusted scene context used to start the
    orchestration session.
    """

    import hashlib

    if not isinstance(result, dict) or result.get("schema_version") != "0.3":
        raise SafeGateBlocked("unsupported orchestration result schema_version")

    decision_name = result.get("decision")
    if decision_name != "AUTHORIZE":
        raise SafeGateBlocked(
            f"{decision_name}: {result.get('reason_code')}: "
            f"{result.get('user_message')}",
            decision=decision_name if isinstance(decision_name, str) else None,
        )
    if result.get("task_type") != "ROBOT_TASK":
        raise SafeGateBlocked("orchestration result is not a robot task")
    if (
        result.get("status") != "AUTHORIZED_FOR_PLANNING"
        or result.get("terminal") is not True
    ):
        raise SafeGateBlocked(
            "orchestration result does not terminally authorize planning"
        )
    if result.get("authorization_scope") != "ALLOW_TO_ENTER_PLANNING":
        raise SafeGateBlocked(
            "orchestration authorization_scope is not planning"
        )

    if not isinstance(trusted_context, dict):
        raise SafeGateBlocked("trusted context must be an object")
    context_id = trusted_context.get("context_id")
    context_hash = hashlib.sha256(
        json.dumps(
            trusted_context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if (
        result.get("context_snapshot_id") != context_id
        or result.get("context_snapshot_hash") != context_hash
    ):
        raise SafeGateBlocked(
            "orchestration context snapshot does not match the trusted scene"
        )

    root = Path(safegate_root).resolve()
    schema_dir = root / "schemas"
    registry = _schema_registry(schema_dir)
    _validate_schema(
        trusted_context,
        schema_path=schema_dir / "trusted-context.schema.json",
        registry=registry,
        label="trusted context",
    )

    request = result.get("evaluated_request")
    if not isinstance(request, dict):
        raise SafeGateBlocked(
            "orchestration result lacks the request actually evaluated by Core"
        )
    _validate_schema(
        request,
        schema_path=schema_dir / "normalized-request.schema.json",
        registry=registry,
        label="evaluated request",
    )

    normalized = result.get("normalized_request")
    if not isinstance(normalized, dict):
        raise SafeGateBlocked("orchestration result lacks normalized_request")
    _validate_schema(
        normalized,
        schema_path=schema_dir / "normalized-request.schema.json",
        registry=registry,
        label="normalized request",
    )
    if (
        request.get("request_id") != normalized.get("request_id")
        or request.get("raw_text") != normalized.get("raw_text")
    ):
        raise SafeGateBlocked(
            "evaluated request does not correlate with normalized request"
        )

    evaluated_action = request.get("action")
    normalized_action = normalized.get("action")
    if not isinstance(evaluated_action, dict) or not isinstance(
        normalized_action, dict
    ):
        raise SafeGateBlocked("authorized orchestration request has no action")
    if evaluated_action.get("type") != normalized_action.get("type"):
        raise SafeGateBlocked(
            "evaluated action type differs from normalized action type"
        )
    if evaluated_action.get("type") not in ROBOT_ACTION_TYPES:
        raise SafeGateBlocked("unsupported SafeGate robot action")
    if request.get("ambiguities"):
        raise SafeGateBlocked(
            "authorized evaluated request contains unresolved ambiguities"
        )

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts.build_safegate_input import resolve_trusted_references

    if resolve_trusted_references(normalized, trusted_context) != request:
        raise SafeGateBlocked(
            "evaluated request is not the trusted resolution of "
            "normalized_request"
        )

    audit = result.get("safegate_output")
    if not isinstance(audit, dict):
        raise SafeGateBlocked("orchestration result lacks Core decision JSON")
    _validate_schema(
        audit,
        schema_path=schema_dir / "output.schema.json",
        registry=registry,
        label="SafeGate output",
    )
    if audit.get("policy_version") != "0.2.2":
        raise SafeGateBlocked("unsupported orchestration policy_version")

    evaluation_id = result.get("evaluation_id")
    request_id = result.get("request_id")
    if (
        not isinstance(evaluation_id, str)
        or not evaluation_id
        or not isinstance(request_id, str)
        or not request_id
        or audit.get("evaluation_id") != evaluation_id
        or audit.get("request_id") != request_id
        or request.get("request_id") != request_id
    ):
        raise SafeGateBlocked(
            "orchestration request/evaluation identifiers do not match"
        )

    for field in (
        "decision",
        "reason_code",
        "authorization_scope",
        "matched_rules",
        "unmet_requirements",
        "obligations",
        "summary",
    ):
        if result.get(field) != audit.get(field):
            raise SafeGateBlocked(
                f"orchestration {field} differs from Core decision"
            )

    obligations = _validate_planning_admission(audit)
    decision = _parse_decision(
        audit,
        evaluation_id=evaluation_id,
        request_id=request_id,
        policy_version="0.2.2",
        obligations=obligations,
    )
    task = _build_authorized_task(
        {"request": request, "trusted_context": trusted_context}, decision
    )
    return SafeGateAuthorization(task=task, decision=decision)
