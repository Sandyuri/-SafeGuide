"""Independent route validator for every RoboGuard planning algorithm.

The validator reads the authoritative JSON files directly.  It must remain
independent from HospitalRoutePlanner, A*, Dijkstra, Pareto search, route
selection, and the execution barrier.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from roboguard.medical.route_validation_types import (
    RouteValidationReceipt,
    RouteValidationResult,
    RouteViolation,
)
from roboguard.medical.validation_map import ValidationMap, load_validation_map
from roboguard.medical.validation_risk import (
    event_is_active,
    event_target,
    event_targets,
    parse_timestamp,
    recompute_metrics,
)


CHECKED_RULES = (
    "GRAPH-001", "GRAPH-002", "GRAPH-003", "GRAPH-004", "GRAPH-005",
    "GRAPH-006", "GRAPH-007", "GRAPH-008",
    "ACCESS-001", "ACCESS-002", "ACCESS-003", "ACCESS-004",
    "ACCESSIBILITY-001", "ACCESSIBILITY-002", "ACCESSIBILITY-003",
    "ACCESSIBILITY-004",
    "WORKFLOW-001", "WORKFLOW-002", "WORKFLOW-003",
    "EVENT-001", "EVENT-002", "EVENT-003", "EVENT-005",
    "METRIC-001", "METRIC-002", "RISK-001",
    "VERSION-001", "VERSION-002",
)


def _first(mapping: Mapping[str, object], names: Sequence[str], default=None):
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return default


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Iterable):
        return tuple(str(item) for item in value if str(item))
    return ()


class IndependentRouteValidator:
    """A deterministic judge shared by all route-producing algorithms."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        validation_map: ValidationMap | None = None,
        **map_paths: str | Path,
    ) -> None:
        self.map = validation_map or load_validation_map(project_root, **map_paths)

    def validate(self, request: Mapping[str, object]) -> RouteValidationResult:
        """Validate one serialized route request and recompute all metrics."""

        task = request.get("task", {})
        if not isinstance(task, Mapping):
            raise ValueError("request.task must be an object")
        route = _string_tuple(request.get("route"))
        events_value = request.get("events", ())
        if not isinstance(events_value, Iterable) or isinstance(events_value, (str, bytes)):
            raise ValueError("request.events must be an array")
        events = tuple(events_value)
        if not all(isinstance(item, Mapping) for item in events):
            raise ValueError("request.events entries must be objects")
        violations: list[RouteViolation] = []

        source = str(_first(task, ("source", "source_node", "source_region_id", "start"), ""))
        destinations = _string_tuple(
            _first(
                task,
                (
                    "ordered_destinations",
                    "ordered_destination_region_refs",
                    "destinations",
                    "destination",
                    "destination_node",
                    "destination_region_id",
                ),
                (),
            )
        )
        permissions = set(_string_tuple(_first(task, ("permissions", "granted_permissions"), ())))
        roles = set(_string_tuple(_first(task, ("roles", "user_roles"), ())))
        cargo_kinds = {
            value.upper() for value in _string_tuple(task.get("cargo_kinds", ()))
        }
        accessibility_needs = {
            value.upper()
            for value in _string_tuple(task.get("accessibility_needs", ()))
        }
        wheelchair = bool(
            _first(task, ("wheelchair", "requires_wheelchair"), False)
        ) or bool(
            accessibility_needs.intersection(
                {"WHEELCHAIR_ACCESSIBLE", "STEP_FREE", "ELEVATOR_REQUIRED"}
            )
        )
        task_type = str(_first(task, ("task_type", "type"), "")).upper()
        start_time = parse_timestamp(_first(task, ("start_time", "started_at")))
        if start_time is None:
            start_time = datetime(1970, 1, 1, tzinfo=timezone.utc)

        self._check_versions(request, violations)
        self._check_graph(route, source, destinations, task_type, violations)
        self._check_access(
            route, destinations, permissions, roles, cargo_kinds, task_type, violations
        )
        self._check_accessibility(route, task, wheelchair, events, start_time, violations)
        completed = self._check_workflow(
            route,
            destinations,
            task,
            request.get("workflow_trace"),
            violations,
        )
        self._check_dynamic_events(route, events, start_time, permissions, violations)

        metrics = recompute_metrics(
            self.map, route, events, start_time=start_time
        )
        self._check_reported_metrics(request.get("planner_reported"), metrics, violations)
        workflow_invalid = any(item.rule_id.startswith("WORKFLOW-") for item in violations)
        workflow = {
            "completed_destinations": list(completed),
            "final_state": (
                "COMPLETED"
                if destinations and len(completed) == len(destinations) and not workflow_invalid
                else "INCOMPLETE"
            ),
        }
        try:
            plan_version = int(request.get("plan_version", 1))
        except (TypeError, ValueError):
            plan_version = -1
        decision = "INVALID" if violations else "VALID"
        receipt = None
        if decision == "VALID":
            receipt = RouteValidationReceipt.issue(
                route=route,
                map_id=self.map.map_id,
                map_version=self.map.map_version,
                context_map_version=request.get("context_map_version", ""),
                plan_version=plan_version,
                events=events,
                workflow_trace=request.get("workflow_trace", ()),
                checked_rules=CHECKED_RULES,
                recomputed=metrics,
            )
        return RouteValidationResult(
            decision=decision,
            violations=tuple(violations),
            recomputed=metrics,
            workflow=workflow,
            checked_rules=CHECKED_RULES,
            map_version=self.map.map_version,
            plan_version=plan_version,
            receipt=receipt,
        )

    def _add(
        self,
        output: list[RouteViolation],
        rule_id: str,
        kind: str,
        message: str,
        *,
        node_id: str | None = None,
        edge_id: str | None = None,
        route_index: int | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        output.append(RouteViolation(
            rule_id, kind, message, node_id, edge_id, route_index, details or {}
        ))

    def _check_versions(
        self, request: Mapping[str, object], output: list[RouteViolation]
    ) -> None:
        supplied = str(request.get("map_version", ""))
        if supplied != self.map.map_version:
            self._add(
                output, "VERSION-001", "MAP_VERSION_MISMATCH",
                f"Route uses map version {supplied or '<missing>'}; expected {self.map.map_version}.",
                details={"reported": supplied, "expected": self.map.map_version},
            )
        try:
            plan = int(request.get("plan_version", 1))
            current = int(request.get("current_plan_version", plan))
        except (TypeError, ValueError):
            plan, current = -1, -2
        if plan != current:
            self._add(
                output, "VERSION-002", "STALE_PLAN_VERSION",
                f"Route plan version {plan} is stale; current version is {current}.",
                details={"reported": plan, "expected": current},
            )

    def _task_only_allowed(self, task_type: str) -> bool:
        return "MEDICATION" in task_type and "DELIVERY" in task_type

    def _available(self, item: Mapping[str, object], task_type: str) -> bool:
        status = str(item.get("navigation_status", "open")).lower()
        open_statuses = {str(v).lower() for v in self.map.rules.get("open_statuses", ())}
        task_statuses = {str(v).lower() for v in self.map.rules.get("task_only_statuses", ())}
        return status in open_statuses or (
            status in task_statuses and self._task_only_allowed(task_type)
        )

    def _check_graph(
        self,
        route: tuple[str, ...],
        source: str,
        destinations: tuple[str, ...],
        task_type: str,
        output: list[RouteViolation],
    ) -> None:
        if not route:
            self._add(output, "GRAPH-001", "EMPTY_ROUTE", "Route must not be empty.")
            return
        if not source or route[0] != source:
            self._add(
                output, "GRAPH-002", "SOURCE_MISMATCH",
                f"Route starts at {route[0]}, not task source {source or '<missing>'}.",
                node_id=route[0], route_index=0,
            )
        if not destinations or route[-1] != destinations[-1]:
            expected = destinations[-1] if destinations else "<missing>"
            self._add(
                output, "GRAPH-003", "DESTINATION_MISMATCH",
                f"Route ends at {route[-1]}, not final destination {expected}.",
                node_id=route[-1], route_index=len(route) - 1,
            )
        for index, node_id in enumerate(route):
            node = self.map.nodes.get(node_id)
            if node is None:
                self._add(
                    output, "GRAPH-006", "UNKNOWN_NODE",
                    f"Node {node_id} does not exist in the map.",
                    node_id=node_id, route_index=index,
                )
            elif not self._available(node, task_type):
                self._add(
                    output, "GRAPH-007", "NODE_UNAVAILABLE",
                    f"Node {node_id} is not available for this task.",
                    node_id=node_id, route_index=index,
                )
        for index, (left, right) in enumerate(zip(route, route[1:])):
            edge = self.map.edge(left, right)
            if edge is None:
                reverse = self.map.one_way_reverse.get((left, right))
                if reverse is not None:
                    self._add(
                        output, "GRAPH-005", "WRONG_WAY",
                        f"Edge {reverse['id']} cannot be traversed from {left} to {right}.",
                        edge_id=str(reverse["id"]), route_index=index,
                    )
                elif left in self.map.nodes and right in self.map.nodes:
                    self._add(
                        output, "GRAPH-004", "DISCONNECTED_NODES",
                        f"Nodes {left} and {right} are not connected.",
                        route_index=index, details={"from": left, "to": right},
                    )
                continue
            if not self._available(edge, task_type):
                self._add(
                    output, "GRAPH-008", "EDGE_UNAVAILABLE",
                    f"Edge {edge['id']} is not available for this task.",
                    edge_id=str(edge["id"]), route_index=index,
                )

    def _zone_allowed(
        self, zone: str, permissions: set[str], roles: set[str]
    ) -> bool:
        required_permissions = {
            value.upper() for value in self.map.zone_permissions.get(zone, ())
        }
        required_roles = {
            value.upper() for value in self.map.zone_roles.get(zone, ())
        }
        supplied_permissions = {value.upper() for value in permissions}
        supplied_roles = {value.upper() for value in roles}
        permission_ok = not required_permissions or bool(
            required_permissions & supplied_permissions
        )
        role_ok = not required_roles or bool(required_roles & supplied_roles)
        return permission_ok and role_ok

    def _check_access(
        self,
        route: tuple[str, ...],
        destinations: tuple[str, ...],
        permissions: set[str],
        roles: set[str],
        cargo_kinds: set[str],
        task_type: str,
        output: list[RouteViolation],
    ) -> None:
        stage = 0
        for index, node_id in enumerate(route):
            node = self.map.nodes.get(node_id)
            if node is None:
                continue
            zone = str(node.get("access_zone", "public"))
            if not self._zone_allowed(zone, permissions, roles):
                self._add(
                    output, "ACCESS-001", "MISSING_PERMISSION",
                    f"Task lacks permission for node {node_id} in zone {zone}.",
                    node_id=node_id, route_index=index,
                    details={"zone": zone, "required": list(self.map.zone_permissions.get(zone, ()))},
                )
            semantic = self.map.route_semantics.get(node_id, {})
            prohibited = {
                str(value).upper()
                for value in semantic.get("prohibited_cargo_kinds", ())
            }
            conflict = prohibited & cargo_kinds
            if conflict:
                self._add(
                    output, "ACCESS-004", "CARGO_RESTRICTED",
                    f"Cargo kinds {sorted(conflict)} are prohibited at node {node_id}.",
                    node_id=node_id, route_index=index,
                )
            policy = str(semantic.get("route_policy", "ALLOW_TRANSIT")).upper()
            expected = destinations[stage] if stage < len(destinations) else None
            if policy == "DESTINATION_ONLY" and index > 0 and node_id != expected:
                self._add(
                    output, "ACCESS-003", "DESTINATION_ONLY_TRANSIT",
                    f"Destination-only node {node_id} is used as transit.",
                    node_id=node_id, route_index=index,
                )
            if policy == "MEDICATION_DELIVERY_ONLY" and not self._task_only_allowed(task_type):
                self._add(
                    output, "ACCESS-003", "TASK_ONLY_ROUTE",
                    f"Node {node_id} is reserved for medication delivery tasks.",
                    node_id=node_id, route_index=index,
                )
            if expected is not None and node_id == expected:
                stage += 1
        for index, (left, right) in enumerate(zip(route, route[1:])):
            edge = self.map.edge(left, right)
            if edge is None:
                continue
            zone = str(edge.get("access_zone", "public"))
            if not self._zone_allowed(zone, permissions, roles):
                self._add(
                    output, "ACCESS-002", "MISSING_EDGE_PERMISSION",
                    f"Task lacks permission for edge {edge['id']} in zone {zone}.",
                    edge_id=str(edge["id"]), route_index=index,
                    details={"zone": zone, "required": list(self.map.zone_permissions.get(zone, ()))},
                )
            prohibited = {
                str(value).upper()
                for value in edge.get("prohibited_cargo_kinds", ())
            }
            conflict = prohibited & cargo_kinds
            if conflict:
                self._add(
                    output, "ACCESS-004", "CARGO_RESTRICTED",
                    f"Cargo kinds {sorted(conflict)} are prohibited on edge {edge['id']}.",
                    edge_id=str(edge["id"]), route_index=index,
                )

    def _check_accessibility(
        self,
        route: tuple[str, ...],
        task: Mapping[str, object],
        wheelchair: bool,
        events: tuple[Mapping[str, object], ...],
        start_time: datetime,
        output: list[RouteViolation],
    ) -> None:
        if not wheelchair:
            return
        minimum = float(task.get(
            "minimum_width_m",
            self.map.rules.get("accessibility", {}).get("default_min_width_m", 0.9),
        ))
        for index, node_id in enumerate(route):
            node = self.map.nodes.get(node_id)
            if node is None:
                continue
            if node.get("has_stairs") is True or "stair" in str(node.get("region_type", "")).lower():
                self._add(
                    output, "ACCESSIBILITY-001", "STAIRS_ON_WHEELCHAIR_ROUTE",
                    f"Wheelchair route passes stairs at {node_id}.",
                    node_id=node_id, route_index=index,
                )
            if node.get("wheelchair_accessible") is False:
                self._add(
                    output, "ACCESSIBILITY-002", "NOT_WHEELCHAIR_ACCESSIBLE",
                    f"Node {node_id} is not wheelchair accessible.",
                    node_id=node_id, route_index=index,
                )
        elapsed = 0.0
        speed = float(self.map.rules.get("nominal_speed_mps", 1.2))
        for index, (left, right) in enumerate(zip(route, route[1:])):
            edge = self.map.edge(left, right)
            if edge is None:
                continue
            elapsed += float(edge.get("distance_m", 0.0)) / speed
            when = start_time + timedelta(seconds=elapsed)
            if edge.get("has_stairs") is True:
                self._add(
                    output, "ACCESSIBILITY-001", "STAIRS_ON_WHEELCHAIR_ROUTE",
                    f"Wheelchair route uses stair edge {edge['id']}.",
                    edge_id=str(edge["id"]), route_index=index,
                )
            if edge.get("wheelchair_accessible") is False:
                self._add(
                    output, "ACCESSIBILITY-002", "NOT_WHEELCHAIR_ACCESSIBLE",
                    f"Edge {edge['id']} is not wheelchair accessible.",
                    edge_id=str(edge["id"]), route_index=index,
                )
            width = edge.get("width_m")
            if isinstance(width, (int, float)) and float(width) < minimum:
                self._add(
                    output, "ACCESSIBILITY-004", "PASSAGE_TOO_NARROW",
                    f"Edge {edge['id']} width {width}m is below required {minimum}m.",
                    edge_id=str(edge["id"]), route_index=index,
                )
            elevator_ids = {left, right, str(edge.get("id"))}
            for event in events:
                if str(event.get("type", "")).upper() not in {
                    "ELEVATOR_OUTAGE", "ELEVATOR_DISABLED", "ELEVATOR_UNAVAILABLE"
                }:
                    continue
                if set(event_targets(event)) & elevator_ids and event_is_active(event, when):
                    self._add(
                        output, "ACCESSIBILITY-003", "ELEVATOR_UNAVAILABLE",
                        f"Elevator required by edge {edge['id']} is unavailable at arrival time.",
                        edge_id=str(edge["id"]), route_index=index,
                    )

    def _check_workflow(
        self,
        route: tuple[str, ...],
        destinations: tuple[str, ...],
        task: Mapping[str, object],
        trace_value: object,
        output: list[RouteViolation],
    ) -> tuple[str, ...]:
        if not destinations:
            self._add(
                output, "WORKFLOW-002", "MISSING_DESTINATIONS",
                "Task must provide at least one destination.",
            )
            return ()
        cursor = 0
        completed: list[str] = []
        destination_set = set(destinations)
        for index, node_id in enumerate(route):
            if cursor < len(destinations) and node_id == destinations[cursor]:
                completed.append(node_id)
                cursor += 1
            elif node_id in destination_set and node_id not in completed:
                self._add(
                    output, "WORKFLOW-001", "DESTINATION_ORDER_INVALID",
                    f"Destination {node_id} was reached before {destinations[cursor]}.",
                    node_id=node_id, route_index=index,
                )
        if cursor < len(destinations):
            self._add(
                output, "WORKFLOW-002", "DESTINATION_SKIPPED",
                f"Route did not complete destinations: {list(destinations[cursor:])}.",
                details={"missing": list(destinations[cursor:])},
            )
        confirmations = set(_string_tuple(_first(
            task,
            ("confirmation_destinations", "confirmation_destination_ids"),
            (),
        )))
        require_handoff = bool(task.get("require_handoff", False))
        require_finish = bool(task.get("require_explicit_finish", False))
        require_trace = bool(task.get("require_workflow_trace", False))
        if confirmations or require_handoff or require_finish or require_trace or trace_value:
            raw_trace = trace_value if isinstance(trace_value, Iterable) and not isinstance(
                trace_value, (str, bytes, Mapping)
            ) else ()
            reported_trace: list[tuple[str, str, str, bool]] = []
            rejected_action = False
            for entry in raw_trace:
                if isinstance(entry, Mapping):
                    action = entry.get("action")
                    from_state = entry.get("from_state")
                    to_state = entry.get("to_state")
                    accepted = entry.get("accepted", False)
                elif isinstance(entry, (list, tuple)) and len(entry) >= 5:
                    action = entry[1]
                    from_state = entry[2]
                    to_state = entry[3]
                    accepted = entry[4]
                else:
                    rejected_action = True
                    continue
                if not all(isinstance(value, str) for value in (action, from_state, to_state)) or accepted is not True:
                    rejected_action = True
                    continue
                reported_trace.append((action, from_state, to_state, accepted))

            def next_state(completed: int) -> str:
                if completed < len(destinations):
                    return f"GO_TO_{completed + 1}"
                if require_handoff:
                    return "WAIT_HANDOFF"
                if require_finish:
                    return "READY_TO_COMPLETE"
                return "COMPLETE"

            expected_trace: list[tuple[str, str, str, bool]] = []
            for index, destination in enumerate(destinations, 1):
                current = f"GO_TO_{index}"
                if destination in confirmations:
                    arrived = f"WAIT_CONFIRM_{index}"
                    expected_trace.append(
                        (f"ARRIVE:{destination}", current, arrived, True)
                    )
                    expected_trace.append(
                        (f"CONFIRM:{destination}", arrived, next_state(index), True)
                    )
                else:
                    expected_trace.append(
                        (f"ARRIVE:{destination}", current, next_state(index), True)
                    )
            if require_handoff:
                target = "READY_TO_COMPLETE" if require_finish else "COMPLETE"
                expected_trace.append(("HANDOFF", "WAIT_HANDOFF", target, True))
            if require_finish:
                expected_trace.append(
                    ("FINISH", "READY_TO_COMPLETE", "COMPLETE", True)
                )
            if rejected_action or reported_trace != expected_trace:
                self._add(
                    output,
                    "WORKFLOW-003",
                    "WORKFLOW_TRACE_INVALID",
                    "Workflow trace does not contain the required confirmations, handoff, and finish actions in order.",
                    details={
                        "expected": [list(item) for item in expected_trace],
                        "reported": [list(item) for item in reported_trace],
                    },
                )
        return tuple(completed)

    def _check_dynamic_events(
        self,
        route: tuple[str, ...],
        events: tuple[Mapping[str, object], ...],
        start_time: datetime,
        permissions: set[str],
        output: list[RouteViolation],
    ) -> None:
        speed = float(self.map.rules.get("nominal_speed_mps", 1.2))
        elapsed = 0.0
        route_edge_ids: set[str] = set()
        route_edge_arrivals: dict[str, datetime] = {}
        route_node_arrivals: dict[str, datetime] = {
            route[0]: start_time
        } if route else {}
        route_node_ids = set(route)
        for index, node_id in enumerate(route):
            when = start_time + timedelta(seconds=elapsed)
            for event in events:
                kind = str(event.get("type", "")).upper()
                targets = set(event_targets(event))
                if kind in {"NODE_CLOSED", "NODE_BLOCKED"} and node_id in targets and event_is_active(event, when):
                    self._add(
                        output, "EVENT-001", "DYNAMIC_NODE_CLOSED",
                        f"Node {node_id} is closed at estimated arrival time.",
                        node_id=node_id, route_index=index,
                    )
                if kind in {"PERMISSION_REVOKED", "PERMISSION_CHANGED"} and targets.intersection(permissions) and event_is_active(event, when):
                    self._add(
                        output, "EVENT-005", "PERMISSION_REVOKED",
                        f"Permission {target} is revoked before route completion.",
                        node_id=node_id, route_index=index,
                    )
            if index >= len(route) - 1:
                continue
            edge = self.map.edge(node_id, route[index + 1])
            if edge is None:
                continue
            edge_id = str(edge.get("id"))
            route_edge_ids.add(edge_id)
            entry = start_time + timedelta(seconds=elapsed)
            distance = float(edge.get("distance_m", 0.0))
            for event in events:
                if str(event.get("type", "")).upper() != "EDGE_COST_CHANGED":
                    continue
                endpoint_match = (
                    str(event.get("source_region_id", "")) == node_id
                    and str(event.get("target_region_id", "")) == route[index + 1]
                )
                if (
                    (edge_id in set(event_targets(event)) or endpoint_match)
                    and event_is_active(event, entry)
                ):
                    value = event.get("new_cost", event.get("distance_m"))
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        changed = float(value)
                        if changed > 0:
                            distance = changed
            base_time = distance / speed
            provisional = start_time + timedelta(seconds=elapsed + base_time)
            congestion = sum(
                max(0.0, float(event.get("severity", 0.0)))
                for event in events
                if str(event.get("type", "")).upper() in {"CONGESTION", "CROWDING"}
                and ({edge_id, route[index + 1]} & set(event_targets(event)))
                and event_is_active(event, provisional)
            )
            elapsed += base_time * (1.0 + congestion)
            arrival = start_time + timedelta(seconds=elapsed)
            route_edge_arrivals[edge_id] = arrival
            route_node_arrivals[route[index + 1]] = arrival
            for event in events:
                kind = str(event.get("type", "")).upper()
                if kind in {"EDGE_CLOSED", "EDGE_BLOCKED"} and edge_id in set(event_targets(event)) and event_is_active(event, arrival):
                    self._add(
                        output, "EVENT-002", "DYNAMIC_EDGE_CLOSED",
                        f"Edge {edge_id} is closed at traversal time.",
                        edge_id=edge_id, route_index=index,
                    )

        for event in events:
            kind = str(event.get("type", "")).upper()
            if kind not in {"EMERGENCY_CORRIDOR_ACTIVE", "EMERGENCY_ROUTE_ACTIVE"}:
                continue
            target = event_target(event) or str(event.get("risk_zone_id", ""))
            for zone in self.map.risk_zones:
                if str(zone.get("type", "")).upper() != "EMERGENCY_CORRIDOR":
                    continue
                zone_id = str(zone.get("risk_zone_id", ""))
                affected_edges = {str(value) for value in zone.get("affected_edges", ())}
                affected_nodes = {str(value) for value in zone.get("affected_nodes", ())}
                target_matches = (
                    not target
                    or target in {zone_id, "EMERGENCY_CORRIDOR"}
                    or target in affected_edges
                    or target in affected_nodes
                )
                exposure_times = [
                    route_edge_arrivals[item]
                    for item in route_edge_ids & affected_edges
                ] + [
                    route_node_arrivals[item]
                    for item in route_node_ids & affected_nodes
                    if item in route_node_arrivals
                ]
                if target_matches and any(
                    event_is_active(event, when) for when in exposure_times
                ):
                    self._add(
                        output, "EVENT-003", "ACTIVE_EMERGENCY_CORRIDOR",
                        "Route enters an emergency corridor while its hard constraint is active.",
                        details={"risk_zone_id": zone_id},
                    )

    def _check_reported_metrics(
        self,
        reported_value: object,
        recomputed: Mapping[str, object],
        output: list[RouteViolation],
    ) -> None:
        if reported_value is None:
            return
        if not isinstance(reported_value, Mapping):
            raise ValueError("request.planner_reported must be an object")
        tolerances = dict(self.map.rules.get("metric_tolerance", {}))
        comparisons = (
            ("distance_m", "METRIC-001", "DISTANCE_MISMATCH"),
            ("travel_time_s", "METRIC-002", "TRAVEL_TIME_MISMATCH"),
            ("risk_score", "RISK-001", "RISK_MISMATCH"),
        )
        for name, rule_id, kind in comparisons:
            if name not in reported_value:
                continue
            try:
                claimed = float(reported_value[name])
                actual = float(recomputed[name])
            except (TypeError, ValueError):
                claimed, actual = math.nan, float(recomputed[name])
            tolerance = float(tolerances.get(name, 1e-6))
            if not math.isfinite(claimed) or not math.isclose(claimed, actual, rel_tol=0.0, abs_tol=tolerance):
                self._add(
                    output, rule_id, kind,
                    f"Planner reported {name}={claimed}; independent value is {actual}.",
                    details={"reported": claimed, "recomputed": actual, "tolerance": tolerance},
                )


def validate_route(
    request: Mapping[str, object], project_root: str | Path
) -> dict[str, object]:
    """Convenience JSON-in/JSON-out entry point."""

    return IndependentRouteValidator(project_root).validate(request).to_dict()
