"""Multi-label Pareto search over hospital map and workflow states."""

from __future__ import annotations

from heapq import heappop, heappush
from typing import Any

from roboguard.medical.guard_types import ALLOW, PAUSE, REJECT
from roboguard.medical.joint_planner import DynamicEventSet
from roboguard.medical.pareto_types import (
    DEFAULT_MAX_LABELS_PER_STATE,
    ParetoFrontier,
    ParetoLabel,
    ParetoPlanResult,
    ProductState,
    insert_nondominated_label,
)
from roboguard.medical.planning_constraints import PlanningConstraintEvaluator
from roboguard.medical.planning_cost_model import NOMINAL_SPEED_MPS, PlanningCostModel
from roboguard.medical.planner_view import PlannerSceneView
from roboguard.medical.scene import MedicalScene, MedicalSceneError


DEFAULT_MAX_FINAL_CANDIDATES = 16


class ParetoRoutePlanner:
    """Generate safe, mutually non-dominated route candidates.

    Selection is deliberately left to the task-profile selector implemented in
    the next stage. Workflow-only actions are zero-cost product-graph edges;
    map movement is permitted only while the workflow is in ``GO_TO_n``.
    """

    planner = "pareto"

    def __init__(
        self,
        *,
        max_labels_per_state: int = DEFAULT_MAX_LABELS_PER_STATE,
        max_final_candidates: int = DEFAULT_MAX_FINAL_CANDIDATES,
        constraints: PlanningConstraintEvaluator | None = None,
        cost_model: PlanningCostModel | None = None,
    ) -> None:
        if isinstance(max_final_candidates, bool) or not isinstance(max_final_candidates, int):
            raise ValueError("max_final_candidates must be a positive integer")
        if max_final_candidates < 1:
            raise ValueError("max_final_candidates must be a positive integer")
        self.max_labels_per_state = max_labels_per_state
        self.max_final_candidates = max_final_candidates
        self._constraints = constraints or PlanningConstraintEvaluator()
        self._cost_model = cost_model or PlanningCostModel()

    def plan(
        self,
        *,
        scene: MedicalScene,
        planner_view: PlannerSceneView,
        events: DynamicEventSet | None = None,
    ) -> ParetoPlanResult:
        overlay = events or DynamicEventSet.empty()
        facility = scene.data.get("facility", {})
        if facility.get("emergency_status", "NONE") != "NONE":
            return self._empty_result(
                PAUSE,
                "FACILITY_EMERGENCY_ACTIVE",
                "Navigation is paused while the facility emergency state is active or unknown.",
                overlay,
            )
        if facility.get("hazards"):
            return self._empty_result(
                PAUSE,
                "FACILITY_HAZARD_REVIEW_REQUIRED",
                "A reported facility hazard requires route review.",
                overlay,
            )

        task = planner_view.payload["task"]
        source = planner_view.payload["robot"]["current_region_ref"]
        destinations = tuple(
            task.get(
                "ordered_destination_region_refs",
                (task["destination_region_ref"],),
            )
        )
        confirmations = frozenset(
            task.get("confirmation_destination_region_refs", ())
        )
        unknown = sorted(confirmations - set(destinations))
        if unknown:
            raise MedicalSceneError(
                "workflow confirmation destination is not in ordered destinations: "
                + ", ".join(unknown)
            )
        require_handoff = bool(task.get("require_handoff", False))
        require_finish = bool(task.get("require_explicit_finish", False))
        regions = planner_view.payload["regions"]
        version = overlay.plan_version
        start_state = ProductState(source, "GO_TO_1", version)
        start = ParetoLabel(start_state, 0, 0, 0, 0, (source,))
        frontier = ParetoFrontier(max_labels_per_state=self.max_labels_per_state)
        frontier.insert(start)
        pending: list[tuple[tuple[object, ...], int, ParetoLabel]] = []
        serial = 0
        heappush(pending, (start.deterministic_key, serial, start))
        completed: tuple[ParetoLabel, ...] = ()
        expanded = 0
        paused_violation = None

        while pending:
            _, _, label = heappop(pending)
            if label not in frontier.labels_for(label.state):
                continue
            expanded += 1
            state = label.state
            if state.workflow_state == "COMPLETE":
                insertion = insert_nondominated_label(
                    completed,
                    label,
                    max_labels=self.max_final_candidates,
                    epsilon=frontier.epsilon,
                )
                completed = insertion.labels
                continue

            workflow_successor = self._workflow_successor(
                label,
                destinations=destinations,
                confirmations=confirmations,
                require_handoff=require_handoff,
                require_finish=require_finish,
            )
            if workflow_successor is not None:
                serial = self._enqueue(frontier, pending, workflow_successor, serial)
                continue

            stage = self._stage(state.workflow_state, len(destinations))
            expected = destinations[stage]
            for neighbor in sorted(regions[state.map_node]["connections"]):
                if neighbor not in regions:
                    continue
                edge = regions[state.map_node].get("connection_costs", {}).get(neighbor)
                if not self._constraints.valid_edge(edge):
                    continue
                edge_distance = self._cost_model.edge_distance(
                    planner_view,
                    state.map_node,
                    neighbor,
                    float(edge),
                    overlay,
                )
                violation = self._constraints.node_violation(
                    scene,
                    planner_view,
                    neighbor,
                    overlay,
                    expected,
                    arrival_time_s=(
                        label.estimated_time_s + edge_distance / NOMINAL_SPEED_MPS
                    ),
                )
                if violation:
                    if violation[0] == PAUSE and paused_violation is None:
                        paused_violation = violation
                    continue
                successor = self._move_label(
                    label,
                    neighbor=neighbor,
                    edge_distance=edge_distance,
                    region=regions[neighbor],
                    edge_risk_rates=regions[state.map_node].get(
                        "connection_risk_rates", {}
                    ).get(neighbor),
                    view=planner_view,
                    overlay=overlay,
                    current_time=scene.data["current_time"],
                )
                serial = self._enqueue(frontier, pending, successor, serial)

        if completed:
            return ParetoPlanResult(
                ALLOW,
                "PARETO_CANDIDATES_READY",
                "Safe non-dominated route candidates generated; task selection is pending.",
                version,
                completed,
                selected_candidate=None,
                expanded_states=expanded,
                generated_labels=frontier.generated_labels,
                pruned_labels=frontier.pruned_labels,
                checked_rules=self._constraints.checked_rules(),
            )
        if paused_violation:
            decision, reason, message, _ = paused_violation
            return self._empty_result(
                decision,
                reason,
                message,
                overlay,
                expanded=expanded,
                frontier=frontier,
            )
        return self._empty_result(
            REJECT,
            "NO_ALLOWED_ROUTE",
            "No route satisfies all spatial and workflow constraints.",
            overlay,
            expanded=expanded,
            frontier=frontier,
        )

    @staticmethod
    def _stage(workflow_state: str, destination_count: int) -> int:
        if not workflow_state.startswith("GO_TO_"):
            raise MedicalSceneError(f"workflow state cannot move on map: {workflow_state}")
        try:
            stage = int(workflow_state.removeprefix("GO_TO_")) - 1
        except ValueError as error:
            raise MedicalSceneError(f"invalid workflow state: {workflow_state}") from error
        if stage < 0 or stage >= destination_count:
            raise MedicalSceneError(f"invalid workflow stage: {workflow_state}")
        return stage

    @staticmethod
    def _next_state(
        completed_stage: int,
        destination_count: int,
        *,
        require_handoff: bool,
        require_finish: bool,
    ) -> str:
        if completed_stage < destination_count:
            return f"GO_TO_{completed_stage + 1}"
        if require_handoff:
            return "WAIT_HANDOFF"
        if require_finish:
            return "READY_TO_COMPLETE"
        return "COMPLETE"

    def _workflow_successor(
        self,
        label: ParetoLabel,
        *,
        destinations: tuple[str, ...],
        confirmations: frozenset[str],
        require_handoff: bool,
        require_finish: bool,
    ) -> ParetoLabel | None:
        current = label.state.workflow_state
        target_state = None
        transition = None
        if current.startswith("GO_TO_"):
            stage = self._stage(current, len(destinations))
            destination = destinations[stage]
            if label.state.map_node != destination:
                return None
            target_state = (
                f"WAIT_CONFIRM_{stage + 1}"
                if destination in confirmations
                else self._next_state(
                    stage + 1,
                    len(destinations),
                    require_handoff=require_handoff,
                    require_finish=require_finish,
                )
            )
            transition = ("WF-001", f"ARRIVE:{destination}", current, target_state, True)
        elif current.startswith("WAIT_CONFIRM_"):
            try:
                stage = int(current.removeprefix("WAIT_CONFIRM_")) - 1
            except ValueError as error:
                raise MedicalSceneError(f"invalid workflow state: {current}") from error
            destination = destinations[stage]
            target_state = self._next_state(
                stage + 1,
                len(destinations),
                require_handoff=require_handoff,
                require_finish=require_finish,
            )
            transition = ("WF-002", f"CONFIRM:{destination}", current, target_state, True)
        elif current == "WAIT_HANDOFF":
            target_state = "READY_TO_COMPLETE" if require_finish else "COMPLETE"
            transition = ("WF-005", "HANDOFF", current, target_state, True)
        elif current == "READY_TO_COMPLETE":
            target_state = "COMPLETE"
            transition = ("WF-006", "FINISH", current, target_state, True)
        if target_state is None or transition is None:
            return None
        return ParetoLabel(
            ProductState(label.state.map_node, target_state, label.state.plan_version),
            label.risk,
            label.distance_m,
            label.estimated_time_s,
            label.patient_burden,
            label.route,
            label.risk_components,
            label.workflow_trace + (transition,),
        )

    def _move_label(
        self,
        label: ParetoLabel,
        *,
        neighbor: str,
        edge_distance: float,
        region: dict[str, Any],
        edge_risk_rates: dict[str, float] | None,
        view: PlannerSceneView,
        overlay: DynamicEventSet,
        current_time: str,
    ) -> ParetoLabel:
        transition = self._cost_model.transition_cost(
            view=view,
            node=neighbor,
            region=region,
            edge_distance=edge_distance,
            edge_risk_rates=edge_risk_rates,
            overlay=overlay,
            current_time=current_time,
            elapsed_before_s=label.estimated_time_s,
        )
        components = dict(label.risk_components)
        for key, value in transition.risk_components:
            components[key] = components.get(key, 0.0) + value
        return ParetoLabel(
            ProductState(
                neighbor,
                label.state.workflow_state,
                label.state.plan_version,
            ),
            sum(components.values()),
            label.distance_m + transition.distance_m,
            label.estimated_time_s + transition.travel_time_s,
            label.patient_burden + transition.patient_burden,
            label.route + (neighbor,),
            components,
            label.workflow_trace,
        )

    @staticmethod
    def _enqueue(frontier, pending, candidate, serial):
        result = frontier.insert(candidate)
        if result.accepted:
            serial += 1
            heappush(pending, (candidate.deterministic_key, serial, candidate))
        return serial

    def _empty_result(
        self,
        decision: str,
        reason: str,
        message: str,
        overlay: DynamicEventSet,
        *,
        expanded: int = 0,
        frontier: ParetoFrontier | None = None,
    ) -> ParetoPlanResult:
        return ParetoPlanResult(
            decision,
            reason,
            message,
            overlay.plan_version,
            expanded_states=expanded,
            generated_labels=frontier.generated_labels if frontier else 0,
            pruned_labels=frontier.pruned_labels if frontier else 0,
            checked_rules=self._constraints.checked_rules(),
        )


def plan_pareto_candidates(
    *,
    scene: MedicalScene,
    planner_view: PlannerSceneView,
    events: DynamicEventSet | None = None,
    max_labels_per_state: int = DEFAULT_MAX_LABELS_PER_STATE,
    max_final_candidates: int = DEFAULT_MAX_FINAL_CANDIDATES,
) -> ParetoPlanResult:
    """Convenience API for generating Pareto candidates without selection."""

    return ParetoRoutePlanner(
        max_labels_per_state=max_labels_per_state,
        max_final_candidates=max_final_candidates,
    ).plan(scene=scene, planner_view=planner_view, events=events)
