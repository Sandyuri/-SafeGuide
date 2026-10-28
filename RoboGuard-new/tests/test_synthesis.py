"""Spot-backed LTL smoke tests."""

import pytest

pytest.importorskip("spot")

from roboguard.synthesis import ControlSynthesis


def test_spot_rejects_forbidden_intermediate_region():
    checker = ControlSynthesis(["G(!goto(task-region-1))"])
    safe, _ = checker.validate_action_sequence([("goto", "task-region-2")])
    unsafe, trace = checker.validate_action_sequence([("goto", "task-region-1")])
    assert safe is True
    assert unsafe is False
    assert trace[-1] == ("goto(task-region-1)", False)


def test_eventual_destination_is_checked_at_end_of_trace():
    checker = ControlSynthesis(["F(goto(task-destination-1))"])
    safe, trace = checker.validate_action_sequence([
        ("goto", "task-region-1"),
        ("goto", "task-destination-1"),
    ])
    assert safe is True
    assert all(allowed for _, allowed in trace)
