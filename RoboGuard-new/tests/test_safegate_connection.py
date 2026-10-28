"""Offline checks for the SafeGate -> SPINE navigation boundary."""

from __future__ import annotations

import argparse
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from roboguard.medical.providers import NavigationSandboxSceneProvider
from roboguard.navigation import build_navigation_view
from roboguard.safegate_adapter import AuthorizedTask


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
SCENE = ROOT / "data/fixtures/medical/hospital_1f.synthetic.json"
SANDBOX = ROOT / "navigation_sandbox"
BINDINGS = ROOT / "data/navigation/hospital_1f.bindings.json"


def load_runner():
    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(
        "roboguard_new_safegate_runner",
        scripts / "run_spine_roboguard.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def provider_and_scene():
    provider = NavigationSandboxSceneProvider(SCENE, SANDBOX, BINDINGS)
    return provider, provider.load_scene()


def args(**overrides):
    values = {
        "source": "门诊大厅",
        "destination": "药房",
        "text": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.mark.parametrize("destination_parameter", [None, "药房", "sg-f1-pharmacy"])
def test_safegate_navigation_target_does_not_require_duplicate_parameter(
    destination_parameter,
):
    provider, scene = provider_and_scene()
    destination = provider.resolve_location("药房")
    task = AuthorizedTask(
        schema_version="0.2",
        evaluation_id="evaluation-offline",
        request_id="request-offline",
        action_type="NAVIGATE_TO_LOCATION",
        target={"kind": "LOCATION", "id": destination},
        resource=None,
        parameters={},
        authorization_scope="ALLOW_TO_ENTER_PLANNING",
        obligations=("REQUIRE_ROBOGUARD_EXECUTION_CHECK",),
        matched_rules=(),
        policy_version="0.2.2",
        context_id=scene.snapshot_id,
        raw_text="请带我去药房",
    )

    if destination_parameter is not None:
        task = replace(task, parameters={"destination": destination_parameter})
    view = build_navigation_view(
        scene, task, destination_aliases={"药房": destination}
    )

    assert view.payload["task"]["destination_region_ref"] == "task-destination-1"

    mismatched_task = replace(task, parameters={"destination": "卫生间"})
    with pytest.raises(ValueError, match="do not match"):
        build_navigation_view(
            scene,
            mismatched_task,
            destination_aliases={"卫生间": provider.resolve_location("卫生间")},
        )


def test_explicit_text_must_agree_with_destination_argument():
    runner = load_runner()

    with pytest.raises(ValueError, match="必须包含"):
        runner.request_text(args(text="请带我去挂号处"))


def test_text_only_request_infers_source_and_destination():
    runner = load_runner()
    provider, scene = provider_and_scene()
    text = runner.request_text(
        args(
            source=None,
            destination=None,
            text="请引导我从门诊大厅前往药房。",
        )
    )

    source = runner.infer_source(provider, scene, text)
    destination_text = runner.infer_destination_text(provider, text, source)
    destination = runner.resolve_destination(
        provider,
        scene,
        SANDBOX,
        destination_text,
        source,
    )

    assert source == provider.resolve_location("门诊大厅")
    assert destination_text == "药房"
    assert destination == provider.resolve_location("药房")


def test_destination_only_builds_request_from_live_location():
    runner = load_runner()

    assert runner.request_text(args(source=None)) == "请引导我前往药房。"


def test_request_requires_text_or_destination():
    runner = load_runner()

    with pytest.raises(ValueError, match="必须提供"):
        runner.request_text(args(source=None, destination=None))


@pytest.mark.parametrize(
    "alias",
    ["无障碍卫生间", "左下无障碍卫生间", "自助挂号收费机（南侧）", "功能检查等候区"],
)
def test_destination_inference_keeps_complete_location_name(alias):
    runner = load_runner()
    provider, scene = provider_and_scene()
    text = f"请从门诊大厅带我去{alias}。"
    source = runner.infer_source(provider, scene, text)
    assert runner.infer_destination_text(provider, text, source) == alias


def test_accessible_restroom_request_uses_nearest_accessible_candidate():
    runner = load_runner()
    provider, scene = provider_and_scene()
    text = "请从门诊大厅带我去最近的无障碍卫生间。"
    source = runner.infer_source(provider, scene, text)
    destination_text = runner.infer_destination_text(provider, text, source)
    destination = runner.resolve_destination(
        provider, scene, SANDBOX, destination_text, source
    )
    registry = json.loads(
        (SANDBOX / "semantics/location_registry.json").read_text(encoding="utf-8")
    )
    candidates = next(
        location["navigation_resolution"]["candidate_node_ids"]
        for location in registry["locations"]
        if location["canonical_name"] == "无障碍卫生间"
    )
    assert destination_text == "无障碍卫生间"
    assert destination == min(
        candidates,
        key=lambda node: runner.shortest_allowed_distance(scene, source, node),
    )
