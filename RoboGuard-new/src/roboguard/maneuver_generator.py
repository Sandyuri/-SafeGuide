"""Convert an approved graph route into deterministic turn-by-turn guidance."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping, Sequence


@dataclass(frozen=True)
class Maneuver:
    """One human-readable movement instruction derived from route geometry."""

    kind: str
    text_zh: str
    at_node_id: str | None = None
    destination_node_id: str | None = None
    distance_m: float | None = None
    turn_deg: float | None = None
    via_node_ids: tuple[str, ...] = ()


def _normalize_angle(angle_deg: float) -> float:
    """Normalize an angle to [-180, 180)."""

    return (angle_deg + 180.0) % 360.0 - 180.0


def _bearing(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) < 2 or len(right) < 2:
        raise ValueError("navigation coordinates require at least x and y")
    delta_x = float(right[0]) - float(left[0])
    delta_y = float(right[1]) - float(left[1])
    if math.isclose(delta_x, 0.0) and math.isclose(delta_y, 0.0):
        raise ValueError("consecutive route nodes cannot share the same position")
    return math.degrees(math.atan2(delta_y, delta_x))


def _name(node_id: str, names: Mapping[str, str] | None) -> str:
    if names is None:
        return node_id
    return names.get(node_id, node_id)


def generate_turn_by_turn(
    node_path: Sequence[str],
    edge_distances_m: Sequence[float],
    coordinates: Mapping[str, Sequence[float]],
    *,
    initial_yaw_deg: float = 0.0,
    names: Mapping[str, str] | None = None,
    straight_tolerance_deg: float = 15.0,
    u_turn_threshold_deg: float = 165.0,
) -> tuple[Maneuver, ...]:
    """Generate Chinese turn instructions for one approved route.

    Consecutive collinear edges are collapsed into one straight instruction.
    Positive angle changes are left turns under the map's CCW-positive yaw
    convention; negative changes are right turns.
    """

    if not node_path:
        raise ValueError("route must contain at least one node")
    if len(edge_distances_m) != len(node_path) - 1:
        raise ValueError("edge distance count must equal node count minus one")
    if not 0 <= straight_tolerance_deg < 90:
        raise ValueError("straight_tolerance_deg must be in [0, 90)")
    if not 90 < u_turn_threshold_deg <= 180:
        raise ValueError("u_turn_threshold_deg must be in (90, 180]")

    for node_id in node_path:
        if node_id not in coordinates:
            raise ValueError(f"route node has no coordinates: {node_id}")
    for distance_m in edge_distances_m:
        if (
            isinstance(distance_m, bool)
            or not math.isfinite(float(distance_m))
            or float(distance_m) <= 0
        ):
            raise ValueError(f"invalid route edge distance: {distance_m}")

    if len(node_path) == 1:
        destination = node_path[0]
        return (
            Maneuver(
                kind="ARRIVE",
                text_zh=f"已到达{_name(destination, names)}。",
                destination_node_id=destination,
            ),
        )

    maneuvers: list[Maneuver] = []
    current_heading = float(initial_yaw_deg)
    straight_start = node_path[0]
    straight_nodes: list[str] = []
    straight_distance = 0.0

    def flush_straight() -> None:
        nonlocal straight_start, straight_nodes, straight_distance
        if not straight_nodes:
            return
        destination = straight_nodes[-1]
        via = tuple(straight_nodes[:-1])
        via_text = ""
        if via:
            via_text = "，途经" + "、".join(_name(item, names) for item in via)
        maneuvers.append(
            Maneuver(
                kind="GO_STRAIGHT",
                text_zh=(
                    f"直行{straight_distance:.2f}米{via_text}，"
                    f"到达{_name(destination, names)}。"
                ),
                at_node_id=straight_start,
                destination_node_id=destination,
                distance_m=round(straight_distance, 3),
                via_node_ids=via,
            )
        )
        straight_start = destination
        straight_nodes = []
        straight_distance = 0.0

    for index, distance_m in enumerate(edge_distances_m):
        left = node_path[index]
        right = node_path[index + 1]
        target_heading = _bearing(coordinates[left], coordinates[right])
        turn_deg = _normalize_angle(target_heading - current_heading)

        if abs(turn_deg) > straight_tolerance_deg:
            flush_straight()
            if abs(turn_deg) >= u_turn_threshold_deg:
                kind = "U_TURN"
                text = f"在{_name(left, names)}掉头。"
            elif turn_deg > 0:
                kind = "TURN_LEFT"
                text = f"在{_name(left, names)}左转约{abs(turn_deg):.0f}°。"
            else:
                kind = "TURN_RIGHT"
                text = f"在{_name(left, names)}右转约{abs(turn_deg):.0f}°。"
            maneuvers.append(
                Maneuver(
                    kind=kind,
                    text_zh=text,
                    at_node_id=left,
                    turn_deg=round(turn_deg, 3),
                )
            )
            straight_start = left

        straight_nodes.append(right)
        straight_distance += float(distance_m)
        current_heading = target_heading

    flush_straight()
    destination = node_path[-1]
    maneuvers.append(
        Maneuver(
            kind="ARRIVE",
            text_zh=f"已到达{_name(destination, names)}。",
            destination_node_id=destination,
        )
    )
    return tuple(maneuvers)
