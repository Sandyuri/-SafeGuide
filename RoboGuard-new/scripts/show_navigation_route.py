"""Plan, review, print, and render one hospital navigation route locally.

This command deliberately bypasses the SafeGate service and SPINE planner. It
creates the compatibility task envelope required by the existing navigation
guard, runs the deterministic local route check, and renders the approved route
on the registered hospital floor plan.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import math
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_SOURCE = PROJECT_ROOT / "src"
if str(LOCAL_SOURCE) not in sys.path:
    sys.path.insert(0, str(LOCAL_SOURCE))


from roboguard.medical.providers import NavigationSandboxSceneProvider
from roboguard.medical.scene import MedicalScene, MedicalSceneError
from roboguard.medical.temporal_guard import MedicalTemporalGuard
from roboguard.maneuver_generator import generate_turn_by_turn
from roboguard.navigation import NavigationExecutionGuard, build_navigation_view
from roboguard.safegate_adapter import AuthorizedTask
from roboguard.spine_adapter import adapt_spine_plan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show the local navigation route approved by RoboGuard."
    )
    parser.add_argument(
        "--from",
        dest="source",
        default="门诊大厅",
        help="Start location name or node ID (default: 门诊大厅)",
    )
    parser.add_argument(
        "--to",
        dest="destination",
        required=True,
        help="Destination location name or node ID",
    )
    parser.add_argument(
        "--sandbox",
        type=Path,
        default=PROJECT_ROOT / "navigation_sandbox",
    )
    parser.add_argument(
        "--base-scene",
        type=Path,
        default=(
            PROJECT_ROOT
            / "data"
            / "fixtures"
            / "medical"
            / "hospital_1f.synthetic.json"
        ),
    )
    parser.add_argument(
        "--bindings",
        type=Path,
        default=(
            PROJECT_ROOT
            / "data"
            / "navigation"
            / "hospital_1f.bindings.json"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "hospital_route.svg",
    )
    parser.add_argument(
        "--floorplan-image",
        type=Path,
        help="Floor-plan image; defaults to the sandbox's registered 1F image",
    )
    parser.add_argument(
        "--floorplan-anchors",
        type=Path,
        help="Pixel anchor mapping; defaults to the sandbox's 1F anchor mapping",
    )
    parser.add_argument(
        "--floorplan-edge-routes",
        type=Path,
        help="Manual corridor waypoints; defaults to the sandbox's 1F edge routes",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the generated SVG after validation",
    )
    parser.add_argument(
        "--temporal-mode",
        choices=("hybrid", "fixed", "off"),
        default="hybrid",
        help="Temporal review mode (default: hybrid)",
    )
    parser.add_argument("--rules", type=Path, help="Medical navigation rules JSON")
    parser.add_argument("--model", help="OpenAI-compatible model; defaults to ROBOGUARD_MODEL")
    parser.add_argument("--show-ltl", action="store_true", help="Print checked LTL and trace")
    return parser.parse_args()


def resolve_location(provider, scene, value: str) -> str:
    if value in scene.data["regions"]:
        return value
    resolved = provider.resolve_location(value)
    if resolved is None:
        raise MedicalSceneError(f"无法解析地点名称：{value}")
    return resolved


def move_robot(scene: MedicalScene, source: str) -> MedicalScene:
    if scene.data["robot_state"]["current_region_id"] == source:
        return scene
    data = scene.to_dict()
    data["robot_state"]["current_region_id"] = source
    data["snapshot"]["version"] += 1
    return MedicalScene.from_mapping(data)


def route_costs(view, references):
    regions = view.payload["regions"]
    costs = []
    for left, right in zip(references, references[1:]):
        explicit = regions[left].get("connection_costs", {})
        if right in explicit:
            cost = float(explicit[right])
        else:
            cost = math.dist(regions[left]["coords"], regions[right]["coords"])
        costs.append(cost)
    return costs


def render_svg(
    scene,
    view,
    route,
    edge_costs,
    output: Path,
    floorplan_image: Path,
    floorplan_anchors: Path,
    floorplan_edge_routes: Path,
    navigation_map: Path,
) -> None:
    regions = scene.data["regions"]
    route_ids = [view.resolve_reference(reference) for reference in route.regions]
    try:
        anchors = json.loads(floorplan_anchors.read_text(encoding="utf-8"))
        route_geometry = json.loads(floorplan_edge_routes.read_text(encoding="utf-8"))
        navigation = json.loads(navigation_map.read_text(encoding="utf-8"))
        image_bytes = floorplan_image.read_bytes()
    except (OSError, json.JSONDecodeError) as error:
        raise OSError(f"无法读取平面图或像素锚点：{error}") from error
    image_size = anchors.get("source_image_size_px")
    if not isinstance(image_size, list) or len(image_size) != 2:
        raise ValueError("平面图锚点缺少 source_image_size_px")
    width, height = (int(image_size[0]), int(image_size[1]))
    points = {
        item["node_id"]: tuple(float(value) for value in item["floorplan_pixel_xy"])
        for item in anchors.get("nodes", [])
    }
    display_points = {
        item["node_id"]: tuple(float(value) for value in item.get(
            "display_floorplan_pixel_xy", item["floorplan_pixel_xy"]
        ))
        for item in anchors.get("nodes", [])
    }
    missing = [node_id for node_id in route_ids if node_id not in points]
    if missing:
        raise ValueError(f"路径节点缺少平面图像素锚点：{missing}")
    edge_routes = route_geometry.get("edge_routes", {})
    edge_lookup = {
        frozenset((edge["from"], edge["to"])): edge
        for edge in navigation.get("edges", [])
    }
    route_segments = []
    for left, right in zip(route_ids, route_ids[1:]):
        edge = edge_lookup.get(frozenset((left, right)))
        if edge is None:
            raise ValueError(f"路径缺少导航边：{left} -> {right}")
        waypoints = edge_routes.get(edge["id"])
        if not isinstance(waypoints, list):
            raise ValueError(f"导航边缺少平面图走廊折点：{edge['id']}")
        if edge["from"] != left:
            waypoints = list(reversed(waypoints))
        route_segments.append([
            points[left],
            *[tuple(float(value) for value in point) for point in waypoints],
            points[right],
        ])

    def polyline_midpoint(polyline):
        lengths = [
            math.hypot(right[0] - left[0], right[1] - left[1])
            for left, right in zip(polyline, polyline[1:])
        ]
        target = sum(lengths) / 2
        walked = 0.0
        for index, length in enumerate(lengths):
            if walked + length >= target and length:
                ratio = (target - walked) / length
                left, right = polyline[index], polyline[index + 1]
                return (
                    left[0] + (right[0] - left[0]) * ratio,
                    left[1] + (right[1] - left[1]) * ratio,
                )
            walked += length
        return polyline[-1]
    suffix = floorplan_image.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    image_uri = f"data:{mime};base64," + base64.b64encode(image_bytes).decode("ascii")

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="{round(1600 * height / width)}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        f'<image href="{image_uri}" x="0" y="0" width="{width}" height="{height}" preserveAspectRatio="none"/>',
        '<rect width="100%" height="100%" fill="#ffffff" opacity="0.10"/>',
    ]

    # Show registered places as subtle map annotations instead of drawing the
    # complete abstract topology, which produced the former star-shaped view.
    for region_id, (x, y) in display_points.items():
        if region_id in regions and region_id not in route_ids:
            lines.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="18" fill="#64748b" '
                'fill-opacity="0.55" stroke="#ffffff" stroke-width="10"/>'
            )

    for segment in route_segments:
        segment_points = " ".join(f"{x:.1f},{y:.1f}" for x, y in segment)
        lines.append(
            f'<polyline points="{segment_points}" fill="none" stroke="#ffffff" '
            'stroke-opacity="0.92" stroke-width="70" stroke-linejoin="round" stroke-linecap="round"/>'
        )
        lines.append(
            f'<polyline points="{segment_points}" fill="none" stroke="#2563eb" '
            'stroke-width="38" stroke-linejoin="round" stroke-linecap="round"/>'
        )

    for region_id in route_ids:
        x, y = display_points[region_id]
        route_x, route_y = points[region_id]
        if (x, y) != (route_x, route_y):
            lines.append(
                f'<line x1="{route_x:.1f}" y1="{route_y:.1f}" x2="{x:.1f}" y2="{y:.1f}" '
                'stroke="#475569" stroke-width="12" stroke-dasharray="35 25"/>'
            )
        if region_id == route_ids[0]:
            fill, radius = "#16a34a", 58
        elif region_id == route_ids[-1]:
            fill, radius = "#dc2626", 58
        else:
            fill, radius = "#2563eb", 46
        lines.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" fill="{fill}" stroke="#ffffff" stroke-width="18"/>'
        )
        label = html.escape(regions[region_id].get("display_name", region_id))
        lines.append(
            f'<text x="{x:.1f}" y="{y - 82:.1f}" text-anchor="middle" '
            'font-family="Microsoft YaHei,Noto Sans CJK SC,sans-serif" font-size="86" '
            'font-weight="700" fill="#172033" stroke="#ffffff" stroke-width="22" '
            f'paint-order="stroke">{label}</text>'
        )

    for index, segment in enumerate(route_segments):
        label_x, label_y = polyline_midpoint(segment)
        lines.append(
            f'<text x="{label_x:.1f}" y="{label_y - 40:.1f}" '
            'text-anchor="middle" font-family="Consolas,monospace" font-size="72" '
            'font-weight="700" fill="#1d4ed8" stroke="#ffffff" stroke-width="18" '
            f'paint-order="stroke">{edge_costs[index]:.2f} m</text>'
        )

    lines.extend(
        [
            '<rect x="100" y="100" width="2850" height="390" rx="45" fill="#ffffff" fill-opacity="0.92" stroke="#cbd5e1" stroke-width="8"/>',
            '<text x="180" y="265" font-family="Microsoft YaHei,Noto Sans CJK SC,sans-serif" font-size="145" font-weight="700" fill="#172033">RoboGuard 医院导航路线</text>',
            f'<text x="180" y="405" font-family="Microsoft YaHei,Noto Sans CJK SC,sans-serif" font-size="78" fill="#475569">总距离 {sum(edge_costs):.2f} m · Dijkstra 最短路径 · RoboGuard ALLOW</text>',
            f'<rect x="100" y="{height - 285}" width="1700" height="185" rx="40" fill="#ffffff" fill-opacity="0.90" stroke="#cbd5e1" stroke-width="8"/>',
            f'<circle cx="210" cy="{height - 190}" r="38" fill="#16a34a"/><text x="275" y="{height - 160}" font-family="Microsoft YaHei,Noto Sans CJK SC,sans-serif" font-size="76" fill="#334155">起点</text>',
            f'<circle cx="670" cy="{height - 190}" r="34" fill="#2563eb"/><text x="735" y="{height - 160}" font-family="Microsoft YaHei,Noto Sans CJK SC,sans-serif" font-size="76" fill="#334155">路径节点</text>',
            f'<circle cx="1330" cy="{height - 190}" r="38" fill="#dc2626"/><text x="1395" y="{height - 160}" font-family="Microsoft YaHei,Noto Sans CJK SC,sans-serif" font-size="76" fill="#334155">终点</text>',
            '</svg>',
        ]
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8")
    try:
        provider = NavigationSandboxSceneProvider(
            args.base_scene,
            args.sandbox,
            args.bindings,
        )
        scene = provider.load_scene()
        source = resolve_location(provider, scene, args.source)
        destination = resolve_location(provider, scene, args.destination)
        scene = move_robot(scene, source)

        task = AuthorizedTask(
            schema_version="0.2",
            evaluation_id="manual-navigation-check",
            request_id="manual-navigation-check",
            action_type="NAVIGATE_TO_LOCATION",
            target={"kind": "LOCATION", "id": destination},
            resource=None,
            parameters={"destination": destination, "navigation_mode": "GUIDED"},
            authorization_scope="ALLOW_TO_ENTER_PLANNING",
            obligations=("REQUIRE_ROBOGUARD_EXECUTION_CHECK",),
            matched_rules=(),
            policy_version="local-manual-check",
            context_id=scene.snapshot_id,
            raw_text=f"从{args.source}前往{args.destination}",
        )
        view = build_navigation_view(scene, task)
        destination_ref = view.payload["task"]["destination_region_ref"]
        actions = adapt_spine_plan([("goto", destination_ref)])
        temporal_guard = MedicalTemporalGuard(
            mode=args.temporal_mode,
            rules_path=args.rules,
            model=args.model,
        )
        result = NavigationExecutionGuard(temporal_guard=temporal_guard).review_plan(
            scene=scene,
            authorized_task=task,
            planner_view=view,
            actions=actions,
        )
        if not result.allowed or not result.checked_routes:
            print(f"RoboGuard: {result.decision} / {result.reason_code}")
            print(result.message)
            return 3

        route = result.checked_routes[0]
        references = tuple(route.regions)
        node_ids = [view.resolve_reference(reference) for reference in references]
        costs = route_costs(view, references)
        floorplan_image = args.floorplan_image or (
            args.sandbox
            / "artifacts"
            / "floorplan"
            / "source"
            / "hospital_1f_floorplan.jpg"
        )
        floorplan_anchors = args.floorplan_anchors or (
            args.sandbox
            / "artifacts"
            / "floorplan"
            / "hospital_1f.floorplan_anchor_mapping.json"
        )
        floorplan_edge_routes = args.floorplan_edge_routes or (
            args.sandbox
            / "artifacts"
            / "floorplan"
            / "hospital_1f.edge_routes.json"
        )
        render_svg(
            scene,
            view,
            route,
            costs,
            args.output,
            floorplan_image,
            floorplan_anchors,
            floorplan_edge_routes,
            args.sandbox / "maps" / "hospital_1f.navigation.json",
        )
        coordinate_mapping = json.loads(
            (args.sandbox / "maps/hospital_1f.coordinate_mapping.json").read_text(
                encoding="utf-8"
            )
        )["node_coordinates"]
        names = {
            node_id: region.get("display_name", node_id)
            for node_id, region in scene.data["regions"].items()
        }
        maneuvers = generate_turn_by_turn(
            node_ids,
            costs,
            {
                node_id: coordinate_mapping[node_id]["usd_xyz_m"]
                for node_id in node_ids
            },
            initial_yaw_deg=float(
                coordinate_mapping[node_ids[0]].get("yaw_deg", 0.0)
            ),
            names=names,
        )

        print(f"RoboGuard: {result.decision} / {result.reason_code}")
        print("规划算法：Dijkstra（按 navigation.json 的 distance_m 累计）")
        print("路径：")
        for index, node_id in enumerate(node_ids, start=1):
            name = scene.data["regions"][node_id].get("display_name", node_id)
            print(f"  {index}. {name} ({node_id})")
        print("分段距离：")
        for left, right, cost in zip(node_ids, node_ids[1:], costs):
            print(f"  {left} -> {right}: {cost:.2f} m")
        print(f"总距离：{sum(costs):.2f} m")
        print("USD三维坐标：")
        for node_id in node_ids:
            print(
                f"  {node_id}: "
                f"{coordinate_mapping[node_id]['usd_xyz_m']}"
            )
        print("中文转向指令：")
        for index, maneuver in enumerate(maneuvers, start=1):
            print(f"  {index}. {maneuver.text_zh}")
        print(f"路径图：{args.output.resolve()}")
        if args.show_ltl:
            print("命中的医疗导航规则：")
            for rule in result.matched_natural_language_rules:
                print(f"  - {rule}")
            print("检查的LTL：")
            for formula in result.checked_ltl:
                print(f"  - {formula}")
            print("时序轨迹：")
            for step, allowed in result.temporal_trace:
                print(f"  - {step}: {'ALLOW' if allowed else 'REJECT'}")

        if args.open:
            if os.name != "nt":
                print("--open 当前只支持 Windows；请手动打开上面的 SVG。")
            else:
                os.startfile(args.output.resolve())
        return 0
    except (MedicalSceneError, OSError, ValueError) as error:
        print(f"验证失败：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
