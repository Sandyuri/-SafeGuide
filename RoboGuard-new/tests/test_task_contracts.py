"""Tests for the unified navigation task and route context contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from roboguard.medical.providers import NavigationSandboxSceneProvider
from roboguard.medical.joint_planner import DynamicEventSet
from roboguard.medical.safety_barrier import require_execution_permit
from roboguard.medical.task_contracts import (
    build_route_context,
    build_task_spec,
)
from roboguard.medical.temporal_guard import MedicalTemporalGuard
from roboguard.navigation import NavigationExecutionGuard, build_navigation_view
from roboguard.safegate_adapter import AuthorizedTask
from roboguard.spine_adapter import SpineAction

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "data/fixtures/medical/hospital_1f.synthetic.json"
SANDBOX = ROOT / "navigation_sandbox"
BINDINGS = ROOT / "data/navigation/hospital_1f.bindings.json"


def provider_and_scene():
    provider = NavigationSandboxSceneProvider(SCENE, SANDBOX, BINDINGS)
    return provider, provider.load_scene()


def authorized_task(scene, destination, *, parameters=None):
    return AuthorizedTask(
        schema_version="0.2",
        evaluation_id="evaluation-task-contract",
        request_id="request-task-contract",
        action_type="NAVIGATE_TO_LOCATION",
        target={"kind": "LOCATION", "id": destination},
        resource=None,
        parameters=parameters or {},
        authorization_scope="ALLOW_TO_ENTER_PLANNING",
        obligations=("REQUIRE_ROBOGUARD_EXECUTION_CHECK",),
        matched_rules=(),
        policy_version="0.2.2",
        context_id=scene.snapshot_id,
        raw_text="offline task contract test",
    )


def test_single_destination_defaults_remain_compatible():
    provider, scene = provider_and_scene()
    destination = provider.resolve_location("药房")
    task = authorized_task(scene, destination)

    spec = build_task_spec(scene, task)
    context = build_route_context(scene, task)

    assert spec.source_region_id == scene.data["robot_state"]["current_region_id"]
    assert spec.ordered_destination_ids == (destination,)
    assert spec.user_role == "GENERAL_PUBLIC"
    assert spec.accessibility_needs == ()
    assert spec.carrying_item_ids == ()
    assert spec.urgency == "ROUTINE"
    assert context.authorization_id == task.evaluation_id
    assert context.scene_snapshot_hash == scene.snapshot_hash
    assert context.map_id == "hospital_1f"


def test_mental_health_route_uses_scene_attested_robot_role():
    provider, scene = provider_and_scene()
    destination = provider.resolve_location("精神心理科")
    task = authorized_task(scene, destination)
    view = build_navigation_view(scene, task)
    destination_ref = view.payload["task"]["destination_region_ref"]

    result = NavigationExecutionGuard(
        temporal_guard=MedicalTemporalGuard(mode="off"),
        planner="joint",
    ).review_plan(
        scene=scene,
        authorized_task=task,
        planner_view=view,
        actions=[SpineAction("goto", (destination_ref,))],
    )

    assert view.payload["robot"]["roles"] == ["AUTHORIZED_ROBOT"]
    assert result.decision == "ALLOW"
    assert result.reason_code == "NAVIGATION_PLAN_ALLOWED"
    assert result.checked_routes[0].regions[-1] == destination_ref


def test_mental_health_route_fails_closed_without_robot_role():
    provider, scene = provider_and_scene()
    scene_data = scene.to_dict()
    scene_data["robot_state"].pop("roles")
    scene_without_role = type(scene).from_mapping(scene_data)
    destination = provider.resolve_location("精神心理科")
    task = authorized_task(scene_without_role, destination)
    view = build_navigation_view(scene_without_role, task)
    destination_ref = view.payload["task"]["destination_region_ref"]

    result = NavigationExecutionGuard(
        temporal_guard=MedicalTemporalGuard(mode="off"),
        planner="joint",
    ).review_plan(
        scene=scene_without_role,
        authorized_task=task,
        planner_view=view,
        actions=[SpineAction("goto", (destination_ref,))],
    )

    assert result.decision == "REJECT"
    assert result.reason_code == "INDEPENDENT_VALIDATION_FAILED"
    assert "ACCESS-001" in result.message


def test_pharmacy_public_pickup_is_separate_from_delivery_task_source():
    provider, scene = provider_and_scene()
    public_pickup = "sg-f1-pharmacy"
    task_source = "task-f1-pharmacy-medication-delivery"

    assert provider.resolve_location("我要去取药") == public_pickup
    assert provider.resolve_location("药房送药服务点") is None
    assert scene.data["delivery_items"]["med-package-demo-001"]["current_region_id"] == task_source
    assert scene.data["medications"]["medication-demo-001"]["storage_region_id"] == task_source
    assert scene.data["regions"][public_pickup]["access_zone_id"] == "public"
    assert scene.data["regions"][task_source]["access_zone_id"] == "clinical_restricted"
    assert (
        scene.data["regions"][task_source]["route_policy"]
        == "MEDICATION_DELIVERY_ONLY"
    )
    assert "MEDICATION_DELIVERY_PICKUP" in scene.data["regions"][task_source]["semantic_tags"]


def test_medication_delivery_routes_are_not_exposed_to_general_navigation():
    _, scene = provider_and_scene()
    task_nodes = {
        "task-f1-pharmacy-medication-delivery",
        "task-f1-pharmacy-delivery-north-gate",
        "task-f1-pharmacy-delivery-junction",
        "task-f1-pharmacy-delivery-south-gate",
    }
    for node_id in task_nodes:
        region = scene.data["regions"][node_id]
        assert region["navigation_status"] == "BLOCKED"
        assert tuple(region["connections"]) == ()

    assert "task-f1-pharmacy-delivery-north-gate" not in scene.data["regions"][
        "nav-f1-waypoint-017"
    ]["connections"]
    assert "task-f1-pharmacy-delivery-south-gate" not in scene.data["regions"][
        "nav-f1-waypoint-032"
    ]["connections"]
    assert "task-f1-pharmacy-delivery-north-gate" not in scene.data["regions"][
        "nav-f1-corridor-12"
    ]["connections"]
    assert "task-f1-pharmacy-delivery-south-gate" not in scene.data["regions"][
        "nav-f1-waypoint-021"
    ]["connections"]
    assert "task-f1-pharmacy-delivery-south-gate" not in scene.data["regions"][
        "nav-f1-waypoint-041"
    ]["connections"]

    task_data = json.loads(
        (SANDBOX / "semantics/task_destinations.json").read_text(encoding="utf-8")
    )
    destination = task_data["destinations"][0]
    assert destination["general_navigation_allowed"] is False
    assert {item["route_id"] for item in destination["route_variants"]} == {
        "pharmacy_delivery_north_route",
        "pharmacy_delivery_south_route",
        "pharmacy_delivery_north_east_route",
        "pharmacy_delivery_south_west_route",
        "pharmacy_delivery_south_lower_route",
    }


def test_multistage_registration_then_exam_task_can_be_constructed():
    _, scene = provider_and_scene()
    registration = "sg-f1-registration-payment-counter"
    exam = "sg-f1-functional-exam-waiting"
    task = authorized_task(
        scene,
        exam,
        parameters={
            "ordered_destinations": [registration, exam],
            "user_role": "PATIENT",
            "wheelchair_accessible": True,
            "urgency": "PRIORITY",
        },
    )

    spec = build_task_spec(scene, task)
    view = build_navigation_view(scene, task)

    assert spec.ordered_destination_ids == (registration, exam)
    assert spec.multi_stage is True
    assert spec.accessibility_needs == ("WHEELCHAIR_ACCESSIBLE",)
    assert spec.user_role == "PATIENT"
    assert spec.urgency == "PRIORITY"
    destination_refs = view.payload["task"]["ordered_destination_region_refs"]
    assert tuple(view.resolve_reference(item) for item in destination_refs) == (
        registration,
        exam,
    )
    result = NavigationExecutionGuard().review_plan(
        scene=scene,
        authorized_task=task,
        planner_view=view,
        actions=[],
    )
    assert result.decision == "PAUSE"
    assert result.reason_code == "MULTI_STAGE_WORKFLOW_REVIEW_REQUIRED"


def test_workflow_requirements_are_validated_and_bound_to_planner_view():
    _, scene = provider_and_scene()
    registration = "sg-f1-registration-payment-counter"
    exam = "sg-f1-functional-exam-waiting"
    task = authorized_task(
        scene,
        exam,
        parameters={
            "ordered_destinations": [registration, exam],
            "confirmation_destinations": [registration],
            "require_handoff": True,
            "require_explicit_finish": True,
        },
    )
    spec = build_task_spec(scene, task)
    view = build_navigation_view(scene, task)
    assert spec.confirmation_destination_ids == (registration,)
    assert spec.require_handoff is True
    assert spec.require_explicit_finish is True
    confirmation_refs = view.payload["task"][
        "confirmation_destination_region_refs"
    ]
    assert tuple(view.resolve_reference(item) for item in confirmation_refs) == (
        registration,
    )
    assert view.payload["task"]["require_handoff"] is True
    assert view.payload["task"]["require_explicit_finish"] is True


def test_confirmation_destination_must_be_in_ordered_workflow():
    provider, scene = provider_and_scene()
    pharmacy = provider.resolve_location("药房")
    task = authorized_task(
        scene,
        pharmacy,
        parameters={"confirmation_destinations": ["sg-f1-registration-payment-counter"]},
    )
    with pytest.raises(ValueError, match="must be ordered destinations"):
        build_task_spec(scene, task)


def test_final_ordered_destination_must_match_authorized_target():
    _, scene = provider_and_scene()
    task = authorized_task(
        scene,
        "sg-f1-functional-exam-waiting",
        parameters={
            "ordered_destinations": [
                "sg-f1-functional-exam-waiting",
                "sg-f1-registration-payment-counter",
            ]
        },
    )

    with pytest.raises(ValueError, match="final ordered destination"):
        build_task_spec(scene, task)


def test_authorized_carrying_items_and_kinds_are_bound_for_validation():
    provider, scene = provider_and_scene()
    destination = provider.resolve_location("药房")
    item_id = "med-package-demo-001"
    task = authorized_task(
        scene,
        destination,
        parameters={"carrying": [item_id]},
    )
    view = build_navigation_view(scene, task)

    carrying_refs = tuple(view.payload["task"]["carrying_refs"])
    assert carrying_refs == ("task-carrying-1",)
    assert view.resolve_reference(carrying_refs[0]) == item_id
    assert view.payload["task"]["carrying_kinds"] == [
        scene.data["delivery_items"][item_id]["kind"]
    ]


def test_multistage_actions_are_jointly_planned_in_authorized_order():
    _, scene = provider_and_scene()
    registration = "sg-f1-registration-payment-counter"
    exam = "sg-f1-functional-exam-waiting"
    task = authorized_task(
        scene,
        exam,
        parameters={"ordered_destinations": [registration, exam]},
    )
    view = build_navigation_view(scene, task)
    refs = tuple(view.payload["task"]["ordered_destination_region_refs"])
    actions = [SpineAction("goto", (item,)) for item in refs]

    result = NavigationExecutionGuard(
        temporal_guard=MedicalTemporalGuard(mode="off"),
        planner="joint",
    ).review_plan(
        scene=scene,
        authorized_task=task,
        planner_view=view,
        actions=actions,
    )

    assert result.decision == "ALLOW"
    assert result.checked_routes[0].planner == "joint"
    route_ids = [view.resolve_reference(item) for item in result.checked_routes[0].regions]
    assert route_ids.index(registration) < route_ids.index(exam)


def test_pareto_pipeline_selects_route_and_preserves_selection_audit():
    provider, scene = provider_and_scene()
    destination = provider.resolve_location("药房")
    task = authorized_task(scene, destination)
    view = build_navigation_view(scene, task)
    destination_ref = view.payload["task"]["destination_region_ref"]
    result = NavigationExecutionGuard(
        temporal_guard=MedicalTemporalGuard(mode="off"),
        planner="pareto",
    ).review_plan(
        scene=scene,
        authorized_task=task,
        planner_view=view,
        actions=[SpineAction("goto", (destination_ref,))],
    )
    assert result.decision == "ALLOW"
    route = result.checked_routes[0]
    assert route.planner == "pareto"
    assert route.selection_profile == "ROUTINE"
    assert route.candidate_count >= 1
    assert route.selection_reason
    assert route.workflow_trace[-1][3] == "COMPLETE"


def test_pareto_pipeline_completes_confirm_handoff_and_finish_workflow():
    _, scene = provider_and_scene()
    registration = "sg-f1-registration-payment-counter"
    exam = "sg-f1-functional-exam-waiting"
    task = authorized_task(
        scene,
        exam,
        parameters={
            "ordered_destinations": [registration, exam],
            "confirmation_destinations": [registration],
            "require_handoff": True,
            "require_explicit_finish": True,
        },
    )
    view = build_navigation_view(scene, task)
    destinations = tuple(view.payload["task"]["ordered_destination_region_refs"])
    result = NavigationExecutionGuard(
        temporal_guard=MedicalTemporalGuard(mode="off"),
        planner="pareto",
    ).review_plan(
        scene=scene,
        authorized_task=task,
        planner_view=view,
        actions=[SpineAction("goto", (item,)) for item in destinations],
    )
    assert result.decision == "ALLOW"
    actions = [item[1] for item in result.checked_routes[0].workflow_trace]
    assert actions == [
        f"ARRIVE:{registration}",
        f"CONFIRM:{registration}",
        f"ARRIVE:{exam}",
        "HANDOFF",
        "FINISH",
    ]


@pytest.mark.parametrize(
    "planner",
    ["dijkstra", "astar", "generate_then_check", "joint", "pareto"],
)
def test_every_planner_uses_validation_and_the_unified_barrier(planner):
    provider, scene = provider_and_scene()
    destination = provider.resolve_location("药房")
    task = authorized_task(scene, destination)
    view = build_navigation_view(scene, task)
    destination_ref = view.payload["task"]["destination_region_ref"]
    result = NavigationExecutionGuard(
        temporal_guard=MedicalTemporalGuard(mode="off"),
        planner=planner,
    ).review_plan(
        scene=scene,
        authorized_task=task,
        planner_view=view,
        actions=[SpineAction("goto", (destination_ref,))],
    )
    assert result.decision == "ALLOW"
    route = result.checked_routes[0]
    assert route.execution_authorized is True
    assert route.safety_certificate_id.startswith("sha256:")
    assert "VALIDATION_METRIC_BINDING" in route.checked_properties


def test_dstar_entry_replans_an_approved_route_and_replaces_its_permit():
    provider, scene = provider_and_scene()
    destination = provider.resolve_location("药房")
    task = authorized_task(scene, destination)
    view = build_navigation_view(scene, task)
    destination_ref = view.payload["task"]["destination_region_ref"]
    actions = [SpineAction("goto", (destination_ref,))]

    baseline = NavigationExecutionGuard(
        temporal_guard=MedicalTemporalGuard(mode="off"),
        planner="pareto",
    ).review_plan(
        scene=scene,
        authorized_task=task,
        planner_view=view,
        actions=actions,
    )
    assert baseline.allowed
    assert baseline.planning_result is not None
    old_route = baseline.checked_routes[0]

    blocked = view.resolve_reference(old_route.regions[len(old_route.regions) // 2])
    events = DynamicEventSet.from_mapping(
        {
            "plan_version": 2,
            "events": [{"type": "NODE_BLOCKED", "region_id": blocked}],
        }
    )
    replanned = NavigationExecutionGuard(
        temporal_guard=MedicalTemporalGuard(mode="off"),
        planner="pareto",
        events=events,
    ).review_plan(
        scene=scene,
        authorized_task=task,
        planner_view=view,
        actions=actions,
        previous_plan=baseline.planning_result,
    )

    assert replanned.allowed
    new_route = replanned.checked_routes[0]
    require_execution_permit(new_route, expected_plan_version=2)
    assert new_route.plan_version == 2
    assert new_route.safety_certificate["source_type"] in {
        "DSTAR_LITE_REPAIR",
        "FULL_PARETO_REPLAN",
    }
    with pytest.raises(ValueError, match="stale plan version"):
        require_execution_permit(old_route, expected_plan_version=2)
