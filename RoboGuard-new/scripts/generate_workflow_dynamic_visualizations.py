#!/usr/bin/env python3
"""Generate workflow and dynamic-replanning visuals from current RoboGuard code."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import matplotlib.pyplot as plt
from matplotlib import patheffects
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT_ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from roboguard.medical.joint_planner import WorkflowAutomaton  # noqa: E402
from scripts.generate_pareto_case_visualizations import (  # noqa: E402
    configure_fonts,
    load_geometry,
    route_polyline,
)
from scripts.run_planner_benchmark import (  # noqa: E402
    PROJECT_ROOT as BENCHMARK_ROOT,
    DynamicEventSet,
    MedicalTemporalGuard,
    NavigationExecutionGuard,
    NavigationSandboxSceneProvider,
    actions_for,
    build_navigation_view,
    scenario_scene,
    task_for,
)


OUT = PROJECT_ROOT / "docs" / "figures" / "workflow_dynamic_case"
FLOORPLAN = (
    PROJECT_ROOT
    / "navigation_sandbox"
    / "artifacts"
    / "floorplan"
    / "source"
    / "hospital_1f_floorplan.jpg"
)
NAVY = "#123A63"
BLUE = "#2563EB"
GREEN = "#198754"
PURPLE = "#7C3AED"
ORANGE = "#D97706"
RED = "#C0392B"
GRAY = "#64748B"


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def spine_drawio() -> str:
    boxes = [
        ("a1", 70, "goto(registration)", "前往第一目的地"),
        ("a2", 390, "confirm(registration)", "完成阶段确认"),
        ("a3", 710, "goto(exam)", "前往最终目的地"),
        ("a4", 1030, "handoff", "完成任务交接"),
        ("a5", 1350, "finish", "结束并提交任务"),
    ]
    states = [
        ("s1", 45, 190, "GO_TO_1"),
        ("s2", 315, 220, "WAIT_CONFIRM_1"),
        ("s3", 600, 190, "GO_TO_2"),
        ("s4", 870, 210, "WAIT_HANDOFF"),
        ("s5", 1160, 230, "READY_TO_COMPLETE"),
        ("s6", 1460, 170, "COMPLETE"),
    ]
    cells = [
        '<mxCell id="bg" value="" style="rounded=1;arcSize=3;fillColor=#F4F8FC;strokeColor=none;" vertex="1" parent="1"><mxGeometry x="25" y="25" width="1630" height="710" as="geometry"/></mxCell>',
        '<mxCell id="title" value="SPINE高层动作序列与RoboGuard工作流状态转换" style="text;html=1;strokeColor=none;fillColor=none;align=center;verticalAlign=middle;fontColor=#123A63;fontSize=27;fontStyle=1;" vertex="1" parent="1"><mxGeometry x="240" y="45" width="1200" height="50" as="geometry"/></mxCell>',
        '<mxCell id="sub" value="任务：挂号点确认 → 检查点到达 → 完成交接 → 正式结束" style="text;html=1;strokeColor=none;fillColor=none;align=center;verticalAlign=middle;fontColor=#4C657D;fontSize=16;fontStyle=1;" vertex="1" parent="1"><mxGeometry x="400" y="100" width="880" height="36" as="geometry"/></mxCell>',
    ]
    for index, (cell_id, x, title, detail) in enumerate(boxes, start=1):
        cells.append(
            f'<mxCell id="{cell_id}" value="{index}. {title}&lt;br&gt;&lt;br&gt;{detail}" style="rounded=1;arcSize=8;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#4C88C7;strokeWidth=2;fontColor=#123A63;fontSize=16;fontStyle=1;align=center;verticalAlign=middle;" vertex="1" parent="1"><mxGeometry x="{x}" y="185" width="230" height="160" as="geometry"/></mxCell>'
        )
    arrow_points = [(301, 265, 389, 265), (621, 265, 709, 265), (941, 265, 1029, 265), (1261, 265, 1349, 265)]
    for index, (x1, y1, x2, y2) in enumerate(arrow_points, start=1):
        cells.append(
            f'<mxCell id="ea{index}" value="" style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;strokeColor=#245E99;strokeWidth=3;endArrow=block;endFill=1;endSize=7;" edge="1" parent="1"><mxGeometry relative="1" as="geometry"><mxPoint x="{x1}" y="{y1}" as="sourcePoint"/><mxPoint x="{x2}" y="{y2}" as="targetPoint"/></mxGeometry></mxCell>'
        )
    cells.append('<mxCell id="state_label" value="联合状态空间中的工作流状态" style="text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;fontColor=#123A63;fontSize=16;fontStyle=1;" vertex="1" parent="1"><mxGeometry x="70" y="405" width="360" height="36" as="geometry"/></mxCell>')
    for cell_id, x, width, label in states:
        fill = "#E7F7EE" if label == "COMPLETE" else "#E8F2FD"
        stroke = "#3B9962" if label == "COMPLETE" else "#5B8DB8"
        cells.append(
            f'<mxCell id="{cell_id}" value="{label}" style="rounded=1;arcSize=50;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};strokeWidth=1.5;fontColor=#123A63;fontSize=15;fontStyle=1;align=center;verticalAlign=middle;" vertex="1" parent="1"><mxGeometry x="{x}" y="470" width="{width}" height="60" as="geometry"/></mxCell>'
        )
    state_arrows = [(236, 500, 314, 500), (536, 500, 599, 500), (791, 500, 869, 500), (1081, 500, 1159, 500), (1391, 500, 1459, 500)]
    for index, (x1, y1, x2, y2) in enumerate(state_arrows, start=1):
        cells.append(
            f'<mxCell id="es{index}" value="" style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;strokeColor=#5B8DB8;strokeWidth=2;endArrow=block;endFill=1;endSize=6;" edge="1" parent="1"><mxGeometry relative="1" as="geometry"><mxPoint x="{x1}" y="{y1}" as="sourcePoint"/><mxPoint x="{x2}" y="{y2}" as="targetPoint"/></mxGeometry></mxCell>'
        )
    cells.append('<mxCell id="note" value="顺序约束：必须先到达并确认第一目的地，再到达最终目的地；交接完成后才允许finish。" style="rounded=1;arcSize=20;whiteSpace=wrap;html=1;fillColor=#FFF8E8;strokeColor=#E5B24B;strokeWidth=1.5;fontColor=#7B5514;fontSize=16;fontStyle=1;align=center;verticalAlign=middle;" vertex="1" parent="1"><mxGeometry x="250" y="610" width="1180" height="70" as="geometry"/></mxCell>')
    return f'''<mxfile host="app.diagrams.net" agent="Codex" version="24.7.17"><diagram id="spine-actions" name="SPINE动作序列"><mxGraphModel dx="1680" dy="760" grid="0" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="1680" pageHeight="760" math="0" shadow="0"><root><mxCell id="0"/><mxCell id="1" parent="0"/>{''.join(cells)}</root></mxGraphModel></diagram></mxfile>'''


def workflow_drawio() -> str:
    cells = [
        '<mxCell id="bg" value="" style="rounded=1;arcSize=3;fillColor=#F6F9FC;strokeColor=none;" vertex="1" parent="1"><mxGeometry x="25" y="25" width="1630" height="830" as="geometry"/></mxCell>',
        '<mxCell id="title" value="正常工作流与违反LTL规则工作流的验证结果" style="text;html=1;strokeColor=none;fillColor=none;align=center;verticalAlign=middle;fontColor=#123A63;fontSize=27;fontStyle=1;" vertex="1" parent="1"><mxGeometry x="260" y="40" width="1160" height="55" as="geometry"/></mxCell>',
        '<mxCell id="left_bg" value="" style="rounded=1;arcSize=4;fillColor=#F0FAF4;strokeColor=none;" vertex="1" parent="1"><mxGeometry x="70" y="125" width="720" height="670" as="geometry"/></mxCell>',
        '<mxCell id="right_bg" value="" style="rounded=1;arcSize=4;fillColor=#FFF3F2;strokeColor=none;" vertex="1" parent="1"><mxGeometry x="890" y="125" width="720" height="670" as="geometry"/></mxCell>',
        '<mxCell id="left_title" value="正常工作流｜VALID" style="text;html=1;strokeColor=none;fillColor=none;align=center;verticalAlign=middle;fontColor=#198754;fontSize=22;fontStyle=1;" vertex="1" parent="1"><mxGeometry x="220" y="145" width="420" height="42" as="geometry"/></mxCell>',
        '<mxCell id="right_title" value="违反顺序约束｜INVALID" style="text;html=1;strokeColor=none;fillColor=none;align=center;verticalAlign=middle;fontColor=#C0392B;fontSize=22;fontStyle=1;" vertex="1" parent="1"><mxGeometry x="1040" y="145" width="420" height="42" as="geometry"/></mxCell>',
    ]
    normal = [
        ("n1", 205, "ARRIVE:registration", "GO_TO_1 → WAIT_CONFIRM_1"),
        ("n2", 305, "CONFIRM:registration", "WAIT_CONFIRM_1 → GO_TO_2"),
        ("n3", 405, "ARRIVE:exam", "GO_TO_2 → WAIT_HANDOFF"),
        ("n4", 505, "HANDOFF", "WAIT_HANDOFF → READY_TO_COMPLETE"),
        ("n5", 605, "FINISH", "READY_TO_COMPLETE → COMPLETE"),
    ]
    for cell_id, y, action, state in normal:
        cells.append(f'<mxCell id="{cell_id}" value="✓ {action}&lt;br&gt;{state}" style="rounded=1;arcSize=8;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#46A16B;strokeWidth=1.8;fontColor=#205C38;fontSize=16;fontStyle=1;align=center;verticalAlign=middle;" vertex="1" parent="1"><mxGeometry x="165" y="{y}" width="530" height="70" as="geometry"/></mxCell>')
    for index, y in enumerate([276, 376, 476, 576], start=1):
        cells.append(f'<mxCell id="en{index}" value="" style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;strokeColor=#46A16B;strokeWidth=2;endArrow=block;endFill=1;endSize=6;" edge="1" parent="1"><mxGeometry relative="1" as="geometry"><mxPoint x="430" y="{y}" as="sourcePoint"/><mxPoint x="430" y="{y+28}" as="targetPoint"/></mxGeometry></mxCell>')
    cells.append('<mxCell id="normal_result" value="最终状态：COMPLETE&lt;br&gt;全部5个状态转换均被接受" style="rounded=1;arcSize=15;whiteSpace=wrap;html=1;fillColor=#DFF3E7;strokeColor=#198754;strokeWidth=2;fontColor=#185C36;fontSize=16;fontStyle=1;align=center;verticalAlign=middle;" vertex="1" parent="1"><mxGeometry x="220" y="700" width="420" height="70" as="geometry"/></mxCell>')
    invalid = [
        ("v1", 245, "✕ ARRIVE:exam", "WF-001：期望先到registration"),
        ("v2", 405, "✕ FINISH", "WF-006：尚未完成阶段与交接"),
    ]
    for cell_id, y, action, detail in invalid:
        cells.append(f'<mxCell id="{cell_id}" value="{action}&lt;br&gt;{detail}&lt;br&gt;状态保持 GO_TO_1" style="rounded=1;arcSize=8;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#D26058;strokeWidth=2;fontColor=#7B302B;fontSize=16;fontStyle=1;align=center;verticalAlign=middle;" vertex="1" parent="1"><mxGeometry x="985" y="{y}" width="530" height="105" as="geometry"/></mxCell>')
    cells.extend([
        '<mxCell id="ev" value="" style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;strokeColor=#D26058;strokeWidth=2;endArrow=block;endFill=1;endSize=6;dashed=1;" edge="1" parent="1"><mxGeometry relative="1" as="geometry"><mxPoint x="1250" y="351" as="sourcePoint"/><mxPoint x="1250" y="404" as="targetPoint"/></mxGeometry></mxCell>',
        '<mxCell id="rules" value="触发的LTL/工作流约束&lt;br&gt;&lt;br&gt;(!arrive₂) U arrive₁&lt;br&gt;G(finish → handoff_complete)" style="rounded=1;arcSize=8;whiteSpace=wrap;html=1;fillColor=#FFF8E8;strokeColor=#E5B24B;strokeWidth=1.5;fontColor=#7B5514;fontSize=16;fontStyle=1;align=center;verticalAlign=middle;" vertex="1" parent="1"><mxGeometry x="1035" y="555" width="430" height="120" as="geometry"/></mxCell>',
        '<mxCell id="invalid_result" value="最终状态：GO_TO_1&lt;br&gt;验证结果：INVALID / 不签发执行许可证" style="rounded=1;arcSize=15;whiteSpace=wrap;html=1;fillColor=#FADFDB;strokeColor=#C0392B;strokeWidth=2;fontColor=#7B302B;fontSize=16;fontStyle=1;align=center;verticalAlign=middle;" vertex="1" parent="1"><mxGeometry x="1005" y="700" width="490" height="70" as="geometry"/></mxCell>',
    ])
    return f'''<mxfile host="app.diagrams.net" agent="Codex" version="24.7.17"><diagram id="workflow-validation" name="工作流验证"><mxGraphModel dx="1680" dy="880" grid="0" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="1680" pageHeight="880" math="0" shadow="0"><root><mxCell id="0"/><mxCell id="1" parent="0"/>{''.join(cells)}</root></mxGraphModel></diagram></mxfile>'''


def load_scenarios():
    return json.loads(
        (PROJECT_ROOT / "benchmarks" / "scenarios.json").read_text(encoding="utf-8")
    )["scenarios"]


def run_dynamic_case(base_scene, scenarios, scenario_id, blocked):
    scenario = next(item for item in scenarios if item["id"] == scenario_id)
    scene = scenario_scene(base_scene, scenario)
    task = task_for(scene, scenario)
    view = build_navigation_view(scene, task)
    actions = actions_for(view)
    baseline = NavigationExecutionGuard(
        temporal_guard=MedicalTemporalGuard(mode="off"), planner="pareto"
    ).review_plan(
        scene=scene,
        authorized_task=task,
        planner_view=view,
        actions=actions,
    )
    events = DynamicEventSet.from_mapping(
        {
            "plan_version": 2,
            "events": [{"type": "NODE_BLOCKED", "region_id": blocked}],
        }
    )
    replanned = NavigationExecutionGuard(
        temporal_guard=MedicalTemporalGuard(mode="off"),
        planner="pareto",
        events=events,
    ).review_plan(
        scene=scene,
        authorized_task=task,
        planner_view=view,
        actions=actions,
        previous_plan=baseline.planning_result,
    )
    old_route = [view.resolve_reference(item) for item in baseline.planning_result.route]
    checked = replanned.checked_routes[0] if replanned.checked_routes else None
    new_route = [view.resolve_reference(item) for item in checked.regions] if checked else []
    prefix_length = 0
    while (
        prefix_length < min(len(old_route), len(new_route))
        and old_route[prefix_length] == new_route[prefix_length]
    ):
        prefix_length += 1
    return {
        "scenario_id": scenario_id,
        "source": scenario["source"],
        "destinations": scenario["ordered_destinations"],
        "blocked_node": blocked,
        "old_route": old_route,
        "new_route": new_route,
        "preserved_prefix": old_route[:prefix_length],
        "decision": replanned.decision,
        "reason_code": replanned.reason_code,
        "plan_decision": replanned.planning_result.decision,
        "plan_reason_code": replanned.planning_result.reason_code,
        "plan_message": replanned.planning_result.message,
        "planner": checked.planner if checked else None,
        "plan_version": checked.plan_version if checked else 2,
        "certificate_source": (
            checked.safety_certificate.get("source_type") if checked else None
        ),
        "permit_issued": bool(checked and checked.execution_authorized),
        "risk": checked.risk_score if checked else None,
        "distance_m": checked.distance_m if checked else None,
        "estimated_time_s": checked.estimated_time_s if checked else None,
    }


def bounds_for(routes, anchor, edge_routes, edge_lookup, floorplan, margin=320):
    points = [
        point
        for route in routes
        if route
        for point in route_polyline(route, anchor, edge_routes, edge_lookup)
    ]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (
        max(0, min(xs) - margin),
        max(0, min(ys) - margin),
        min(floorplan.width, max(xs) + margin),
        min(floorplan.height, max(ys) + margin),
    )


def map_axis(ax, floorplan, bounds):
    left, top, right, bottom = bounds
    ax.imshow(
        floorplan.crop((left, top, right, bottom)),
        extent=[left, right, bottom, top],
        alpha=0.93,
    )
    ax.set_xlim(left, right)
    ax.set_ylim(bottom, top)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_color("#AEC2D4")


def draw_route(ax, route, anchor, edge_routes, edge_lookup, color, label, *, width=5, style="-"):
    points = route_polyline(route, anchor, edge_routes, edge_lookup)
    xs, ys = zip(*points)
    line = ax.plot(xs, ys, color=color, linewidth=width, linestyle=style, label=label, solid_capstyle="round", zorder=10)[0]
    line.set_path_effects([patheffects.Stroke(linewidth=width + 3, foreground="white", alpha=0.9), patheffects.Normal()])


def mark(ax, node, anchor, color, marker, label):
    point = anchor[node]
    ax.scatter(*point, s=210, marker=marker, color=color, edgecolor="white", linewidth=2, zorder=30)
    ax.annotate(label, point, xytext=(9, -18), textcoords="offset points", fontsize=10, weight="bold", color=color)


def save(fig, name):
    path = OUT / name
    fig.savefig(path, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def before_after_figure(case, floorplan, anchor, edge_routes, edge_lookup):
    bounds = bounds_for([case["old_route"], case["new_route"]], anchor, edge_routes, edge_lookup, floorplan)
    fig, axes = plt.subplots(1, 2, figsize=(14, 7.5))
    map_axis(axes[0], floorplan, bounds)
    draw_route(axes[0], case["old_route"], anchor, edge_routes, edge_lookup, BLUE, "Version 1 原路线")
    mark(axes[0], case["old_route"][0], anchor, GREEN, "o", "起点")
    mark(axes[0], case["old_route"][-1], anchor, RED, "*", "终点")
    axes[0].set_title("封路前｜Version 1", fontsize=17, weight="bold", color=NAVY)
    axes[0].legend(loc="upper right")
    map_axis(axes[1], floorplan, bounds)
    draw_route(axes[1], case["new_route"], anchor, edge_routes, edge_lookup, GREEN, "Version 2 修复路线")
    mark(axes[1], case["blocked_node"], anchor, RED, "X", "临时封闭")
    mark(axes[1], case["new_route"][0], anchor, GREEN, "o", "起点")
    mark(axes[1], case["new_route"][-1], anchor, RED, "*", "终点")
    axes[1].set_title("封路后｜Version 2 / D* Lite", fontsize=17, weight="bold", color=NAVY)
    axes[1].legend(loc="upper right")
    fig.suptitle("图3  动态封路前后的路线对比", fontsize=21, weight="bold", color=NAVY)
    fig.tight_layout()
    return save(fig, "03_dynamic_block_before_after.png")


def prefix_figure(case, floorplan, anchor, edge_routes, edge_lookup):
    bounds = bounds_for([case["old_route"], case["new_route"]], anchor, edge_routes, edge_lookup, floorplan)
    fig, ax = plt.subplots(figsize=(14, 8))
    map_axis(ax, floorplan, bounds)
    prefix = case["preserved_prefix"]
    old_suffix = case["old_route"][max(0, len(prefix) - 1) :]
    new_suffix = case["new_route"][max(0, len(prefix) - 1) :]
    if len(old_suffix) >= 2:
        draw_route(ax, old_suffix, anchor, edge_routes, edge_lookup, GRAY, "失效的原路线后缀", width=3.5, style="--")
    if len(new_suffix) >= 2:
        draw_route(ax, new_suffix, anchor, edge_routes, edge_lookup, GREEN, "D* Lite局部修复后缀", width=5)
    if len(prefix) >= 2:
        draw_route(ax, prefix, anchor, edge_routes, edge_lookup, PURPLE, "保留的原路线前缀", width=7)
    mark(ax, case["blocked_node"], anchor, RED, "X", "封闭节点")
    mark(ax, case["old_route"][0], anchor, GREEN, "o", "起点")
    mark(ax, case["old_route"][-1], anchor, RED, "*", "终点")
    ax.legend(loc="upper right", framealpha=0.95)
    ax.set_title("图4  D* Lite保留原路线前缀的局部修复结果", fontsize=20, pad=16)
    ax.text(
        0.02,
        0.02,
        f"保留前缀：{' → '.join(prefix)}\n新计划版本：{case['plan_version']}｜许可证来源：{case['certificate_source']}",
        transform=ax.transAxes,
        fontsize=10.5,
        weight="bold",
        bbox=dict(boxstyle="round,pad=0.55", facecolor="white", edgecolor="#B6C9DB", alpha=0.94),
    )
    return save(fig, "04_dstar_preserved_prefix.png")


def pause_figure(case, floorplan, anchor, edge_routes, edge_lookup):
    bounds = bounds_for([case["old_route"]], anchor, edge_routes, edge_lookup, floorplan)
    fig = plt.figure(figsize=(14, 7.8))
    grid = fig.add_gridspec(1, 2, width_ratios=[1.45, 1])
    ax = fig.add_subplot(grid[0, 0])
    map_axis(ax, floorplan, bounds)
    draw_route(ax, case["old_route"], anchor, edge_routes, edge_lookup, GRAY, "Version 1 已失效路线", width=4, style="--")
    mark(ax, case["blocked_node"], anchor, RED, "X", "封闭且无安全替代")
    mark(ax, case["old_route"][0], anchor, GREEN, "o", "起点")
    mark(ax, case["old_route"][-1], anchor, RED, "*", "终点")
    ax.legend(loc="upper right")
    ax.set_title("无安全替代路线", fontsize=18, weight="bold", color=NAVY)

    side = fig.add_subplot(grid[0, 1])
    side.axis("off")
    side.text(0.04, 0.95, "安全暂停审计记录", fontsize=20, weight="bold", color=NAVY, va="top")
    records = [
        ("动态事件", f"NODE_BLOCKED\n{case['blocked_node']}", "#FFF1F0", RED),
        ("计划版本", "Version 1 → Version 2\n旧路线与旧许可证失效", "#FFF8E8", ORANGE),
        ("重新规划", "未产生可独立验证的替代路线", "#F3F5F7", GRAY),
        (
            "最终处置",
            f"PAUSE\n{case['plan_reason_code']}\n执行许可未签发，机器人保持安全停止",
            "#FFE8E6",
            RED,
        ),
    ]
    y = 0.80
    for title, text, fill, color in records:
        side.text(0.05, y, f"{title}\n{text}", fontsize=12.5, linespacing=1.35, va="top", bbox=dict(boxstyle="round,pad=0.62", facecolor=fill, edgecolor=color, linewidth=1.5))
        y -= 0.19
    side.text(
        0.05,
        0.035,
        "安全原则：找不到经过独立验证的替代路线时，\n系统宁可暂停，也不沿失效路线继续执行。",
        fontsize=9.8,
        weight="bold",
        color=NAVY,
        va="bottom",
        linespacing=1.3,
    )
    fig.suptitle("图5  无安全替代路线时的暂停记录", fontsize=21, weight="bold", color=NAVY)
    fig.tight_layout()
    return save(fig, "05_safe_pause_record.png")


def workflow_evidence():
    normal = WorkflowAutomaton(
        ("registration", "exam"),
        confirmation_destinations=("registration",),
        require_handoff=True,
        require_explicit_finish=True,
    )
    normal.arrive("registration")
    normal.confirm("registration")
    normal.arrive("exam")
    normal.handoff()
    normal.finish()
    invalid = WorkflowAutomaton(
        ("registration", "exam"),
        confirmation_destinations=("registration",),
        require_handoff=True,
        require_explicit_finish=True,
    )
    invalid.arrive("exam")
    invalid.finish()
    return {
        "normal": {
            "state": normal.state,
            "trace": [item.to_dict() for item in normal.trace],
            "ltlf": list(normal.ltlf_formulas()),
        },
        "violating": {
            "state": invalid.state,
            "trace": [item.to_dict() for item in invalid.trace],
            "ltlf": list(invalid.ltlf_formulas()),
        },
    }


def main() -> int:
    configure_fonts()
    OUT.mkdir(parents=True, exist_ok=True)
    write(OUT / "01_spine_action_sequence.drawio", spine_drawio())
    write(OUT / "02_ltl_workflow_validation.drawio", workflow_drawio())
    provider = NavigationSandboxSceneProvider(
        BENCHMARK_ROOT / "data" / "fixtures" / "medical" / "hospital_1f.synthetic.json",
        BENCHMARK_ROOT / "navigation_sandbox",
        BENCHMARK_ROOT / "data" / "navigation" / "hospital_1f.bindings.json",
    )
    base_scene = provider.load_scene()
    scenarios = load_scenarios()
    repair_case = run_dynamic_case(base_scene, scenarios, "normal-003", "nav-f1-spine-01")
    pause_case = run_dynamic_case(base_scene, scenarios, "normal-001", "nav-f1-spine-01")
    anchor, edge_routes, edge_lookup = load_geometry()
    floorplan = Image.open(FLOORPLAN).convert("RGB")
    outputs = [
        before_after_figure(repair_case, floorplan, anchor, edge_routes, edge_lookup),
        prefix_figure(repair_case, floorplan, anchor, edge_routes, edge_lookup),
        pause_figure(pause_case, floorplan, anchor, edge_routes, edge_lookup),
    ]
    floorplan.close()
    evidence = {
        "workflow": workflow_evidence(),
        "dynamic_repair": repair_case,
        "safe_pause": pause_case,
    }
    write(OUT / "case_evidence.json", json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output_dir": str(OUT), "raster_outputs": [str(path) for path in outputs]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
