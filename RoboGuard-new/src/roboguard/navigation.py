"""Task-specific planning and deterministic checks for guided navigation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from roboguard.medical.guard_types import (
    ALLOW,
    PAUSE,
    REJECT,
    ReviewResult,
)
from roboguard.medical.planner_view import PlannerSceneView
from roboguard.medical.scene import MedicalScene, MedicalSceneError
from roboguard.medical.joint_planner import (
    DynamicEventSet,
    HospitalRoutePlanner,
    PlanResult,
)
from roboguard.medical.pareto_planner import ParetoRoutePlanner
from roboguard.medical.pareto_types import (
    ParetoLabel,
    ParetoPlanResult,
    insert_nondominated_label,
)
from roboguard.medical.route_selector import RouteSelector
from roboguard.medical.route_validator import CHECKED_RULES, IndependentRouteValidator
from roboguard.medical.safety_barrier import UnifiedSafetyBarrier
from roboguard.medical.task_contracts import (
    TaskSpec,
    build_route_context,
    build_task_spec,
)
from roboguard.safegate_adapter import (
    REQUIRED_PLANNING_OBLIGATION,
    AuthorizedTask,
)
from roboguard.spine_adapter import SpineAction


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _validator_events(events: DynamicEventSet) -> list[dict[str, object]]:
    """Serialize events with the canonical target key expected by validation."""

    result: list[dict[str, object]] = []
    for event in events.events:
        payload = event.to_dict()
        if len(event.region_ids) == 1:
            payload["region_id"] = event.region_ids[0]
        result.append(payload)
    return result


def _canonical_workflow_trace(trace, planner_view: PlannerSceneView):
    result = []
    for item in trace:
        values = list(item) if isinstance(item, (list, tuple)) else [
            item.rule_id,
            item.action,
            item.from_state,
            item.to_state,
            item.accepted,
        ]
        action = str(values[1])
        for prefix in ("ARRIVE:", "CONFIRM:"):
            if action.startswith(prefix):
                action = prefix + planner_view.resolve_reference(
                    action.removeprefix(prefix)
                )
        values[1] = action
        result.append(values)
    return result


def _validation_request(
    *,
    validator: IndependentRouteValidator,
    route,
    planner_metrics,
    workflow_trace,
    planner_view: PlannerSceneView,
    task: TaskSpec,
    context,
    events: DynamicEventSet,
    plan_version: int,
) -> dict[str, object]:
    return {
        "task": {
            "task_type": task.task_type,
            "source_region_id": task.source_region_id,
            "ordered_destinations": list(task.ordered_destination_ids),
            "confirmation_destinations": list(task.confirmation_destination_ids),
            "require_handoff": task.require_handoff,
            "require_explicit_finish": task.require_explicit_finish,
            "require_workflow_trace": True,
            "permissions": list(
                planner_view.payload["robot"].get("access_permissions", ())
            ),
            # Requester and physical actor are separate principals.  Keep the
            # user role for task policy while carrying only scene-attested
            # robot roles into zone validation.
            "roles": list(
                dict.fromkeys(
                    (
                        task.user_role,
                        *planner_view.payload["robot"].get("roles", ()),
                    )
                )
            ),
            "accessibility_needs": list(task.accessibility_needs),
            "cargo_kinds": list(
                planner_view.payload["task"].get("carrying_kinds", ())
            ),
            "wheelchair": bool(
                set(task.accessibility_needs).intersection(
                    {"WHEELCHAIR_ACCESSIBLE", "STEP_FREE", "ELEVATOR_REQUIRED"}
                )
            ),
            "start_time": context.current_time,
        },
        "route": [planner_view.resolve_reference(reference) for reference in route],
        "workflow_trace": _canonical_workflow_trace(workflow_trace, planner_view),
        "map_id": context.map_id,
        "map_version": validator.map.map_version,
        "context_map_version": context.map_version,
        "plan_version": plan_version,
        "current_plan_version": events.plan_version,
        "events": _validator_events(events),
        "planner_reported": planner_metrics,
    }


def _validate_pareto_candidates(
    pareto: ParetoPlanResult,
    *,
    planner_view: PlannerSceneView,
    task: TaskSpec,
    context,
    events: DynamicEventSet,
    validator: IndependentRouteValidator | None = None,
) -> ParetoPlanResult:
    """Validate, re-score and rebuild the frontier before route selection."""

    if not pareto.allowed:
        return pareto
    validator = validator or IndependentRouteValidator(PROJECT_ROOT)
    valid: tuple[ParetoLabel, ...] = ()
    rejected: list[ParetoLabel] = []
    for candidate in pareto.candidates:
        reported = candidate.reported_metrics()
        validation = validator.validate(
            _validation_request(
                validator=validator,
                route=candidate.route,
                planner_metrics={
                    "distance_m": reported["distance_m"],
                    "risk_score": reported["risk_score"],
                },
                workflow_trace=candidate.workflow_trace,
                planner_view=planner_view,
                task=task,
                context=context,
                events=events,
                plan_version=candidate.state.plan_version,
            )
        )
        metrics = validation.recomputed
        checked = replace(
            candidate,
            risk=float(metrics["risk_score"]),
            distance_m=float(metrics["distance_m"]),
            estimated_time_s=float(metrics["travel_time_s"]),
            risk_components=tuple(dict(metrics["risk_components"]).items()),
            planner_metrics=MappingProxyType(dict(reported)),
            validation=validation,
        )
        if not validation.valid:
            rejected.append(checked)
            continue
        insertion = insert_nondominated_label(
            valid,
            checked,
            max_labels=max(1, len(pareto.candidates)),
        )
        valid = insertion.labels

    checked_rules = tuple(dict.fromkeys(pareto.checked_rules + CHECKED_RULES))
    if not valid:
        return ParetoPlanResult(
            PAUSE,
            "NO_VALIDATED_PARETO_CANDIDATE",
            "Independent validation rejected every Pareto candidate.",
            pareto.plan_version,
            expanded_states=pareto.expanded_states,
            generated_labels=pareto.generated_labels,
            pruned_labels=pareto.pruned_labels,
            checked_rules=checked_rules,
            rejected_candidates=tuple(rejected),
        )
    return ParetoPlanResult(
        ALLOW,
        "PARETO_CANDIDATES_VALIDATED",
        "Pareto candidates passed independent validation and were re-scored.",
        pareto.plan_version,
        valid,
        expanded_states=pareto.expanded_states,
        generated_labels=pareto.generated_labels,
        pruned_labels=pareto.pruned_labels + len(pareto.candidates) - len(valid),
        checked_rules=checked_rules,
        rejected_candidates=tuple(rejected),
    )


def _validate_plan_result(
    planned: PlanResult,
    *,
    planner_view: PlannerSceneView,
    task: TaskSpec,
    context,
    events: DynamicEventSet,
    validator: IndependentRouteValidator | None = None,
) -> PlanResult:
    """Apply the same independent judge to every single-route planner."""

    if not planned.allowed or not planned.route:
        return planned
    validator = validator or IndependentRouteValidator(PROJECT_ROOT)
    validation = validator.validate(
        _validation_request(
            validator=validator,
            route=planned.route,
            planner_metrics={
                "distance_m": planned.distance_m,
                "risk_score": planned.risk_score,
            },
            workflow_trace=planned.workflow_trace,
            planner_view=planner_view,
            task=task,
            context=context,
            events=events,
            plan_version=planned.plan_version,
        )
    )
    checked_rules = tuple(dict.fromkeys(planned.checked_rules + CHECKED_RULES))
    if not validation.valid:
        first = validation.violations[0]
        return replace(
            planned,
            decision=REJECT,
            reason_code="INDEPENDENT_VALIDATION_FAILED",
            message=f"{first.rule_id}: {first.message}",
            checked_rules=checked_rules,
            replan_allowed=True,
        )
    metrics = validation.recomputed
    return replace(
        planned,
        distance_m=float(metrics["distance_m"]),
        estimated_time_s=float(metrics["travel_time_s"]),
        risk_score=float(metrics["risk_score"]),
        risk_components=dict(metrics["risk_components"]),
        checked_rules=checked_rules,
        validation_receipt=validation.receipt,
    )

def build_navigation_view(
    scene: MedicalScene,
    authorized_task: AuthorizedTask,
    *,
    destination_aliases: dict[str, str] | None = None,
) -> PlannerSceneView:
    if authorized_task.action_type != "NAVIGATE_TO_LOCATION":
        raise MedicalSceneError(
            f"unsupported navigation task: {authorized_task.action_type}"
        )
    if authorized_task.context_id != scene.snapshot_id:
        raise MedicalSceneError(
            "SafeGate context_id does not match MedicalScene snapshot_id"
        )

    data = scene.data
    task_spec = build_task_spec(
        scene,
        authorized_task,
        destination_aliases=destination_aliases,
    )
    route_context = build_route_context(scene, authorized_task)
    destination_id = task_spec.destination_region_id
    current_id = task_spec.source_region_id
    aliases = {current_id: "task-source-1"}
    if destination_id != current_id:
        aliases[destination_id] = "task-destination-1"
    index = 1
    for region_id in data["regions"]:
        if region_id not in aliases:
            aliases[region_id] = f"task-region-{index}"
            index += 1

    regions = {}
    for region_id, region in data["regions"].items():
        zone = data["access_zones"][region["access_zone_id"]]
        regions[aliases[region_id]] = {
            "type": region["type"],
            "coords": list(region["coords"]),
            "connections": [aliases[item] for item in region["connections"]],
            "connection_costs": {
                aliases[item]: float(cost)
                for item, cost in region.get("connection_costs", {}).items()
            },
            "connection_risk_rates": {
                aliases[item]: dict(rates)
                for item, rates in region.get("connection_risk_rates", {}).items()
            },
            "area_type": zone["area_type"],
            "required_permissions": list(zone["required_permissions"]),
            "navigation_status": region.get("navigation_status", "OPEN"),
            "navigation_status_valid_until": region.get(
                "navigation_status_valid_until"
            ),
            "prohibited_cargo_kinds": list(
                region.get("prohibited_cargo_kinds", ())
            ),
            "semantic_tags": list(region.get("semantic_tags", ())),
            "route_policy": region.get("route_policy", "ALLOW_TRANSIT"),
            "safe_stop": bool(region.get("safe_stop", False)),
        }

    destination_ref = aliases[destination_id]
    references = {alias: region_id for region_id, alias in aliases.items()}
    carrying_refs = []
    for index, item_id in enumerate(task_spec.carrying_item_ids, 1):
        reference = f"task-carrying-{index}"
        carrying_refs.append(reference)
        references[reference] = item_id
    payload = {
        "schema_version": "0.2",
        "scene_id": scene.scene_id,
        "snapshot_id": scene.snapshot_id,
        "snapshot_hash": scene.snapshot_hash,
        "evaluation_id": authorized_task.evaluation_id,
        "request_id": authorized_task.request_id,
        "task": {
            "type": authorized_task.action_type,
            "source_region_ref": aliases[current_id],
            "destination_region_ref": destination_ref,
            "ordered_destination_region_refs": [
                aliases[item] for item in task_spec.ordered_destination_ids
            ],
            "user_role": task_spec.user_role,
            "accessibility_needs": list(task_spec.accessibility_needs),
            "carrying_refs": carrying_refs,
            "carrying_kinds": [
                data["delivery_items"][item_id]["kind"]
                for item_id in task_spec.carrying_item_ids
                if item_id in data.get("delivery_items", {})
            ],
            "urgency": task_spec.urgency,
            "multi_stage": task_spec.multi_stage,
            "confirmation_destination_region_refs": [
                aliases[item] for item in task_spec.confirmation_destination_ids
            ],
            "require_handoff": task_spec.require_handoff,
            "require_explicit_finish": task_spec.require_explicit_finish,
        },
        "route_context": route_context.to_dict(),
        "regions": regions,
        "robot": {
            "current_region_ref": aliases[current_id],
            "capabilities": list(data["robot_state"]["capabilities"]),
            "access_permissions": list(data["robot_state"]["access_permissions"]),
            "roles": list(data["robot_state"].get("roles", ())),
            "operational_status": data["robot_state"]["operational_status"],
        },
    }
    return PlannerSceneView(payload, MappingProxyType(references))


def navigation_task(view: PlannerSceneView) -> str:
    destinations = tuple(
        view.payload["task"].get(
            "ordered_destination_region_refs",
            (view.payload["task"]["destination_region_ref"],),
        )
    )
    sequence = ", then ".join(destinations)
    return (
        f"Navigate in this exact order: {sequence}. Use only regions present in the graph. "
        "The executable plan must contain one goto for each ordered destination. "
        "Do not pick up, inspect, map, explore, or hand off objects."
    )


def navigation_spine_graph(view: PlannerSceneView) -> dict:
    regions = view.payload["regions"]
    region_nodes = [
        {"name": region_id, "coords": list(region["coords"])}
        for region_id, region in regions.items()
    ]
    seen_edges: set[tuple[str, str]] = set()
    region_edges: list[list[str]] = []
    for region_id, region in regions.items():
        for neighbor in region["connections"]:
            edge = tuple(sorted((region_id, neighbor)))
            if edge not in seen_edges:
                seen_edges.add(edge)
                region_edges.append(list(edge))
    return {
        "objects": [],
        "regions": region_nodes,
        "object_connections": [],
        "region_connections": region_edges,
        "current_location": view.payload["robot"]["current_region_ref"],
        "task_context": {
            "type": "NAVIGATE_TO_LOCATION",
            "destination_region_ref": view.payload["task"][
                "destination_region_ref"
            ],
            "ordered_destination_region_refs": list(
                view.payload["task"].get(
                    "ordered_destination_region_refs",
                    (view.payload["task"]["destination_region_ref"],),
                )
            ),
        },
    }


def task_spec_from_view(view: PlannerSceneView) -> TaskSpec:
    """Recover the validated task contract without reparsing user aliases."""

    task = view.payload["task"]
    destination_refs = tuple(
        task.get(
            "ordered_destination_region_refs",
            (task["destination_region_ref"],),
        )
    )
    confirmation_refs = tuple(
        task.get("confirmation_destination_region_refs", ())
    )
    return TaskSpec(
        task_type=task["type"],
        source_region_id=view.resolve_reference(task["source_region_ref"]),
        ordered_destination_ids=tuple(
            view.resolve_reference(item) for item in destination_refs
        ),
        user_role=task.get("user_role", "GENERAL_PUBLIC"),
        accessibility_needs=tuple(task.get("accessibility_needs", ())),
        carrying_item_ids=tuple(
            view.resolve_reference(item) for item in task.get("carrying_refs", ())
        ),
        urgency=task.get("urgency", "ROUTINE"),
        confirmation_destination_ids=tuple(
            view.resolve_reference(item) for item in confirmation_refs
        ),
        require_handoff=bool(task.get("require_handoff", False)),
        require_explicit_finish=bool(task.get("require_explicit_finish", False)),
    )


class NavigationExecutionGuard:
    """Fail-closed plan-shape check for an admitted navigation mission."""

    def __init__(self, *, temporal_guard=None, planner="dijkstra", events=None):
        self.temporal_guard = temporal_guard
        self.planner = planner
        self.events = events or DynamicEventSet.empty()

    def _result(
        self,
        decision,
        reason,
        message,
        checked,
        *,
        routes=(),
        violated_rules=(),
        required_evidence=(),
        matched_rules=(),
        checked_ltl=(),
        failed_route_node=None,
        temporal_trace=(),
        planning_result=None,
    ):
        return ReviewResult(
            decision=decision,
            reason_code=reason,
            message=message,
            checked_routes=tuple(routes),
            violated_rules=tuple(violated_rules),
            required_evidence=tuple(required_evidence),
            replan_allowed=decision == REJECT,
            matched_natural_language_rules=tuple(matched_rules),
            checked_ltl=tuple(checked_ltl),
            failed_route_node=failed_route_node,
            temporal_trace=tuple(temporal_trace),
            planning_result=planning_result,
        )

    def _review_navigation(
        self,
        *,
        scene,
        planner_view,
        actions,
        task_spec=None,
        route_context=None,
    ):
        temporal_guard = self.temporal_guard
        if temporal_guard is None:
            from roboguard.medical.temporal_guard import MedicalTemporalGuard
            temporal_guard = MedicalTemporalGuard()
        destinations = tuple(
            planner_view.payload["task"].get(
                "ordered_destination_region_refs",
                (planner_view.payload["task"]["destination_region_ref"],),
            )
        )
        if self.planner == "pareto":
            if task_spec is None or route_context is None:
                raise MedicalSceneError(
                    "pareto planning requires TaskSpec and RouteContext"
                )
            candidates = ParetoRoutePlanner().plan(
                scene=scene,
                planner_view=planner_view,
                events=self.events,
            )
            candidates = _validate_pareto_candidates(
                candidates,
                planner_view=planner_view,
                task=task_spec,
                context=route_context,
                events=self.events,
            )
            selection = RouteSelector().select(
                candidates,
                task=task_spec,
                context=route_context,
                require_validation=True,
            )
            planned = selection.to_plan_result(
                checked_rules=candidates.checked_rules
            )
            barrier = UnifiedSafetyBarrier().review(
                source_type="INITIAL_PARETO",
                proposed=planned,
                scene=scene,
                planner_view=planner_view,
                task=task_spec,
                context=route_context,
                events=self.events,
            )
            route_result = barrier.to_step_result()
        else:
            if task_spec is None or route_context is None:
                raise MedicalSceneError(
                    "route planning requires TaskSpec and RouteContext"
                )
            planner = HospitalRoutePlanner(self.planner)
            planned = planner.plan(
                scene=scene,
                planner_view=planner_view,
                events=self.events,
            )
            planned = _validate_plan_result(
                planned,
                planner_view=planner_view,
                task=task_spec,
                context=route_context,
                events=self.events,
            )
            source_types = {
                "dijkstra": "DIJKSTRA_BASELINE",
                "astar": "ASTAR_BASELINE",
                "generate_then_check": "GENERATE_THEN_CHECK",
                "joint": "JOINT_ASTAR",
            }
            barrier = UnifiedSafetyBarrier().review(
                source_type=source_types[self.planner],
                proposed=planned,
                scene=scene,
                planner_view=planner_view,
                task=task_spec,
                context=route_context,
                events=self.events,
            )
            route_result = barrier.to_step_result()
        if route_result.decision != ALLOW or route_result.route is None:
            return route_result, planned
        supplemental = temporal_guard.review(
            planner_view=planner_view,
            actions=actions,
            checked_route=route_result.route,
        )
        if supplemental.decision != ALLOW:
            return supplemental, planned
        return type(route_result).allow(
            route=route_result.route,
            matched_rules=supplemental.matched_rules,
            checked_ltl=tuple(dict.fromkeys(route_result.checked_ltl + supplemental.checked_ltl)),
            temporal_trace=route_result.temporal_trace + supplemental.temporal_trace,
        ), planned

    def _review_replan_navigation(
        self,
        *,
        scene,
        planner_view,
        actions,
        previous_plan,
        task_spec,
        route_context,
        changed_fields=(),
    ):
        """Repair a previously approved plan and pass it through the same barrier."""

        from roboguard.medical.dstar_lite import IncrementalNavigationReplanner
        from roboguard.medical.temporal_guard import MedicalTemporalGuard

        replanned = IncrementalNavigationReplanner().replan(
            scene=scene,
            planner_view=planner_view,
            previous=previous_plan,
            task=task_spec,
            context=route_context,
            events=self.events,
            changed_fields=changed_fields,
        )
        source_type = (
            "FULL_PARETO_REPLAN"
            if replanned.mode == "FULL_PARETO_REPLAN"
            else "DSTAR_LITE_REPAIR"
        )
        barrier = UnifiedSafetyBarrier().review(
            source_type=source_type,
            proposed=replanned.result,
            scene=scene,
            planner_view=planner_view,
            task=task_spec,
            context=route_context,
            events=self.events,
        )
        route_result = barrier.to_step_result()
        if route_result.decision != ALLOW or route_result.route is None:
            return route_result, replanned.result

        temporal_guard = self.temporal_guard or MedicalTemporalGuard()
        supplemental = temporal_guard.review(
            planner_view=planner_view,
            actions=actions,
            checked_route=route_result.route,
        )
        if supplemental.decision != ALLOW:
            return supplemental, replanned.result
        return type(route_result).allow(
            route=route_result.route,
            matched_rules=supplemental.matched_rules,
            checked_ltl=tuple(
                dict.fromkeys(route_result.checked_ltl + supplemental.checked_ltl)
            ),
            temporal_trace=route_result.temporal_trace + supplemental.temporal_trace,
        ), replanned.result

    def review_plan(
        self,
        *,
        scene: MedicalScene,
        authorized_task: AuthorizedTask,
        planner_view: PlannerSceneView,
        actions: list[SpineAction],
        previous_plan: PlanResult | None = None,
        changed_fields=(),
    ) -> ReviewResult:
        checked: list[str] = []
        if authorized_task.action_type != "NAVIGATE_TO_LOCATION":
            return self._result(
                REJECT, "UNSUPPORTED_NAVIGATION_ACTION", "Not a navigation task.", checked
            )
        if authorized_task.context_id != scene.snapshot_id:
            return self._result(
                REJECT,
                "SNAPSHOT_BINDING_MISMATCH",
                "The authorization is not bound to this scene snapshot.",
                checked,
            )
        if REQUIRED_PLANNING_OBLIGATION not in authorized_task.obligations:
            return self._result(
                REJECT,
                "MISSING_EXECUTION_CHECK_OBLIGATION",
                "SafeGate did not require the execution check.",
                checked,
            )
        if planner_view.payload["snapshot_hash"] != scene.snapshot_hash:
            return self._result(
                REJECT,
                "SCENE_HASH_MISMATCH",
                "The planner view was not generated from this scene snapshot.",
                checked,
            )

        robot = planner_view.payload["robot"]
        status = robot["operational_status"]
        if status != "READY":
            decision = REJECT if status == "UNAVAILABLE" else PAUSE
            return self._result(
                decision,
                "ROBOT_NOT_READY",
                f"The robot operational status is {status}.",
                checked,
            )
        if "GUIDED_NAVIGATION" not in set(robot["capabilities"]):
            return self._result(
                REJECT,
                "GUIDED_NAVIGATION_CAPABILITY_MISSING",
                "The robot lacks guided-navigation capability.",
                checked,
            )
        active_request = scene.data["robot_state"].get("active_task_request_id")
        if active_request and active_request != authorized_task.request_id:
            return self._result(
                PAUSE,
                "ROBOT_TASK_CONFLICT",
                "The robot is executing another mutually exclusive task.",
                checked,
            )

        destinations = tuple(
            planner_view.payload["task"].get(
                "ordered_destination_region_refs",
                (planner_view.payload["task"]["destination_region_ref"],),
            )
        )
        if len(destinations) > 1 and not actions:
            return self._result(
                PAUSE,
                "MULTI_STAGE_WORKFLOW_REVIEW_REQUIRED",
                "The multi-stage task is valid, but its ordered goto actions are missing.",
                checked,
            )
        goto_count = 0
        arrived = False
        for action in actions:
            checked.append(action.display())
            if action.name == "request_human_review":
                return self._result(PAUSE, "HUMAN_REVIEW_REQUESTED", str(action.args[0]), checked)
            if action.name in {"clarify", "replan"}:
                return self._result(
                    PAUSE, "PLANNER_INPUT_REQUIRED", f"The plan stopped at {action.name}.", checked
                )
            if action.name == "abort_task":
                return self._result(REJECT, "TASK_ABORTED", str(action.args[0]), checked)
            if action.name == "goto":
                if goto_count >= len(destinations) or action.target != destinations[goto_count]:
                    expected = destinations[goto_count] if goto_count < len(destinations) else None
                    return self._result(
                        REJECT,
                        "UNAUTHORIZED_NAVIGATION_TARGET",
                        f"Navigation target must follow the authorized order; expected {expected}.",
                        checked,
                    )
                goto_count += 1
                arrived = goto_count == len(destinations)
                continue
            if action.name == "answer" and arrived:
                continue
            return self._result(
                REJECT,
                "UNSUPPORTED_NAVIGATION_PLAN_ACTION",
                f"Action {action.name} is not allowed in a navigation plan.",
                checked,
            )
        if goto_count != len(destinations):
            return self._result(
                REJECT,
                "INCOMPLETE_OR_DUPLICATE_NAVIGATION",
                "The plan must contain exactly one goto for every ordered destination.",
                checked,
            )
        task_spec = task_spec_from_view(planner_view)
        route_context = build_route_context(
            scene,
            authorized_task,
            dynamic_events=self.events.to_dict()["events"],
        )
        if previous_plan is None:
            route_result, planning_result = self._review_navigation(
                scene=scene,
                planner_view=planner_view,
                actions=actions,
                task_spec=task_spec,
                route_context=route_context,
            )
        else:
            route_result, planning_result = self._review_replan_navigation(
                scene=scene,
                planner_view=planner_view,
                actions=actions,
                previous_plan=previous_plan,
                task_spec=task_spec,
                route_context=route_context,
                changed_fields=changed_fields,
            )
        if route_result.decision != ALLOW:
            return self._result(
                route_result.decision,
                route_result.reason_code,
                route_result.message,
                checked,
                violated_rules=route_result.violated_rules,
                required_evidence=route_result.required_evidence,
                matched_rules=route_result.matched_rules,
                checked_ltl=route_result.checked_ltl,
                failed_route_node=route_result.failed_route_node,
                temporal_trace=route_result.temporal_trace,
                planning_result=planning_result,
            )
        routes = (route_result.route,) if route_result.route is not None else ()
        return self._result(
            ALLOW,
            "NAVIGATION_PLAN_ALLOWED",
            "Destination binding, route permissions, and action shape passed.",
            checked,
            routes=routes,
            matched_rules=route_result.matched_rules,
            checked_ltl=route_result.checked_ltl,
            temporal_trace=route_result.temporal_trace,
            planning_result=planning_result,
        )
