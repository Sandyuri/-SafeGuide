"""End-to-end verification for all three route production paths."""

from scripts.run_full_pipeline_demo import run_demo


def test_full_pipeline_demo_generates_three_certified_results(tmp_path):
    payload = run_demo(tmp_path)
    assert payload["all_scenarios_allowed"] is True
    scenarios = payload["scenarios"]
    assert set(scenarios) == {
        "pareto_task_selection",
        "local_dstar_repair",
        "major_change_full_replan",
    }
    assert scenarios["pareto_task_selection"]["candidate_count"] == 2
    assert scenarios["pareto_task_selection"]["selected_route"] == [
        "s", "safe", "registration", "d"
    ]
    assert scenarios["pareto_task_selection"]["workflow_actions"] == [
        "ARRIVE:registration",
        "CONFIRM:registration",
        "ARRIVE:d",
        "HANDOFF",
        "FINISH",
    ]
    local = scenarios["local_dstar_repair"]
    assert local["mode"] == "DSTAR_LITE_REPAIR"
    assert local["new_route"] == ["s", "a", "b", "d"]
    assert local["preserved_prefix"] == ["s", "a"]
    assert local["dstar_repair_expanded_states"] < local["full_pareto_expanded_states"]
    assert scenarios["major_change_full_replan"]["mode"] == "FULL_PARETO_REPLAN"
    for scenario in scenarios.values():
        assert scenario["barrier_decision"] == "ALLOW"
        assert scenario["certificate"]["certificate_id"].startswith("sha256:")
