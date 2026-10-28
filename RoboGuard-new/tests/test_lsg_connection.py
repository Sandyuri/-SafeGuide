"""Contract tests for the Learned SafeGate to RoboGuard bridge."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

from roboguard.lsg_adapter import (
    LSGBlocked,
    authorize_lsg_response_for_roboguard,
    load_lsg_default_context,
    load_lsg_expected_provenance,
    request_lsg_json,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LSG_ROOT = PROJECT_ROOT.parent / "LSG" / "LSG"
LSG_IMPORT_ROOT = LSG_ROOT.parent
PROVENANCE = {
    "splitter_checkpoint_sha256": "1" * 64,
    "gate_checkpoint_sha256": "2" * 64,
    "splitter_manifest_sha256": "3" * 64,
    "gate_manifest_sha256": "4" * 64,
}


class Splitter:
    def predict(self, text):
        return f"[意图1] {text}"


class Gate:
    def predict(self, text, candidates):
        return {
            "decision": "AUTHORIZE",
            "task_type": "ROBOT_TASK",
            "response_branch": None,
            "defer_type": None,
            "confidence": {"decision": 1.0, "task_type": 1.0},
        }


def lsg_response():
    if str(LSG_IMPORT_ROOT) not in sys.path:
        sys.path.insert(0, str(LSG_IMPORT_ROOT))
    from LSG.runtime.pipeline import Pipeline

    context = load_lsg_default_context(LSG_ROOT)
    result = Pipeline(
        Splitter(),
        Gate(),
        integration_manifest={},
        model_provenance=PROVENANCE,
        location_resolver=None,
    ).run("带我去卫生间", context, dispatch=False)
    # Match the tuple-to-array normalization performed by the HTTP JSON layer.
    response = json.loads(json.dumps({"native_result": result.to_dict()}))
    return response, context


def test_lsg_authorization_is_bound_to_current_roboguard_snapshot():
    response, context = lsg_response()

    task = authorize_lsg_response_for_roboguard(
        response=response,
        trusted_context=context,
        expected_provenance=PROVENANCE,
        expected_destination_id="sg-f1-restroom",
        scene_snapshot_id="scene-snapshot-current",
        lsg_root=LSG_ROOT,
    )

    assert task.action_type == "NAVIGATE_TO_LOCATION"
    assert task.target == {"kind": "LOCATION", "id": "sg-f1-restroom"}
    assert task.context_id == "scene-snapshot-current"
    assert task.obligations == ("REQUIRE_ROBOGUARD_EXECUTION_CHECK",)
    assert task.policy_version == "LSG/0.1"


def test_lsg_bridge_rejects_tampered_candidate_hash():
    response, context = lsg_response()
    tampered = deepcopy(response)
    tampered["native_result"]["runtime_candidates"][0]["trusted_locations"][
        "candidates"
    ][0]["location_id"] = "sg-f1-pharmacy"

    with pytest.raises(LSGBlocked, match="candidate hash mismatch"):
        authorize_lsg_response_for_roboguard(
            response=tampered,
            trusted_context=context,
            expected_provenance=PROVENANCE,
            expected_destination_id="sg-f1-restroom",
            scene_snapshot_id="scene-snapshot-current",
            lsg_root=LSG_ROOT,
        )


def test_lsg_bridge_rejects_a_different_resolved_destination():
    response, context = lsg_response()

    with pytest.raises(LSGBlocked, match="does not match"):
        authorize_lsg_response_for_roboguard(
            response=response,
            trusted_context=context,
            expected_provenance=PROVENANCE,
            expected_destination_id="sg-f1-pharmacy",
            scene_snapshot_id="scene-snapshot-current",
            lsg_root=LSG_ROOT,
        )


def test_lsg_expected_provenance_is_loaded_from_local_registry():
    provenance = load_lsg_expected_provenance(LSG_ROOT)
    assert set(provenance) == set(PROVENANCE)
    assert provenance["splitter_checkpoint_sha256"] == (
        "50ac3eb962b481bb8d0156723ef08d264e456ce256227f34970acf6c4a055887"
    )
    assert provenance["gate_checkpoint_sha256"] == (
        "b2a75c8e6e2aac87fa442aab0bd562fcf757c108eca3a031e9151f517fab494b"
    )


def test_lsg_bridge_rejects_privileged_navigation_parameters():
    response, context = lsg_response()
    parameters = {"user_role": "CLINICAL_STAFF", "urgency": "EMERGENCY"}
    authorization = response["native_result"]["authorizations"][0]
    authorization["normalized_request"]["action"]["parameters"] = parameters
    authorization["evaluated_request"]["action"]["parameters"] = parameters
    response["native_result"]["dispatch_plan"]["items"][0]["authorized_task"][
        "parameters"
    ] = parameters

    with pytest.raises(LSGBlocked, match="parameters are not trusted"):
        authorize_lsg_response_for_roboguard(
            response=response,
            trusted_context=context,
            expected_provenance=PROVENANCE,
            expected_destination_id="sg-f1-restroom",
            scene_snapshot_id="scene-snapshot-current",
            lsg_root=LSG_ROOT,
        )


def test_lsg_bridge_binds_request_session_and_raw_input():
    response, context = lsg_response()
    native = response["native_result"]
    native["request_id"] = "external-1"

    with pytest.raises(LSGBlocked, match="raw input mismatch"):
        authorize_lsg_response_for_roboguard(
            response=response,
            trusted_context=context,
            expected_provenance=PROVENANCE,
            expected_destination_id="sg-f1-restroom",
            scene_snapshot_id="scene-snapshot-current",
            lsg_root=LSG_ROOT,
            expected_request_id="external-1",
            expected_session_id=native["session_id"],
            expected_raw_input="different request",
        )


def test_lsg_bridge_rejects_replaced_evaluated_text():
    response, context = lsg_response()
    native = response["native_result"]
    authorization = native["authorizations"][0]
    replacement = "这是被替换后的请求"
    authorization["normalized_request"]["raw_text"] = replacement
    authorization["evaluated_request"]["raw_text"] = replacement
    native["dispatch_plan"]["items"][0]["authorized_task"][
        "raw_text"
    ] = replacement

    with pytest.raises(LSGBlocked, match="parsed intent"):
        authorize_lsg_response_for_roboguard(
            response=response,
            trusted_context=context,
            expected_provenance=PROVENANCE,
            expected_destination_id="sg-f1-restroom",
            scene_snapshot_id="scene-snapshot-current",
            lsg_root=LSG_ROOT,
            expected_raw_input="带我去卫生间",
        )


def test_lsg_bridge_fail_closes_malformed_dispatch_item():
    response, context = lsg_response()
    response["native_result"]["dispatch_plan"]["items"] = ["bad"]

    with pytest.raises(LSGBlocked, match="dry-run"):
        authorize_lsg_response_for_roboguard(
            response=response,
            trusted_context=context,
            expected_provenance=PROVENANCE,
            expected_destination_id="sg-f1-restroom",
            scene_snapshot_id="scene-snapshot-current",
            lsg_root=LSG_ROOT,
        )


def test_lsg_http_bridge_accepts_distinct_origin_and_destination(tmp_path):
    if str(LSG_IMPORT_ROOT) not in sys.path:
        sys.path.insert(0, str(LSG_IMPORT_ROOT))
    from LSG.runtime.application import LSGApplication
    from LSG.runtime.session import SessionStore
    from LSG.scripts.serve_lsg import Handler

    provenance = load_lsg_expected_provenance(LSG_ROOT)
    app = LSGApplication(
        splitter_factory=Splitter,
        gate_factory=Gate,
        verifier=lambda: {},
        provenance_factory=lambda: provenance,
        session_store=SessionStore(tmp_path / "sessions"),
    ).start()
    Handler.app = app
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        request_id = "roboguard-http-e2e-1"
        context = load_lsg_default_context(LSG_ROOT)
        response = request_lsg_json(
            base_url,
            "/evaluate",
            payload={
                "text": "请从药房带我去卫生间",
                "request_id": request_id,
                "trusted_context": context,
                "dispatch": True,
            },
            timeout=5,
        )
        native = response["native_result"]
        task = authorize_lsg_response_for_roboguard(
            response=response,
            trusted_context=context,
            expected_provenance=provenance,
            expected_destination_id="sg-f1-restroom",
            scene_snapshot_id="scene-http-e2e",
            lsg_root=LSG_ROOT,
            expected_request_id=request_id,
            expected_session_id=native["session_id"],
            expected_raw_input="请从药房带我去卫生间",
        )
        assert task.target["id"] == "sg-f1-restroom"
        assert task.parameters == {}
    finally:
        server.shutdown()
        server.server_close()
        app.shutdown()
        worker.join(timeout=5)
