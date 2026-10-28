#!/usr/bin/env python3
"""Generate paper-ready visualizations for RoboGuard scenario multistage-013."""

from __future__ import annotations

import copy
import csv
import json
from pathlib import Path
import sys

import matplotlib.pyplot as plt
from matplotlib import patheffects
from matplotlib.lines import Line2D
from PIL import Image, ImageDraw, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT_ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_planner_benchmark import (  # noqa: E402
    PROJECT_ROOT as BENCHMARK_ROOT,
    DynamicEventSet,
    NavigationSandboxSceneProvider,
    build_navigation_view,
    run_pipeline,
    scenario_scene,
    task_for,
)


OUT = PROJECT_ROOT / "docs" / "figures" / "pareto_case_multistage_013"
FLOORPLAN = (
    PROJECT_ROOT
    / "navigation_sandbox"
    / "artifacts"
    / "floorplan"
    / "source"
    / "hospital_1f_floorplan.jpg"
)
ANCHORS = (
    PROJECT_ROOT
    / "navigation_sandbox"
    / "artifacts"
    / "floorplan"
    / "hospital_1f.floorplan_anchor_mapping.json"
)
EDGE_ROUTES = (
    PROJECT_ROOT
    / "navigation_sandbox"
    / "artifacts"
    / "floorplan"
    / "hospital_1f.edge_routes.json"
)
NAVIGATION = (
    PROJECT_ROOT / "navigation_sandbox" / "maps" / "hospital_1f.navigation.json"
)

COLORS = ["#16A085", "#2563EB", "#F59E0B", "#DC4C64"]
LINE_STYLES = ["--", "-", "-.", ":"]
NAVY = "#123A63"
BLUE = "#2563EB"
PURPLE = "#7C3AED"
GREEN = "#198754"
ORANGE = "#D97706"
RED = "#C0392B"
LIGHT = "#F4F8FC"
GRID = "#D8E3EE"


def configure_fonts() -> None:
    plt.rcParams.update(
        {
            "font.sans-serif": [
                "Microsoft YaHei",
                "SimHei",
                "Noto Sans CJK SC",
                "Arial Unicode MS",
                "DejaVu Sans",
            ],
            "axes.unicode_minus": False,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.titleweight": "bold",
            "axes.titlecolor": NAVY,
            "text.color": NAVY,
        }
    )


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def run_variant(base_scene, base_scenario, label: str, needs: list[str]):
    scenario = copy.deepcopy(base_scenario)
    scenario["id"] = f"visual-{label}"
    scenario["accessibility_needs"] = needs
    scene = scenario_scene(base_scene, scenario)
    task = task_for(scene, scenario)
    view = build_navigation_view(scene, task)
    result, route, permit, elapsed_ms = run_pipeline(
        scene=scene,
        task=task,
        view=view,
        planner_name="pareto",
        events=DynamicEventSet.empty(),
    )
    if not result.allowed or route is None or not permit:
        raise RuntimeError(f"{label} pipeline did not produce an executable route")
    candidates = []
    for index, raw in enumerate(route.candidate_comparisons, start=1):
        item = dict(raw)
        item["id"] = f"C{index}"
        item["route_real"] = [view.resolve_reference(node) for node in item["route"]]
        candidates.append(item)
    return {
        "label": label,
        "profile": route.selection_profile,
        "selection_reason": route.selection_reason,
        "route": [view.resolve_reference(node) for node in route.regions],
        "risk": route.risk_score,
        "distance_m": route.distance_m,
        "estimated_time_s": route.estimated_time_s,
        "patient_burden": route.patient_burden,
        "candidate_count": route.candidate_count,
        "candidates": candidates,
        "pipeline_ms": elapsed_ms,
        "certificate_id": route.safety_certificate_id,
    }


def load_geometry():
    anchors_doc = load_json(ANCHORS)
    anchor = {
        item["node_id"]: tuple(float(v) for v in item["floorplan_pixel_xy"])
        for item in anchors_doc["nodes"]
    }
    route_doc = load_json(EDGE_ROUTES)
    edge_routes = {
        edge_id: [tuple(float(v) for v in point) for point in points]
        for edge_id, points in route_doc["edge_routes"].items()
    }
    nav = load_json(NAVIGATION)
    lookup = {}
    for edge in nav["edges"]:
        left, right = edge["from"], edge["to"]
        lookup[(left, right)] = (edge["id"], False)
        if edge.get("bidirectional"):
            lookup[(right, left)] = (edge["id"], True)
    return anchor, edge_routes, lookup


def route_polyline(route, anchor, edge_routes, edge_lookup):
    result = []
    for left, right in zip(route, route[1:]):
        if (left, right) not in edge_lookup:
            raise KeyError(f"No edge geometry for {left} -> {right}")
        edge_id, reverse = edge_lookup[(left, right)]
        points = [anchor[left], *edge_routes.get(edge_id, ()), anchor[right]]
        if reverse:
            points = list(reversed(points))
        if result and result[-1] == points[0]:
            result.extend(points[1:])
        else:
            result.extend(points)
    return result


def crop_bounds(routes, anchor, edge_routes, edge_lookup, floorplan):
    points = [
        point
        for route in routes
        for point in route_polyline(route, anchor, edge_routes, edge_lookup)
    ]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    margin = 340
    return (
        max(0, min(xs) - margin),
        max(0, min(ys) - margin),
        min(floorplan.width, max(xs) + margin),
        min(floorplan.height, max(ys) + margin),
    )


def setup_map_axis(ax, floorplan, bounds):
    left, top, right, bottom = bounds
    cropped = floorplan.crop((left, top, right, bottom))
    ax.imshow(cropped, extent=[left, right, bottom, top], alpha=0.93)
    ax.set_xlim(left, right)
    ax.set_ylim(bottom, top)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_color("#AFC5D8")
        spine.set_linewidth(1.2)


def draw_route(ax, points, color, *, label, linewidth=4, linestyle="-", alpha=1):
    xs, ys = zip(*points)
    line = ax.plot(
        xs,
        ys,
        color=color,
        linewidth=linewidth,
        linestyle=linestyle,
        alpha=alpha,
        solid_capstyle="round",
        label=label,
        zorder=10,
    )[0]
    line.set_path_effects(
        [patheffects.Stroke(linewidth=linewidth + 3, foreground="white", alpha=0.9), patheffects.Normal()]
    )
    return line


def mark_endpoints(ax, route, anchor, *, include_mid=True):
    start = anchor[route[0]]
    end = anchor[route[-1]]
    ax.scatter(*start, s=150, marker="o", color=GREEN, edgecolor="white", linewidth=2, zorder=30)
    ax.scatter(*end, s=180, marker="*", color=RED, edgecolor="white", linewidth=1.5, zorder=30)
    if include_mid and "nav-f1-corridor-19" in route:
        mid = anchor["nav-f1-corridor-19"]
        ax.scatter(*mid, s=140, marker="D", color=ORANGE, edgecolor="white", linewidth=2, zorder=30)
    ax.annotate("起点", start, xytext=(8, -18), textcoords="offset points", fontsize=10, weight="bold", color=GREEN)
    ax.annotate("终点", end, xytext=(8, -18), textcoords="offset points", fontsize=10, weight="bold", color=RED)


def save(fig, filename):
    path = OUT / filename
    fig.savefig(path, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def candidate_map(data, floorplan, bounds, anchor, edge_routes, edge_lookup):
    fig, ax = plt.subplots(figsize=(14, 8))
    setup_map_axis(ax, floorplan, bounds)
    for index, candidate in enumerate(data["candidates"]):
        selected = candidate["selected"]
        points = route_polyline(candidate["route_real"], anchor, edge_routes, edge_lookup)
        draw_route(
            ax,
            points,
            COLORS[index],
            label=(
                f"{candidate['id']}  风险 {candidate['risk']:.3f} / "
                f"距离 {candidate['distance_m']:.2f} m"
                + ("（选中）" if selected else "")
            ),
            linewidth=6 if selected else 3.5,
            linestyle=LINE_STYLES[index],
            alpha=1 if selected else 0.82,
        )
    mark_endpoints(ax, data["candidates"][0]["route_real"], anchor)
    ax.set_title("图1  多条 Pareto 候选路线对比｜multistage-013", fontsize=20, pad=16)
    ax.text(
        0.01,
        0.02,
        "任务：走廊14 → 经走廊19 → 主通道1    蓝色 C2 为普通导诊最终选中路线",
        transform=ax.transAxes,
        fontsize=11,
        weight="bold",
        color=NAVY,
        bbox=dict(boxstyle="round,pad=0.55", facecolor="white", edgecolor="#B8CCE0", alpha=0.94),
        zorder=40,
    )
    ax.legend(loc="upper right", frameon=True, framealpha=0.95, fontsize=10)
    return save(fig, "01_pareto_candidate_routes.png")


def metrics_figure(data):
    candidates = data["candidates"]
    fig = plt.figure(figsize=(14, 7.8))
    grid = fig.add_gridspec(2, 2, height_ratios=[1.05, 1.35], width_ratios=[1.35, 1])
    ax_table = fig.add_subplot(grid[0, :])
    ax_table.axis("off")
    rows = []
    for c in candidates:
        status = "最终选中" if c["selected"] else ("不满足距离预算" if not c["eligible"] else "合规但未选")
        rows.append(
            [
                c["id"],
                f"{c['risk']:.3f}",
                f"{c['distance_m']:.2f}",
                f"{c['estimated_time_s']:.2f}",
                f"{c['patient_burden']:.1f}",
                status,
            ]
        )
    table = ax_table.table(
        cellText=rows,
        colLabels=["候选", "风险分", "距离/m", "时间/s", "患者负担", "普通导诊结果"],
        cellLoc="center",
        colLoc="center",
        loc="center",
        colWidths=[0.09, 0.12, 0.12, 0.12, 0.14, 0.28],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.scale(1, 1.7)
    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor("#C5D6E5")
        if row == 0:
            cell.set_facecolor("#DDEEFF")
            cell.set_text_props(weight="bold", color=NAVY)
        elif rows[row - 1][-1] == "最终选中":
            cell.set_facecolor("#E8F1FF")
        elif rows[row - 1][-1] == "不满足距离预算":
            cell.set_facecolor("#FFF3E4")
        else:
            cell.set_facecolor("#F8FAFC")
    ax_table.set_title("图2  Pareto候选路线指标表与权衡关系", fontsize=20, pad=14)

    ax_scatter = fig.add_subplot(grid[1, 0])
    for index, c in enumerate(candidates):
        marker = "*" if c["selected"] else ("X" if not c["eligible"] else "o")
        size = 280 if c["selected"] else 150
        ax_scatter.scatter(c["distance_m"], c["risk"], s=size, marker=marker, color=COLORS[index], edgecolor="white", linewidth=1.5, zorder=3)
        ax_scatter.annotate(c["id"], (c["distance_m"], c["risk"]), xytext=(7, 7), textcoords="offset points", weight="bold")
    ax_scatter.axvline(129.475, color=ORANGE, linestyle="--", linewidth=1.8, label="普通导诊距离预算 129.475 m")
    ax_scatter.set_xlabel("路线距离（m）")
    ax_scatter.set_ylabel("路线风险分（越低越好）")
    ax_scatter.grid(True, color=GRID, linewidth=0.8)
    ax_scatter.legend(loc="upper left", fontsize=9)
    ax_scatter.set_title("风险—距离 Pareto 权衡")

    ax_bar = fig.add_subplot(grid[1, 1])
    ids = [c["id"] for c in candidates]
    y = list(range(len(ids)))
    times = [c["estimated_time_s"] for c in candidates]
    distances = [c["distance_m"] for c in candidates]
    ax_bar.barh([v - 0.17 for v in y], distances, height=0.32, color="#6EA8FE", label="距离/m")
    ax_bar.barh([v + 0.17 for v in y], times, height=0.32, color="#9BD0C0", label="时间/s")
    ax_bar.set_yticks(y, ids)
    ax_bar.invert_yaxis()
    ax_bar.grid(axis="x", color=GRID, linewidth=0.8)
    ax_bar.legend(loc="lower right")
    ax_bar.set_title("距离与时间对比")
    ax_bar.text(
        0.01,
        -0.2,
        "注：本场景各候选患者负担均为0.0，说明当前地图尚未形成差异化负担信号。",
        transform=ax_bar.transAxes,
        fontsize=9,
        color="#5B6B7A",
    )
    fig.tight_layout()
    return save(fig, "02_candidate_metrics.png")


def shortest_vs_selected(data, floorplan, bounds, anchor, edge_routes, edge_lookup):
    selected = next(c for c in data["candidates"] if c["selected"])
    shortest = min(data["candidates"], key=lambda c: c["distance_m"])
    fig = plt.figure(figsize=(14, 7.8))
    grid = fig.add_gridspec(1, 2, width_ratios=[1.55, 1])
    ax = fig.add_subplot(grid[0, 0])
    setup_map_axis(ax, floorplan, bounds)
    draw_route(ax, route_polyline(shortest["route_real"], anchor, edge_routes, edge_lookup), RED, label=f"最短路线 {shortest['id']}", linewidth=4, linestyle="--")
    draw_route(ax, route_polyline(selected["route_real"], anchor, edge_routes, edge_lookup), BLUE, label=f"最终选中 {selected['id']}", linewidth=6)
    mark_endpoints(ax, selected["route_real"], anchor)
    ax.legend(loc="upper right", framealpha=0.95)
    ax.set_title("图3  最短路线与最终选中路线", fontsize=18, pad=14)

    right = fig.add_subplot(grid[0, 1])
    right.axis("off")
    right.text(0.02, 0.95, "为什么没有选择最短路线？", fontsize=18, weight="bold", color=NAVY, va="top")
    cards = [
        ("最短路线 C4", f"距离 {shortest['distance_m']:.2f} m\n时间 {shortest['estimated_time_s']:.2f} s\n风险 {shortest['risk']:.3f}", "#FFF0F1", RED),
        ("选中路线 C2", f"距离 {selected['distance_m']:.2f} m\n时间 {selected['estimated_time_s']:.2f} s\n风险 {selected['risk']:.3f}", "#EAF2FF", BLUE),
        (
            "安全收益",
            f"多行驶 {selected['distance_m'] - shortest['distance_m']:.2f} m\n多用时 {selected['estimated_time_s'] - shortest['estimated_time_s']:.2f} s\n风险降低 {shortest['risk'] - selected['risk']:.3f}（{(shortest['risk'] - selected['risk']) / shortest['risk'] * 100:.1f}%）",
            "#EAF8EF",
            GREEN,
        ),
    ]
    y = 0.79
    for title, body, fill, color in cards:
        right.text(
            0.04,
            y,
            f"{title}\n{body}",
            fontsize=13,
            linespacing=1.45,
            va="top",
            bbox=dict(boxstyle="round,pad=0.7", facecolor=fill, edgecolor=color, linewidth=1.6),
        )
        y -= 0.27
    right.text(
        0.04,
        0.06,
        "选择逻辑：先限制可接受的距离增量，再从合规候选中优先降低风险。",
        fontsize=11,
        weight="bold",
        color=NAVY,
        wrap=True,
    )
    fig.tight_layout()
    return save(fig, "03_shortest_vs_selected.png")


def routine_vs_wheelchair(routine, wheelchair, floorplan, bounds, anchor, edge_routes, edge_lookup):
    fig = plt.figure(figsize=(14, 7.8))
    grid = fig.add_gridspec(1, 2, width_ratios=[1.55, 1])
    ax = fig.add_subplot(grid[0, 0])
    setup_map_axis(ax, floorplan, bounds)
    draw_route(ax, route_polyline(routine["route"], anchor, edge_routes, edge_lookup), BLUE, label="普通导诊：C2", linewidth=5)
    draw_route(ax, route_polyline(wheelchair["route"], anchor, edge_routes, edge_lookup), PURPLE, label="轮椅任务：C1", linewidth=5, linestyle="--")
    mark_endpoints(ax, routine["route"], anchor)
    ax.legend(loc="upper right", framealpha=0.95)
    ax.set_title("图4  普通导诊与轮椅任务的选路差异", fontsize=18, pad=14)

    side = fig.add_subplot(grid[0, 1])
    side.axis("off")
    columns = ["任务画像", "普通导诊", "轮椅任务"]
    rows = [
        ["选择策略", "风险优先\n受距离预算约束", "患者负担优先\n受距离预算约束"],
        ["最终候选", "C2", "C1"],
        ["风险分", f"{routine['risk']:.3f}", f"{wheelchair['risk']:.3f}"],
        ["距离/m", f"{routine['distance_m']:.2f}", f"{wheelchair['distance_m']:.2f}"],
        ["时间/s", f"{routine['estimated_time_s']:.2f}", f"{wheelchair['estimated_time_s']:.2f}"],
        ["患者负担", f"{routine['patient_burden']:.1f}", f"{wheelchair['patient_burden']:.1f}"],
        ["合规候选", "3/4", "4/4"],
    ]
    table = side.table(cellText=rows, colLabels=columns, cellLoc="center", colLoc="center", loc="center", colWidths=[0.3, 0.34, 0.34])
    table.auto_set_font_size(False)
    table.set_fontsize(10.5)
    table.scale(1, 2.05)
    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor("#C5D6E5")
        if row == 0:
            cell.set_facecolor("#DDEEFF")
            cell.set_text_props(weight="bold")
        elif col == 1:
            cell.set_facecolor("#EFF5FF")
        elif col == 2:
            cell.set_facecolor("#F4EEFF")
        else:
            cell.set_facecolor("#F8FAFC")
    side.text(
        0.01,
        0.08,
        "本例负担值均为0，因此轮椅选择差异主要来自任务画像的候选资格与确定性排序；不可将其解释为已测得的负担降低。",
        fontsize=9.5,
        color="#5B6B7A",
        wrap=True,
    )
    fig.tight_layout()
    return save(fig, "04_routine_vs_wheelchair.png")


def audit_figure(routine, wheelchair):
    fig, ax = plt.subplots(figsize=(14, 8))
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 8)
    ax.axis("off")
    ax.set_title("图5  路线选择原因与未选候选审计记录", fontsize=20, pad=18)
    ax.text(0.35, 7.35, "普通导诊（ROUTINE）", fontsize=16, weight="bold", color=BLUE)
    ax.text(7.25, 7.35, "轮椅任务（WHEELCHAIR）", fontsize=16, weight="bold", color=PURPLE)
    ax.text(0.35, 6.85, "risk_within_distance_ratio｜3/4 候选合规｜选择 C2", fontsize=11, color=NAVY)
    ax.text(7.25, 6.85, "burden_within_distance_ratio｜4/4 候选合规｜选择 C1", fontsize=11, color=NAVY)

    for index, base in enumerate(routine["candidates"]):
        wheel = wheelchair["candidates"][index]
        y = 5.95 - index * 1.35
        left_reason = base["exclusion_reason"]
        if left_reason.startswith("distance exceeds"):
            left_reason = "距离超过129.475 m预算"
        left_status = "选中" if base["selected"] else (left_reason or "合规但未选")
        right_status = "选中" if wheel["selected"] else (wheel["exclusion_reason"] or "合规但未选")
        left_color = GREEN if base["selected"] else (ORANGE if not base["eligible"] else "#6B7280")
        right_color = GREEN if wheel["selected"] else (ORANGE if not wheel["eligible"] else "#6B7280")
        metrics = f"风险 {base['risk']:.3f}｜距离 {base['distance_m']:.2f}m｜时间 {base['estimated_time_s']:.2f}s｜负担 {base['patient_burden']:.1f}"
        ax.text(0.35, y, f"{base['id']}  {metrics}\n结论：{left_status}", fontsize=10.5, va="top", linespacing=1.4, bbox=dict(boxstyle="round,pad=0.55", facecolor="#F8FAFC", edgecolor=left_color, linewidth=1.8))
        ax.text(7.25, y, f"{wheel['id']}  {metrics}\n结论：{right_status}", fontsize=10.5, va="top", linespacing=1.4, bbox=dict(boxstyle="round,pad=0.55", facecolor="#F8FAFC", edgecolor=right_color, linewidth=1.8))
    ax.text(
        0.35,
        0.68,
        "普通导诊关键原因：\nC1虽风险最低，但距离超过129.475 m预算；\n在剩余候选中，C2风险最低。",
        fontsize=10.5,
        weight="bold",
        color=NAVY,
        va="top",
        linespacing=1.35,
    )
    ax.text(
        7.25,
        0.68,
        "轮椅任务关键原因：\n4条路线均通过画像约束；负担并列时，\n确定性排序选出风险最低的C1。",
        fontsize=10.5,
        weight="bold",
        color=NAVY,
        va="top",
        linespacing=1.35,
    )
    return save(fig, "05_selection_audit.png")


def make_overview(paths):
    images = [Image.open(path).convert("RGB") for path in paths[:4]]
    thumb_w, thumb_h = 1200, 680
    canvas = Image.new("RGB", (thumb_w * 2 + 90, thumb_h * 2 + 170), "white")
    draw = ImageDraw.Draw(canvas)
    try:
        title_font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", 44)
        label_font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 28)
    except OSError:
        title_font = ImageFont.load_default()
        label_font = ImageFont.load_default()
    draw.text((45, 30), "RoboGuard Pareto路线选择案例可视化总览", fill=NAVY, font=title_font)
    labels = ["A 多候选路线", "B 指标与Pareto权衡", "C 最短路线与最终路线", "D 普通导诊与轮椅任务"]
    for index, image in enumerate(images):
        image.thumbnail((thumb_w, thumb_h - 45), Image.Resampling.LANCZOS)
        col, row = index % 2, index // 2
        x, y = 45 + col * thumb_w, 110 + row * thumb_h
        draw.text((x, y), labels[index], fill=NAVY, font=label_font)
        canvas.paste(image, (x, y + 38))
    output = OUT / "00_overview.png"
    canvas.save(output, quality=95)
    for image in images:
        image.close()
    return output


def write_data(routine, wheelchair):
    payload = {
        "scenario_id": "multistage-013",
        "task": {
            "source": "nav-f1-corridor-14",
            "ordered_destinations": ["nav-f1-corridor-19", "nav-f1-spine-01"],
        },
        "routine": routine,
        "wheelchair": wheelchair,
        "interpretation_note": (
            "All four candidates have patient_burden=0.0 in the current map. "
            "Do not claim a measured burden reduction from this case."
        ),
    }
    (OUT / "case_data.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (OUT / "candidate_metrics.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["candidate", "risk", "distance_m", "time_s", "patient_burden", "routine_eligible", "routine_selected", "routine_exclusion_reason", "wheelchair_eligible", "wheelchair_selected"])
        for left, right in zip(routine["candidates"], wheelchair["candidates"]):
            writer.writerow([left["id"], left["risk"], left["distance_m"], left["estimated_time_s"], left["patient_burden"], left["eligible"], left["selected"], left["exclusion_reason"], right["eligible"], right["selected"]])


def main() -> int:
    configure_fonts()
    OUT.mkdir(parents=True, exist_ok=True)
    scenarios = load_json(PROJECT_ROOT / "benchmarks" / "scenarios.json")["scenarios"]
    scenario = next(item for item in scenarios if item["id"] == "multistage-013")
    provider = NavigationSandboxSceneProvider(
        BENCHMARK_ROOT / "data" / "fixtures" / "medical" / "hospital_1f.synthetic.json",
        BENCHMARK_ROOT / "navigation_sandbox",
        BENCHMARK_ROOT / "data" / "navigation" / "hospital_1f.bindings.json",
    )
    base_scene = provider.load_scene()
    routine = run_variant(base_scene, scenario, "routine", [])
    wheelchair = run_variant(
        base_scene,
        scenario,
        "wheelchair",
        ["WHEELCHAIR_ACCESSIBLE", "STEP_FREE"],
    )
    anchor, edge_routes, edge_lookup = load_geometry()
    floorplan = Image.open(FLOORPLAN).convert("RGB")
    all_routes = [item["route_real"] for item in routine["candidates"]] + [wheelchair["route"]]
    bounds = crop_bounds(all_routes, anchor, edge_routes, edge_lookup, floorplan)
    paths = [
        candidate_map(routine, floorplan, bounds, anchor, edge_routes, edge_lookup),
        metrics_figure(routine),
        shortest_vs_selected(routine, floorplan, bounds, anchor, edge_routes, edge_lookup),
        routine_vs_wheelchair(routine, wheelchair, floorplan, bounds, anchor, edge_routes, edge_lookup),
        audit_figure(routine, wheelchair),
    ]
    overview = make_overview(paths)
    write_data(routine, wheelchair)
    floorplan.close()
    print(json.dumps({"output_dir": str(OUT), "overview": str(overview), "figures": [str(path) for path in paths]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
