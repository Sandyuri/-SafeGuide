"""D* Lite local route repair and escalation to full Pareto replanning."""

from __future__ import annotations

from dataclasses import dataclass, replace
from heapq import heappop, heappush
import math
from typing import Iterable

from roboguard.medical.change_classifier import (
    FULL_PARETO_REPLAN,
    LOCAL_REPAIR,
    NO_CHANGE,
    ChangeClassification,
    NavigationChangeClassifier,
)
from roboguard.medical.guard_types import ALLOW, PAUSE, REJECT
from roboguard.medical.joint_planner import (
    DynamicEventSet,
    HospitalRoutePlanner,
    NOMINAL_SPEED_MPS,
    PlanResult,
    WorkflowAutomaton,
    route_affected_by_events,
)
from roboguard.medical.pareto_planner import ParetoRoutePlanner
from roboguard.medical.pareto_types import ParetoLabel, ParetoPlanResult, ProductState
from roboguard.medical.planner_view import PlannerSceneView
from roboguard.medical.route_selector import RouteSelector
from roboguard.medical.scene import MedicalScene
from roboguard.medical.task_contracts import RouteContext, TaskSpec


INF = math.inf


@dataclass(frozen=True)
class DStarRepairResult:
    route: tuple[str, ...]
    distance_m: float
    initial_expanded_states: int
    repair_expanded_states: int
    reachable: bool


@dataclass(frozen=True)
class IncrementalReplanResult:
    result: PlanResult
    mode: str
    classification: ChangeClassification
    preserved_prefix: tuple[str, ...] = ()
    fallback_reason: str = ""
    dstar_initial_expanded_states: int = 0
    dstar_repair_expanded_states: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "classification": self.classification.action,
            "change_reasons": list(self.classification.reasons),
            "preserved_prefix": list(self.preserved_prefix),
            "fallback_reason": self.fallback_reason,
            "dstar_initial_expanded_states": self.dstar_initial_expanded_states,
            "dstar_repair_expanded_states": self.dstar_repair_expanded_states,
            "result": self.result.to_dict(),
        }


class DStarLite:
    """Incremental shortest-path repair for one workflow segment."""

    def __init__(
        self,
        *,
        scene: MedicalScene,
        planner_view: PlannerSceneView,
        start: str,
        goal: str,
        baseline_events: DynamicEventSet | None = None,
    ) -> None:
        self.scene = scene
        self.view = planner_view
        self.regions = planner_view.payload["regions"]
        self.start = start
        self.last_start = start
        self.goal = goal
        self.events = baseline_events or DynamicEventSet.empty()
        self.km = 0.0
        self.g = {node: INF for node in self.regions}
        self.rhs = {node: INF for node in self.regions}
        self.rhs[goal] = 0.0
        self.queue: list[tuple[float, float, str]] = []
        self._rules = HospitalRoutePlanner("joint")
        self._heuristic_scale = self._rules._heuristic_scale(self.regions)
        self.predecessors = self._build_predecessors()
        self.successors = {
            node: tuple(sorted(region.get("connections", ())))
            for node, region in self.regions.items()
        }
        self._push(goal)
        self.initial_expanded_states = self.compute_shortest_path()

    def _build_predecessors(self) -> dict[str, tuple[str, ...]]:
        predecessors = {node: [] for node in self.regions}
        for node, region in self.regions.items():
            for neighbor in region.get("connections", ()):
                if neighbor in predecessors:
                    predecessors[neighbor].append(node)
        return {node: tuple(sorted(items)) for node, items in predecessors.items()}

    def _heuristic(self, left: str, right: str) -> float:
        left_coords = self.regions[left].get("coords", ())
        right_coords = self.regions[right].get("coords", ())
        if len(left_coords) < 2 or len(right_coords) < 2:
            return 0.0
        return math.dist(
            tuple(float(item) for item in left_coords[:2]),
            tuple(float(item) for item in right_coords[:2]),
        ) * self._heuristic_scale

    def _key(self, node: str) -> tuple[float, float]:
        value = min(self.g[node], self.rhs[node])
        return value + self._heuristic(self.start, node) + self.km, value

    def _push(self, node: str) -> None:
        first, second = self._key(node)
        heappush(self.queue, (first, second, node))

    def _top_key(self) -> tuple[float, float]:
        while self.queue:
            first, second, node = self.queue[0]
            if (first, second) == self._key(node) and self.g[node] != self.rhs[node]:
                return first, second
            heappop(self.queue)
        return INF, INF

    def _matches(self, node: str, identity: str | None) -> bool:
        if identity is None:
            return False
        if node == identity:
            return True
        try:
            return self.view.resolve_reference(node) == identity
        except Exception:
            return False

    def _blocked(self, node: str, events: DynamicEventSet) -> bool:
        violation = self._rules._node_violation(
            self.scene, self.view, node, events, self.goal
        )
        return violation is not None

    def _cost(self, left: str, right: str, events: DynamicEventSet) -> float:
        if right not in self.regions or right not in self.successors.get(left, ()):
            return INF
        if self._blocked(right, events):
            return INF
        raw = self.regions[left].get("connection_costs", {}).get(right)
        if not self._rules._valid_edge(raw):
            return INF
        cost = float(raw)
        for event in events.events:
            if event.event_type != "EDGE_COST_CHANGED":
                continue
            if self._matches(left, event.source_region_id) and self._matches(
                right, event.target_region_id
            ):
                cost = float(event.new_cost)
        return cost

    def _update_vertex(self, node: str) -> None:
        if node != self.goal:
            self.rhs[node] = min(
                (
                    self._cost(node, successor, self.events) + self.g[successor]
                    for successor in self.successors.get(node, ())
                ),
                default=INF,
            )
        if self.g[node] != self.rhs[node]:
            self._push(node)

    def compute_shortest_path(self) -> int:
        expanded = 0
        while self._top_key() < self._key(self.start) or self.rhs[self.start] != self.g[self.start]:
            if not self.queue:
                break
            old_first, old_second, node = heappop(self.queue)
            old_key = (old_first, old_second)
            new_key = self._key(node)
            if old_key < new_key:
                self._push(node)
                continue
            expanded += 1
            if self.g[node] > self.rhs[node]:
                self.g[node] = self.rhs[node]
                for predecessor in self.predecessors.get(node, ()):
                    self._update_vertex(predecessor)
            else:
                self.g[node] = INF
                self._update_vertex(node)
                for predecessor in self.predecessors.get(node, ()):
                    self._update_vertex(predecessor)
        return expanded

    def move_start(self, start: str) -> None:
        self.km += self._heuristic(self.last_start, start)
        self.start = start
        self.last_start = start

    def apply_events(self, events: DynamicEventSet) -> int:
        old_events = self.events
        changed_sources = set()
        for left, successors in self.successors.items():
            for right in successors:
                if self._cost(left, right, old_events) != self._cost(left, right, events):
                    changed_sources.add(left)
        self.events = events
        for node in sorted(changed_sources):
            self._update_vertex(node)
        return self.compute_shortest_path()

    def extract_route(self) -> tuple[str, ...]:
        if not math.isfinite(self.g[self.start]):
            return ()
        route = [self.start]
        visited = {self.start}
        current = self.start
        while current != self.goal:
            choices = []
            for successor in self.successors.get(current, ()):
                cost = self._cost(current, successor, self.events)
                score = cost + self.g[successor]
                if math.isfinite(score):
                    choices.append((score, cost, successor))
            if not choices:
                return ()
            _, _, current = min(choices)
            if current in visited:
                return ()
            visited.add(current)
            route.append(current)
        return tuple(route)

    def repair(self, events: DynamicEventSet) -> DStarRepairResult:
        expanded = self.apply_events(events)
        route = self.extract_route()
        distance = 0.0
        if route:
            distance = sum(
                self._cost(left, right, events)
                for left, right in zip(route, route[1:])
            )
        return DStarRepairResult(
            route,
            distance,
            self.initial_expanded_states,
            expanded,
            bool(route),
        )


class IncrementalNavigationReplanner:
    """Use D* Lite for local changes and Pareto search for major changes."""

    def __init__(self, *, selector: RouteSelector | None = None, validator=None) -> None:
        self.selector = selector or RouteSelector()
        self.validator = validator
        self.classifier = NavigationChangeClassifier()
        self._rules = HospitalRoutePlanner("joint")

    def replan(
        self,
        *,
        scene: MedicalScene,
        planner_view: PlannerSceneView,
        previous: PlanResult,
        task: TaskSpec,
        context: RouteContext,
        events: DynamicEventSet,
        changed_fields: Iterable[str] = (),
    ) -> IncrementalReplanResult:
        # Imported lazily to keep the search modules independent from the
        # orchestration layer while still requiring a fresh validation receipt
        # for every repaired or version-bumped route.
        from roboguard.navigation import (
            _validate_pareto_candidates,
            _validate_plan_result,
        )

        classification = self.classifier.classify(
            events, changed_fields=changed_fields
        )
        if events.plan_version <= previous.plan_version:
            paused = PlanResult(
                PAUSE,
                "PLAN_VERSION_NOT_NEWER",
                "Replanning requires a newer plan version.",
                previous.planner,
                previous.plan_version,
                route=previous.route,
                replan_allowed=True,
            )
            return IncrementalReplanResult(paused, "SAFE_PAUSE", classification)
        if classification.action == FULL_PARETO_REPLAN:
            return self._full_replan(
                scene, planner_view, task, context, events,
                classification, "major change requires full Pareto search",
            )
        if classification.action == NO_CHANGE or not route_affected_by_events(
            previous.route, planner_view, events
        ):
            unchanged = replace(
                previous,
                plan_version=events.plan_version,
                validation_receipt=None,
            )
            unchanged = _validate_plan_result(
                unchanged,
                planner_view=planner_view,
                task=task,
                context=context,
                events=events,
                validator=self.validator,
            )
            return IncrementalReplanResult(
                unchanged, "UNCHANGED", classification, previous.route
            )

        anchor_index = self._repair_anchor(previous.route, planner_view, events)
        prefix = previous.route[: anchor_index + 1]
        anchor = prefix[-1]
        destinations = tuple(
            planner_view.payload["task"].get(
                "ordered_destination_region_refs",
                (planner_view.payload["task"]["destination_region_ref"],),
            )
        )
        remaining = self._remaining_destinations(destinations, prefix)
        repaired = list(prefix)
        initial_expanded = 0
        repair_expanded = 0
        current = anchor
        for destination in remaining:
            session = DStarLite(
                scene=scene,
                planner_view=planner_view,
                start=current,
                goal=destination,
            )
            segment = session.repair(events)
            initial_expanded += segment.initial_expanded_states
            repair_expanded += segment.repair_expanded_states
            if not segment.reachable:
                return self._full_replan(
                    scene, planner_view, task, context, events,
                    classification, "D* Lite found no local replacement",
                    prefix, initial_expanded, repair_expanded,
                )
            repaired.extend(segment.route[1:])
            current = destination
        route = tuple(repaired)
        violation = self._rules._first_violation(
            scene, planner_view, route, events, destinations
        )
        if violation:
            return self._full_replan(
                scene, planner_view, task, context, events,
                classification, f"D* Lite route failed hard rule {violation[1]}",
                prefix, initial_expanded, repair_expanded,
            )
        candidate = self._candidate(
            scene, planner_view, route, destinations, events, task
        )
        fallback = self._profile_violation(candidate, previous, task)
        if fallback:
            return self._full_replan(
                scene, planner_view, task, context, events,
                classification, fallback, prefix, initial_expanded, repair_expanded,
            )
        candidates = ParetoPlanResult(
            ALLOW,
            "DSTAR_REPAIR_READY",
            "D* Lite produced a locally repaired route.",
            events.plan_version,
            (candidate,),
            checked_rules=self._rules._checked_rules(),
            expanded_states=repair_expanded,
        )
        candidates = _validate_pareto_candidates(
            candidates,
            planner_view=planner_view,
            task=task,
            context=context,
            events=events,
            validator=self.validator,
        )
        if not candidates.allowed or not candidates.candidates:
            return self._full_replan(
                scene,
                planner_view,
                task,
                context,
                events,
                classification,
                "D* Lite route failed independent validation",
                prefix,
                initial_expanded,
                repair_expanded,
            )
        selection = self.selector.select(
            candidates, task=task, context=context, require_validation=True
        )
        result = selection.to_plan_result(checked_rules=candidates.checked_rules)
        result = replace(
            result,
            planner="dstar_lite",
            expanded_states=repair_expanded,
            message="D* Lite repaired the selected route after a local change.",
        )
        return IncrementalReplanResult(
            result,
            "DSTAR_LITE_REPAIR",
            classification,
            prefix,
            dstar_initial_expanded_states=initial_expanded,
            dstar_repair_expanded_states=repair_expanded,
        )

    @staticmethod
    def _remaining_destinations(destinations, prefix):
        completed = 0
        for node in prefix:
            if completed < len(destinations) and node == destinations[completed]:
                completed += 1
        return destinations[completed:]

    @staticmethod
    def _repair_anchor(route, view, events):
        affected = []
        for event in events.events:
            if event.event_type == "EDGE_COST_CHANGED":
                for index, (left, right) in enumerate(zip(route, route[1:])):
                    identities = {left, right}
                    try:
                        real_left = view.resolve_reference(left)
                        real_right = view.resolve_reference(right)
                    except Exception:
                        real_left, real_right = left, right
                    if (
                        event.source_region_id in {left, real_left}
                        and event.target_region_id in {right, real_right}
                    ):
                        affected.append(index)
            else:
                for index, node in enumerate(route):
                    try:
                        real = view.resolve_reference(node)
                    except Exception:
                        real = node
                    if node in event.region_ids or real in event.region_ids:
                        affected.append(max(0, index - 1))
        return min(affected) if affected else 0

    def _candidate(self, scene, view, route, destinations, events, task):
        components = self._rules._route_risk_components(view, route, events)
        burden = sum(
            float(view.payload["regions"][node].get("patient_burden", 0.0))
            for node in route[1:]
        )
        workflow = WorkflowAutomaton(
            destinations,
            plan_version=events.plan_version,
            confirmation_destinations=tuple(
                view.payload["task"].get(
                    "confirmation_destination_region_refs", ()
                )
            ),
            require_handoff=task.require_handoff,
            require_explicit_finish=task.require_explicit_finish,
        )
        for destination in destinations:
            workflow.arrive(destination, plan_version=events.plan_version)
            if destination in workflow.confirmation_destinations:
                workflow.confirm(destination, plan_version=events.plan_version)
        if task.require_handoff:
            workflow.handoff()
        if task.require_explicit_finish:
            workflow.finish()
        trace = tuple(
            (item.rule_id, item.action, item.from_state, item.to_state, item.accepted)
            for item in workflow.trace
        )
        return ParetoLabel(
            ProductState(route[-1], workflow.state, events.plan_version),
            sum(components.values()),
            self._route_distance(view, route, events),
            self._route_elapsed(view, route, events),
            burden,
            route,
            components,
            trace,
        )

    @staticmethod
    def _route_distance(view, route, events):
        total = 0.0
        for left, right in zip(route, route[1:]):
            cost = float(view.payload["regions"][left]["connection_costs"][right])
            try:
                real_left = view.resolve_reference(left)
                real_right = view.resolve_reference(right)
            except Exception:
                real_left, real_right = left, right
            for event in events.events:
                if (
                    event.event_type == "EDGE_COST_CHANGED"
                    and event.source_region_id in {left, real_left}
                    and event.target_region_id in {right, real_right}
                ):
                    cost = float(event.new_cost)
            total += cost
        return total

    def _route_elapsed(self, view, route, events):
        elapsed = 0.0
        for left, right in zip(route, route[1:]):
            distance = self._route_distance(view, (left, right), events)
            congestion = self._rules._congestion(
                events, right, self._rules._real_id(view, right)
            )
            elapsed += distance * (1.0 + congestion) / NOMINAL_SPEED_MPS
        return elapsed

    def _profile_violation(self, candidate, previous, task):
        profile = self.selector.resolve_profile(task)
        config = self.selector.profiles[profile]
        strategy = config["strategy"]
        if candidate.risk > previous.risk_score + 1e-9 and profile != "EMERGENCY":
            return "D* Lite repair increases medical risk"
        if strategy.endswith("distance_ratio") and previous.distance_m > 0:
            if candidate.distance_m > previous.distance_m * float(
                config["max_distance_ratio"]
            ) + 1e-9:
                return "D* Lite repair exceeds task-profile detour budget"
        if profile in {"WHEELCHAIR", "MOBILITY_ASSISTED"}:
            if candidate.patient_burden > previous.patient_burden + 1e-9:
                return "D* Lite repair increases patient burden"
        if strategy == "time_within_risk_limit" and candidate.risk > float(
            config["max_risk"]
        ) + 1e-9:
            return "D* Lite repair exceeds emergency risk limit"
        return ""

    def _full_replan(
        self,
        scene,
        view,
        task,
        context,
        events,
        classification,
        reason,
        prefix=(),
        initial_expanded=0,
        repair_expanded=0,
    ):
        from roboguard.navigation import _validate_pareto_candidates

        candidates = ParetoRoutePlanner().plan(
            scene=scene, planner_view=view, events=events
        )
        candidates = _validate_pareto_candidates(
            candidates,
            planner_view=view,
            task=task,
            context=context,
            events=events,
            validator=self.validator,
        )
        selection = self.selector.select(
            candidates, task=task, context=context, require_validation=True
        )
        result = selection.to_plan_result(checked_rules=candidates.checked_rules)
        result = replace(result, expanded_states=candidates.expanded_states)
        if result.decision == REJECT:
            result = replace(
                result,
                decision=PAUSE,
                reason_code="SAFE_PAUSE_NO_REPLAN_ROUTE",
                message="The old route is invalid and no safe replacement is available.",
                replan_allowed=True,
            )
        return IncrementalReplanResult(
            result,
            "FULL_PARETO_REPLAN",
            classification,
            tuple(prefix),
            reason,
            initial_expanded,
            repair_expanded,
        )
