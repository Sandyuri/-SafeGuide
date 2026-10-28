"""Summarize the focused normal/risk benchmark with category-specific metrics."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLANNERS = ("dijkstra", "astar", "generate_then_check", "joint", "pareto")


def percentile(values, probability):
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * probability)))
    return ordered[index]


def average(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def summarize(rows, scenarios):
    references = {item["id"]: item["reference"] for item in scenarios}
    output = []
    for planner in PLANNERS:
        normal = [
            row for row in rows
            if row["planner"] == planner and row["category"] == "normal"
        ]
        risk = [
            row for row in rows
            if row["planner"] == planner and row["category"] == "risk_detour"
        ]
        normal_success = [row for row in normal if int(row["success"])]
        normal_overheads = [
            float(row["distance_m"])
            / float(references[row["scenario_id"]]["shortest_distance_m"])
            - 1.0
            for row in normal_success
        ]

        safe_detours = 0
        risk_reductions = []
        distance_overheads = []
        for row in risk:
            reference = references[row["scenario_id"]]
            baseline_risk = float(reference["shortest_risk"])
            selected_risk = float(row["risk_score"])
            ratio = (
                float(row["distance_m"])
                / float(reference["shortest_distance_m"])
            )
            if (
                int(row["success"])
                and selected_risk < baseline_risk - 1e-9
                and ratio <= float(reference["max_distance_ratio"]) + 1e-9
            ):
                safe_detours += 1
            risk_reductions.append(
                (baseline_risk - selected_risk) / baseline_risk
            )
            distance_overheads.append(ratio - 1.0)

        output.append({
            "planner": planner,
            "normal_safety_constrained_completion_rate": (
                len(normal_success) / len(normal)
            ),
            "normal_mean_excess_distance_ratio": average(normal_overheads),
            "normal_p95_pipeline_ms": percentile(
                [float(row["pipeline_ms"]) for row in normal], 0.95
            ),
            "risk_safe_detour_success_rate": safe_detours / len(risk),
            "risk_mean_relative_risk_reduction": average(risk_reductions),
            "risk_mean_excess_distance_ratio": average(distance_overheads),
        })
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenarios",
        type=Path,
        default=PROJECT_ROOT / "benchmarks/normal_risk_scenarios.json",
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=PROJECT_ROOT / "artifacts/benchmark_normal_risk/planner_comparison.csv",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "artifacts/benchmark_normal_risk",
    )
    args = parser.parse_args()
    scenarios = json.loads(args.scenarios.read_text(encoding="utf-8"))["scenarios"]
    with args.results.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    summary = summarize(rows, scenarios)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "normal_risk_metrics.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    json_path = args.output_dir / "normal_risk_metrics.json"
    json_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scenario_counts": {"normal": 60, "risk_detour": 60},
                "metrics": summary,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"CSV: {csv_path}")
    print(f"JSON: {json_path}")


if __name__ == "__main__":
    main()
