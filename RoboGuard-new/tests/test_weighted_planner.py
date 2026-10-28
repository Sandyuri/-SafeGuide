"""Regression tests for metre-weighted route planning."""

from roboguard.medical.joint_planner import HospitalRoutePlanner

from test_joint_planner import Scene, make_view, region


def test_dijkstra_prefers_shorter_distance_over_fewer_edges():
    # s-a-d uses fewer edges but is 20 m. s-b-c-d is 6 m and must win.
    graph = {
        "s": region(0, 0, ["a", "b"], {"a": 10.0, "b": 2.0}),
        "a": region(1, 0, ["s", "d"], {"s": 10.0, "d": 10.0}),
        "b": region(0, 1, ["s", "c"], {"s": 2.0, "c": 2.0}),
        "c": region(1, 1, ["b", "d"], {"b": 2.0, "d": 2.0}),
        "d": region(2, 0, ["a", "c"], {"a": 10.0, "c": 2.0}),
    }

    result = HospitalRoutePlanner("dijkstra").plan(
        scene=Scene(),
        planner_view=make_view(graph=graph),
    )

    assert result.allowed
    assert result.route == ("s", "b", "c", "d")
    assert result.distance_m == 6.0
