from __future__ import annotations

from datetime import datetime, timezone

from scripts.export_all_legal_routes import deterministic_legal_path


def _region(*, connections=(), costs=None, policy="ALLOW_TRANSIT"):
    return {
        "connections": tuple(connections),
        "connection_costs": costs or {},
        "access_zone_id": "public",
        "navigation_status": "OPEN",
        "navigation_status_valid_until": None,
        "route_policy": policy,
    }


def test_destination_only_node_is_not_used_as_transit():
    regions = {
        "a": _region(connections=("b", "c"), costs={"b": 1.0, "c": 2.0}),
        "b": _region(
            connections=("a", "d"),
            costs={"a": 1.0, "d": 1.0},
            policy="DESTINATION_ONLY",
        ),
        "c": _region(connections=("a", "d"), costs={"a": 2.0, "d": 2.0}),
        "d": _region(connections=("b", "c"), costs={"b": 1.0, "c": 2.0}),
    }
    found = deterministic_legal_path(
        regions=regions,
        access_zones={"public": {"required_permissions": []}},
        source="a",
        destination="d",
        robot_permissions=set(),
        current_time=datetime.now(timezone.utc),
    )
    assert found == (("a", "c", "d"), 4.0)


def test_destination_only_node_can_be_the_destination():
    regions = {
        "a": _region(connections=("b",), costs={"b": 1.0}),
        "b": _region(
            connections=("a",),
            costs={"a": 1.0},
            policy="DESTINATION_ONLY",
        ),
    }
    found = deterministic_legal_path(
        regions=regions,
        access_zones={"public": {"required_permissions": []}},
        source="a",
        destination="b",
        robot_permissions=set(),
        current_time=datetime.now(timezone.utc),
    )
    assert found == (("a", "b"), 1.0)
