#!/usr/bin/env python3
"""Run three deterministic end-to-end RoboGuard planning demonstrations."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType

from roboguard.medical.dstar_lite import IncrementalNavigationReplanner
from roboguard.medical.joint_planner import DynamicEventSet
from roboguard.medical.pareto_planner import ParetoRoutePlanner
from roboguard.medical.planner_view import PlannerSceneView
from roboguard.medical.route_selector import RouteSelector
from roboguard.medical.route_validator import IndependentRouteValidator
from roboguard.medical.safety_barrier import UnifiedSafetyBarrier
from roboguard.medical.task_contracts import RouteContext, TaskSpec
from roboguard.medical.validation_map import ValidationMap
from roboguard.navigation import _validate_pareto_candidates


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class DemoScene:
    snapshot_id: str = "demo-snapshot-1"
    snapshot_hash: str = "demo-hash-1"
    data = {
        "current_time": "2026-10-20T08:00:00Z",
        "facility": {"emergency_status": "NONE", "hazards": []},
        "delivery_items": {},
    }


def region(x, y, connections, costs, *, tags=()):
    return {
        "type": "CORRIDOR",
        "coords": [x, y, 0],
        "connections": list(connections),
        "connection_costs": dict(costs),
        "area_type": "PUBLIC",
        "required_permissions": [],
        "navigation_status": "OPEN",
        "navigation_status_valid_until": None,
        "prohibited_cargo_kinds": [],
        "semantic_tags": list(tags),
        "route_policy": "ALLOW_TRANSIT",
        "safe_stop": True,
    }


def view(
    graph,
    *,
    destinations=("d",),
    confirmations=(),
    require_handoff=False,
    require_finish=False,
):
    references = {node: f"real-{node}" for node in graph}
    return PlannerSceneView(
        {
            "schema_version": "0.2",
            "scene_id": "full-pipeline-demo",
            "snapshot_id": "demo-snapshot-1",
            "snapshot_hash": "demo-hash-1",
            "evaluation_id": "demo-authorization-1",
            "request_id": "demo-request-1",
            "route_context": {
                "authorization_id": "demo-authorization-1",
                "request_id": "demo-request-1",
                "map_id": "demo-map",
                "map_version": 1,
            },
            "task": {
                "type": "NAVIGATE_TO_LOCATION",
                "source_region_ref": "s",
                "destination_region_ref": destinations[-1],
                "ordered_destination_region_refs": list(destinations),
                "user_role": "PATIENT",
                "accessibility_needs": [],
                "carrying_refs": [],
                "urgency": "ROUTINE",
                "confirmation_destination_region_refs": list(confirmations),
                "require_handoff": require_handoff,
                "require_explicit_finish": require_finish,
            },
            "regions": graph,
            "robot": {
                "current_region_ref": "s",
                "capabilities": ["GUIDED_NAVIGATION"],
                "access_permissions": [],
                "operational_status": "READY",
            },
        },
        MappingProxyType(references),
    )


def task(
    *,
    destinations=("real-d",),
    confirmations=(),
    require_handoff=False,
    require_finish=False,
):
    return TaskSpec(
        "NAVIGATE_TO_LOCATION",
        "real-s",
        tuple(destinations),
        user_role="PATIENT",
        confirmation_destination_ids=tuple(confirmations),
        require_handoff=require_handoff,
        require_explicit_finish=require_finish,
    )


def context(events=None):
    overlay = events or DynamicEventSet.empty()
    return RouteContext(
        "demo-authorization-1",
        "demo-request-1",
        "ALLOW_TO_ENTER_PLANNING",
        "demo-snapshot-1",
        "demo-hash-1",
        "demo-map",
        1,
        "2026-10-20T08:00:00Z",
        tuple(event.to_dict() for event in overlay.events),
    )


def demo_validator(graph) -> IndependentRouteValidator:
    """Create a separate immutable validation view for the synthetic demo."""

    nodes = {}
    edges = {}
    directed = {}
    semantics = {}
    emergency_edges = []
    for node_id, value in graph.items():
        real_id = f"real-{node_id}"
        nodes[real_id] = {
            "id": real_id,
            "access_zone": "public",
            "navigation_status": "open",
            "region_type": value.get("type", "CORRIDOR"),
        }
        semantics[real_id] = {
            "semantic_tags": list(value.get("semantic_tags", ())),
            "route_policy": value.get("route_policy", "ALLOW_TRANSIT"),
        }
    for source, value in graph.items():
        for target, distance in value.get("connection_costs", {}).items():
            edge_id = f"demo-edge-{source}-to-{target}"
            edge = {
                "id": edge_id,
                "from": f"real-{source}",
                "to": f"real-{target}",
                "distance_m": float(distance),
                "bidirectional": False,
                "access_policy": "public",
            }
            edges[edge_id] = edge
            directed[(edge["from"], edge["to"])] = edge
            if "EMERGENCY_CORRIDOR" in graph[target].get("semantic_tags", ()):
                emergency_edges.append(edge_id)
    rules = json.loads(
        (PROJECT_ROOT / "config/route_validation_rules.json").read_text(
            encoding="utf-8"
        )
    )
    validation_map = ValidationMap(
        project_root=PROJECT_ROOT,
        map_version="demo.v1",
        map_id="demo-map",
        nodes=nodes,
        edges=edges,
        directed_edges=directed,
        one_way_reverse={},
        zone_permissions={"public": ()},
        zone_roles={"public": ()},
        route_semantics=semantics,
        risk_zones=({
            "id": "demo-emergency-corridor",
            "type": "EMERGENCY_CORRIDOR",
            "affected_edges": emergency_edges,
        },),
        rules=rules,
    )
    return IndependentRouteValidator(PROJECT_ROOT, validation_map=validation_map)


def initial_plan(scene, planner_view, task_spec, events=None, *, validator=None):
    overlay = events or DynamicEventSet.empty()
    candidates = ParetoRoutePlanner().plan(
        scene=scene, planner_view=planner_view, events=overlay
    )
    candidates = _validate_pareto_candidates(
        candidates,
        planner_view=planner_view,
        task=task_spec,
        context=context(overlay),
        events=overlay,
        validator=validator,
    )
    selection = RouteSelector().select(
        candidates,
        task=task_spec,
        context=context(overlay),
        require_validation=True,
    )
    proposed = selection.to_plan_result(checked_rules=candidates.checked_rules)
    barrier = UnifiedSafetyBarrier().review(
        source_type="INITIAL_PARETO",
        proposed=proposed,
        scene=scene,
        planner_view=planner_view,
        task=task_spec,
        context=context(overlay),
        events=overlay,
    )
    return candidates, proposed, barrier


def run_demo(output_dir: Path) -> dict:
    scene = DemoScene()
    tradeoff_graph = {
        "s": region(0, 0, ["risky", "safe"], {"risky": 1, "safe": 1.2}),
        "risky": region(
            1, 0, ["s", "registration"], {"s": 1, "registration": 1},
            tags=("EMERGENCY_CORRIDOR",),
        ),
        "safe": region(
            0, 2, ["s", "registration"], {"s": 1.2, "registration": 1.2}
        ),
        "registration": region(
            2, 0, ["risky", "safe", "d"], {"risky": 1, "safe": 1.2, "d": 1}
        ),
        "d": region(3, 0, ["registration"], {"registration": 1}),
    }
    multistage_task = task(
        destinations=("real-registration", "real-d"),
        confirmations=("real-registration",),
        require_handoff=True,
        require_finish=True,
    )
    tradeoff_view = view(
        tradeoff_graph,
        destinations=("registration", "d"),
        confirmations=("registration",),
        require_handoff=True,
        require_finish=True,
    )
    candidates, proposed, initial_barrier = initial_plan(
        scene,
        tradeoff_view,
        multistage_task,
        validator=demo_validator(tradeoff_graph),
    )

    repair_graph = {
        "s": region(0, 0, ["a"], {"a": 1}),
        "a": region(1, 0, ["s", "x", "b"], {"s": 1, "x": 1, "b": 0.7}),
        "x": region(2, 0, ["a", "d"], {"a": 1, "d": 1}),
        "b": region(2, 1, ["a", "d"], {"a": 0.7, "d": 1.7}),
        "d": region(3, 0, ["x", "b"], {"x": 1, "b": 1.7}),
    }
    repair_view = view(repair_graph)
    repair_task = task()
    _, repair_previous, repair_initial_barrier = initial_plan(
        scene,
        repair_view,
        repair_task,
        validator=demo_validator(repair_graph),
    )
    local_events = DynamicEventSet.from_mapping(
        {
            "plan_version": 2,
            "events": [{"type": "NODE_BLOCKED", "region_id": "real-x"}],
        }
    )
    repaired = IncrementalNavigationReplanner(
        validator=demo_validator(repair_graph)
    ).replan(
        scene=scene,
        planner_view=repair_view,
        previous=repair_previous,
        task=repair_task,
        context=context(local_events),
        events=local_events,
    )
    repair_barrier = UnifiedSafetyBarrier().review(
        source_type="DSTAR_LITE_REPAIR",
        proposed=repaired.result,
        scene=scene,
        planner_view=repair_view,
        task=repair_task,
        context=context(local_events),
        events=local_events,
    )
    full_for_comparison = ParetoRoutePlanner().plan(
        scene=scene, planner_view=repair_view, events=local_events
    )

    major_events = DynamicEventSet.from_mapping(
        {
            "plan_version": 2,
            "events": [{
                "type": "PATIENT_STATUS_CHANGED",
                "region_id": "real-s",
                "reason": "patient mobility status updated",
            }],
        }
    )
    major = IncrementalNavigationReplanner(
        validator=demo_validator(tradeoff_graph)
    ).replan(
        scene=scene,
        planner_view=tradeoff_view,
        previous=proposed,
        task=multistage_task,
        context=context(major_events),
        events=major_events,
    )
    major_barrier = UnifiedSafetyBarrier().review(
        source_type="FULL_PARETO_REPLAN",
        proposed=major.result,
        scene=scene,
        planner_view=tradeoff_view,
        task=multistage_task,
        context=context(major_events),
        events=major_events,
    )

    payload = {
        "schema_version": "1.0",
        "all_scenarios_allowed": all(
            item.allowed
            for item in (initial_barrier, repair_initial_barrier, repair_barrier, major_barrier)
        ),
        "scenarios": {
            "pareto_task_selection": {
                "candidate_count": len(candidates.candidates),
                "candidates": [item.to_dict() for item in candidates.candidates],
                "selected_route": list(proposed.route),
                "selection_profile": proposed.selection_profile,
                "workflow_actions": [
                    item.action for item in proposed.workflow_trace
                ],
                "barrier_decision": initial_barrier.decision,
                "certificate": dict(initial_barrier.certificate),
            },
            "local_dstar_repair": {
                "old_route": list(repair_previous.route),
                "blocked_node": "x",
                "new_route": list(repaired.result.route),
                "preserved_prefix": list(repaired.preserved_prefix),
                "mode": repaired.mode,
                "dstar_repair_expanded_states": repaired.dstar_repair_expanded_states,
                "full_pareto_expanded_states": full_for_comparison.expanded_states,
                "barrier_decision": repair_barrier.decision,
                "certificate": dict(repair_barrier.certificate),
            },
            "major_change_full_replan": {
                "change_classification": major.classification.action,
                "mode": major.mode,
                "route": list(major.result.route),
                "barrier_decision": major_barrier.decision,
                "certificate": dict(major_barrier.certificate),
            },
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "full_pipeline_results.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = [
        "# RoboGuard full pipeline verification",
        "",
        f"- All scenarios allowed: `{payload['all_scenarios_allowed']}`",
        f"- Pareto candidates: `{len(candidates.candidates)}`",
        f"- Selected initial route: `{' -> '.join(proposed.route)}`",
        "- Workflow: `ARRIVE registration -> CONFIRM registration -> ARRIVE destination -> HANDOFF -> FINISH`",
        f"- D* Lite repaired route: `{' -> '.join(repaired.result.route)}`",
        f"- Preserved prefix: `{' -> '.join(repaired.preserved_prefix)}`",
        f"- D* Lite repair expansions: `{repaired.dstar_repair_expanded_states}`",
        f"- Full Pareto comparison expansions: `{full_for_comparison.expanded_states}`",
        f"- Major-change mode: `{major.mode}`",
        "- Every output route received a unified safety-barrier certificate.",
        "",
    ]
    (output_dir / "full_pipeline_summary.md").write_text(
        "\n".join(summary), encoding="utf-8"
    )
    return payload


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "full_pipeline_demo",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = run_demo(args.output_dir)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["all_scenarios_allowed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
