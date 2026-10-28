"""Independent distance, time, and risk recomputation."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping

from roboguard.medical.validation_map import ValidationMap


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def event_is_active(event: Mapping[str, object], when: datetime | None) -> bool:
    """Return whether an event applies at a route step's estimated time."""

    if event.get("active") is False:
        return False
    if when is None:
        return True
    begins = parse_timestamp(
        event.get("effective_at", event.get("starts_at", event.get("timestamp")))
    )
    ends = parse_timestamp(event.get("expires_at", event.get("ends_at")))
    return (begins is None or begins <= when) and (ends is None or when <= ends)


def event_target(event: Mapping[str, object]) -> str:
    targets = event_targets(event)
    return next(iter(targets), "")


def event_targets(event: Mapping[str, object]) -> tuple[str, ...]:
    """Return every canonical target carried by an event."""

    result: list[str] = []
    for key in (
        "region_id", "node_id", "edge_id", "resource_id", "target_id",
        "permission", "permission_id",
    ):
        value = event.get(key)
        if isinstance(value, str) and value:
            result.append(value)
    region_ids = event.get("region_ids", ())
    if isinstance(region_ids, Iterable) and not isinstance(region_ids, (str, bytes)):
        result.extend(str(value) for value in region_ids if str(value))
    return tuple(dict.fromkeys(result))


def _event_kind(event: Mapping[str, object]) -> str:
    kind = str(event.get("type", "")).upper()
    return {
        "CROWDING": "CONGESTION",
        "ELEVATOR_OUTAGE": "ELEVATOR_UNAVAILABLE",
        "ELEVATOR_DISABLED": "ELEVATOR_UNAVAILABLE",
        "PERMISSION_CHANGED": "PERMISSION_REVOKED",
    }.get(kind, kind)


def recompute_metrics(
    validation_map: ValidationMap,
    route: Iterable[str],
    events: Iterable[Mapping[str, object]],
    *,
    start_time: datetime | None = None,
) -> dict[str, object]:
    """Recompute metrics from raw edges and rules, never planner output."""

    path = tuple(route)
    speed = float(validation_map.rules.get("nominal_speed_mps", 1.2))
    risk_rules = dict(validation_map.rules.get("risk", {}))
    edge_rates = dict(risk_rules.get("edge_risk_per_meter", {}))
    fixed_node_risk = dict(risk_rules.get("fixed_node_risk", {}))
    components: defaultdict[str, float] = defaultdict(float)
    distance = 0.0
    elapsed = 0.0
    traversed: list[tuple[str, str, Mapping[str, object], float, float]] = []
    node_arrivals: dict[str, float] = {path[0]: 0.0} if path else {}
    edge_arrivals: dict[str, float] = {}
    for left, right in zip(path, path[1:]):
        edge = validation_map.edge(left, right)
        if edge is None:
            continue
        edge_id = str(edge.get("id"))
        entry_time = start_time + timedelta(seconds=elapsed) if start_time else None
        segment_distance = float(edge.get("distance_m", 0.0))
        for event in events:
            if _event_kind(event) != "EDGE_COST_CHANGED":
                continue
            targets = set(event_targets(event))
            endpoint_match = (
                str(event.get("source_region_id", "")) == left
                and str(event.get("target_region_id", "")) == right
            )
            if (edge_id in targets or endpoint_match) and event_is_active(event, entry_time):
                value = event.get("new_cost", event.get("distance_m"))
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    changed = float(value)
                    if changed > 0:
                        segment_distance = changed
        base_time = segment_distance / speed if speed > 0 else 0.0
        provisional_arrival = (
            start_time + timedelta(seconds=elapsed + base_time)
            if start_time
            else None
        )
        congestion = sum(
            max(0.0, float(event.get("severity", 0.0)))
            for event in events
            if _event_kind(event) == "CONGESTION"
            and ({edge_id, right} & set(event_targets(event)))
            and event_is_active(event, provisional_arrival)
        )
        segment_time = base_time * (1.0 + congestion)
        distance += segment_distance
        elapsed += segment_time
        traversed.append((left, right, edge, segment_distance, elapsed))
        node_arrivals[right] = elapsed
        edge_arrivals[edge_id] = elapsed

    # Risk areas are edge-based to avoid double-counting their tagged nodes.
    for zone in validation_map.risk_zones:
        zone_type = str(zone.get("type", "UNKNOWN_RISK")).upper()
        affected = {str(item) for item in zone.get("affected_edges", ())}
        rate = float(
            edge_rates.get(zone_type, zone.get("base_risk_per_meter", 0.0))
        )
        exposure = sum(
            segment_distance
            for _, _, edge, segment_distance, _ in traversed
            if str(edge.get("id")) in affected
        )
        if exposure:
            components[zone_type.lower()] += exposure * rate

    for node_id in path[1:]:
        tags = {
            str(item).upper()
            for item in validation_map.route_semantics.get(node_id, {}).get(
                "semantic_tags", ()
            )
        }
        for tag in ("NO_STOPPING", "CONTROLLED_RESOURCE_AREA"):
            if tag in tags:
                components[tag.lower()] += float(
                    fixed_node_risk.get(tag, risk_rules.get(tag, 0.0))
                )

    congestion_multiplier = float(risk_rules.get("CONGESTION_MULTIPLIER", 10.0))
    route_nodes = set(path)
    route_edges = {str(edge.get("id")) for _, _, edge, _, _ in traversed}
    for event in events:
        if _event_kind(event) != "CONGESTION":
            continue
        targets = set(event_targets(event))
        matched = targets & (route_nodes | route_edges)
        if not matched:
            continue
        target = sorted(matched)[0]
        arrival_s = node_arrivals.get(target, edge_arrivals.get(target, 0.0))
        when = start_time + timedelta(seconds=arrival_s) if start_time else None
        if not event_is_active(event, when):
            continue
        severity = max(0.0, float(event.get("severity", 0.0)))
        components["congestion"] += severity * congestion_multiplier

    rounded_components = {
        key: round(value, 6) for key, value in sorted(components.items()) if value
    }
    return {
        "distance_m": round(distance, 6),
        "travel_time_s": round(elapsed, 6),
        "risk_score": round(sum(rounded_components.values()), 6),
        "risk_components": rounded_components,
    }
