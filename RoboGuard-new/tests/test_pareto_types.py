"""Tests for Pareto product states, labels, and bounded frontiers."""

from __future__ import annotations

import math

import pytest

from roboguard.medical.guard_types import ALLOW
from roboguard.medical.pareto_types import (
    ParetoFrontier,
    ParetoLabel,
    ParetoPlanResult,
    ProductState,
    insert_nondominated_label,
)


STATE = ProductState("destination", "COMPLETE", 1)


def label(risk, distance, time, burden=0, *, route=("source", "destination")):
    return ParetoLabel(
        state=STATE,
        risk=risk,
        distance_m=distance,
        estimated_time_s=time,
        patient_burden=burden,
        route=route,
    )


def test_conflicting_risk_and_distance_labels_are_both_retained():
    safer = label(0, 30, 35)
    shorter = label(10, 20, 25)
    first = insert_nondominated_label((), safer)
    second = insert_nondominated_label(first.labels, shorter)
    assert second.accepted
    assert second.labels == (safer, shorter)


def test_dominated_candidate_is_rejected():
    better = label(1, 20, 25)
    worse = label(2, 21, 26)
    result = insert_nondominated_label((better,), worse)
    assert result.accepted is False
    assert result.reason == "DOMINATED"
    assert result.labels == (better,)


def test_better_candidate_prunes_dominated_existing_label():
    worse = label(2, 21, 26)
    better = label(1, 20, 25)
    result = insert_nondominated_label((worse,), better)
    assert result.accepted
    assert result.pruned_count == 1
    assert result.labels == (better,)


def test_duplicate_label_is_not_inserted_twice():
    candidate = label(1, 20, 25)
    result = insert_nondominated_label((candidate,), candidate)
    assert result.accepted is False
    assert result.reason == "DUPLICATE"
    assert result.labels == (candidate,)


def test_equivalent_objectives_keep_deterministic_route():
    lexically_later = label(1, 20, 25, route=("source", "z", "destination"))
    lexically_earlier = label(1, 20, 25, route=("source", "a", "destination"))
    result = insert_nondominated_label((lexically_later,), lexically_earlier)
    assert result.accepted
    assert result.pruned_count == 1
    assert result.labels == (lexically_earlier,)


@pytest.mark.parametrize("field,value", [
    ("risk", -1),
    ("distance_m", math.nan),
    ("estimated_time_s", math.inf),
    ("patient_burden", -0.1),
])
def test_label_rejects_invalid_objectives(field, value):
    values = {
        "risk": 1,
        "distance_m": 2,
        "estimated_time_s": 3,
        "patient_burden": 4,
    }
    values[field] = value
    with pytest.raises(ValueError, match="finite and nonnegative"):
        ParetoLabel(state=STATE, route=("destination",), **values)


def test_bounded_frontier_is_deterministic_and_keeps_objective_extremes():
    candidates = (
        label(0, 50, 60, 10, route=("source", "a", "destination")),
        label(5, 30, 35, 5, route=("source", "b", "destination")),
        label(10, 20, 30, 3, route=("source", "c", "destination")),
        label(15, 25, 20, 1, route=("source", "d", "destination")),
    )
    frontier = ParetoFrontier(max_labels_per_state=3)
    for candidate in reversed(candidates):
        frontier.insert(candidate)
    retained = frontier.labels_for(STATE)
    assert len(retained) == 3
    assert candidates[0] in retained  # minimum risk
    assert candidates[2] in retained  # minimum distance
    assert candidates[3] in retained  # minimum time and burden


def test_frontier_keeps_states_separate_and_tracks_statistics():
    first = label(1, 20, 25)
    other_state = ProductState("middle", "GO_TO_2", 1)
    second = ParetoLabel(other_state, 2, 10, 12, 0, ("source", "middle"))
    frontier = ParetoFrontier()
    frontier.insert(first)
    frontier.insert(second)
    frontier.insert(first)
    assert frontier.states() == (STATE, other_state)
    assert frontier.generated_labels == 3
    assert frontier.accepted_labels == 2
    assert frontier.pruned_labels == 1


def test_capacity_pruned_candidate_is_counted_once():
    retained = label(0, 50, 60)
    discarded = label(10, 20, 25)
    frontier = ParetoFrontier(max_labels_per_state=1)
    frontier.insert(retained)
    result = frontier.insert(discarded)
    assert result.accepted is False
    assert result.reason == "CAPACITY_PRUNED"
    assert frontier.generated_labels == 2
    assert frontier.accepted_labels == 1
    assert frontier.pruned_labels == 1


def test_pareto_result_serializes_candidates_and_selection():
    shortest = label(10, 20, 25)
    safer = label(0, 30, 35)
    result = ParetoPlanResult(
        decision=ALLOW,
        reason_code="PARETO_CANDIDATES_READY",
        message="Two non-dominated candidates generated.",
        plan_version=1,
        candidates=(shortest, safer),
        selected_candidate=None,
        expanded_states=7,
        generated_labels=12,
        pruned_labels=4,
        checked_rules=("ROUTE_CONNECTIVITY",),
    )
    payload = result.to_dict()
    assert payload["planner"] == "pareto"
    assert payload["selected_candidate_index"] is None
    assert [
        item["planner_metrics"]["risk_score"] for item in payload["candidates"]
    ] == [0.0, 10.0]
    assert all(item["validation"] is None for item in payload["candidates"])
    assert payload["generated_labels"] == 12


def test_allow_result_requires_a_candidate_and_selection_must_be_from_candidates():
    with pytest.raises(ValueError, match="requires at least one candidate"):
        ParetoPlanResult(ALLOW, "READY", "", 1)
    candidate = label(1, 20, 25)
    outsider = label(2, 19, 24)
    with pytest.raises(ValueError, match="present in candidates"):
        ParetoPlanResult(ALLOW, "READY", "", 1, (candidate,), outsider)
