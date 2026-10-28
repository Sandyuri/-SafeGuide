"""Regression for the requested connection on the hospital map."""

from pathlib import Path

import pytest

from roboguard.medical.joint_planner import HospitalRoutePlanner
from roboguard.medical.providers import NavigationSandboxSceneProvider
from roboguard.navigation import build_navigation_view
from scripts.run_planner_benchmark import scenario_scene, task_for


ROOT = Path(__file__).resolve().parents[1]
SOURCE = "sg-f1-medication-consultation"
DESTINATION = "sg-f1-nebulization-room"


def make_scene(*, revoke_permissions=()):
    base = NavigationSandboxSceneProvider(
        ROOT / "data/fixtures/medical/hospital_1f.synthetic.json",
        ROOT / "navigation_sandbox",
        ROOT / "data/navigation/hospital_1f.bindings.json",
    ).load_scene()
    scenario = {
        "id": "direct-room-connection",
        "source": SOURCE,
        "ordered_destinations": [DESTINATION],
        "accessibility_needs": [],
        "revoke_permissions": list(revoke_permissions),
    }
    scene = scenario_scene(base, scenario)
    return base, scene, scenario


def test_authorized_route_uses_current_authoritative_corridor_connection():
    _, scene, scenario = make_scene()
    regions = scene.data["regions"]
    assert DESTINATION not in regions[SOURCE]["connection_costs"]
    expected = [
        SOURCE,
        "nav-f1-waypoint-019",
        "nav-f1-corridor-22",
        "nav-f1-waypoint-023",
        "nav-f1-waypoint-022",
        "nav-f1-waypoint-021",
        "nav-f1-corridor-16",
        DESTINATION,
    ]
    view = build_navigation_view(scene, task_for(scene, scenario))
    for name in ("dijkstra", "astar", "joint"):
        result = HospitalRoutePlanner(name).plan(scene=scene, planner_view=view)
        assert result.allowed
        assert [view.resolve_reference(ref) for ref in result.route] == expected
        assert result.distance_m == pytest.approx(30.658)


def test_direct_connection_does_not_bypass_destination_permission():
    base, _, _ = make_scene()
    zone = base.data["regions"][DESTINATION]["access_zone_id"]
    required = base.data["access_zones"][zone]["required_permissions"]
    assert required
    _, scene, scenario = make_scene(revoke_permissions=required)
    view = build_navigation_view(scene, task_for(scene, scenario))
    result = HospitalRoutePlanner("joint").plan(scene=scene, planner_view=view)
    assert not result.allowed
