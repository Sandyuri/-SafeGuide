"""Export deterministic legal routes between every current registered location.

This utility intentionally does not call DeepSeek or Medical SafeGate once per
pair.  It loads the same trusted navigation scene used by RoboGuard and applies
the current robot permissions, availability fields, and mandatory route
policies before running deterministic Dijkstra searches.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from heapq import heappop, heappush
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_SOURCE = PROJECT_ROOT / "src"
if str(LOCAL_SOURCE) not in sys.path:
    sys.path.insert(0, str(LOCAL_SOURCE))

from roboguard.medical.providers import NavigationSandboxSceneProvider


CORRIDOR_TYPE = "CORRIDOR_STRUCTURE"
FORBIDDEN_TRANSIT_POLICIES = frozenset({"FORBID", "EMERGENCY_ONLY_ACTIVE"})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export every ordered legal route in the current hospital map."
    )
    parser.add_argument(
        "--sandbox",
        type=Path,
        default=PROJECT_ROOT / "navigation_sandbox",
    )
    parser.add_argument(
        "--base-scene",
        type=Path,
        default=PROJECT_ROOT / "data/fixtures/medical/hospital_1f.synthetic.json",
    )
    parser.add_argument(
        "--bindings",
        type=Path,
        default=PROJECT_ROOT / "data/navigation/hospital_1f.bindings.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "artifacts/all_legal_routes",
    )
    parser.add_argument(
        "--business-only",
        action="store_true",
        help=(
            "Use only non-corridor business locations as endpoints. Corridor "
            "nodes may still be used for transit. The default exports all "
            "currently legal registered map nodes."
        ),
    )
    return parser.parse_args()


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _zone_permissions(
    region: Mapping[str, Any], access_zones: Mapping[str, Mapping[str, Any]]
) -> set[str]:
    zone = access_zones.get(region.get("access_zone_id"), {})
    return set(zone.get("required_permissions", ()))


def _base_node_block_reason(
    node_id: str,
    *,
    regions: Mapping[str, Mapping[str, Any]],
    access_zones: Mapping[str, Mapping[str, Any]],
    robot_permissions: set[str],
    current_time: datetime,
) -> str | None:
    region = regions.get(node_id)
    if region is None:
        return "ROUTE_ENDPOINT_UNKNOWN"
    status = region.get("navigation_status", "OPEN")
    if status in {"UNKNOWN", "CONFLICTED"}:
        return "ROUTE_STATUS_UNKNOWN"
    if status == "BLOCKED":
        return "ROUTE_REGION_BLOCKED"
    valid_until = region.get("navigation_status_valid_until")
    if valid_until and current_time >= _parse_datetime(str(valid_until)):
        return "ROUTE_STATUS_EXPIRED"
    required = _zone_permissions(region, access_zones)
    if not required.issubset(robot_permissions):
        return "ROUTE_ACCESS_DENIED"
    if region.get("route_policy", "ALLOW_TRANSIT") in FORBIDDEN_TRANSIT_POLICIES:
        return "MANDATORY_ROUTE_POLICY_VIOLATION"
    return None


def _entry_block_reason(
    node_id: str,
    destination: str,
    *,
    regions: Mapping[str, Mapping[str, Any]],
    access_zones: Mapping[str, Mapping[str, Any]],
    robot_permissions: set[str],
    current_time: datetime,
) -> str | None:
    reason = _base_node_block_reason(
        node_id,
        regions=regions,
        access_zones=access_zones,
        robot_permissions=robot_permissions,
        current_time=current_time,
    )
    if reason:
        return reason
    policy = regions[node_id].get("route_policy", "ALLOW_TRANSIT")
    if policy == "DESTINATION_ONLY" and node_id != destination:
        return "DESTINATION_ONLY_TRANSIT"
    return None


def deterministic_legal_path(
    *,
    regions: Mapping[str, Mapping[str, Any]],
    access_zones: Mapping[str, Mapping[str, Any]],
    source: str,
    destination: str,
    robot_permissions: set[str],
    current_time: datetime,
) -> tuple[tuple[str, ...], float] | None:
    """Return the shortest policy-compliant path with stable tie breaking."""
    if source not in regions or destination not in regions:
        return None
    for endpoint in (source, destination):
        if _base_node_block_reason(
            endpoint,
            regions=regions,
            access_zones=access_zones,
            robot_permissions=robot_permissions,
            current_time=current_time,
        ):
            return None

    start_path = (source,)
    pending: list[tuple[float, tuple[str, ...], str]] = [(0.0, start_path, source)]
    best: dict[str, tuple[float, tuple[str, ...]]] = {source: (0.0, start_path)}

    while pending:
        distance_m, path, node_id = heappop(pending)
        if best.get(node_id) != (distance_m, path):
            continue
        if node_id == destination:
            return path, distance_m

        region = regions[node_id]
        costs = region.get("connection_costs", {})
        for neighbor in sorted(region.get("connections", ())):
            if neighbor in path or neighbor not in regions:
                continue
            if _entry_block_reason(
                neighbor,
                destination,
                regions=regions,
                access_zones=access_zones,
                robot_permissions=robot_permissions,
                current_time=current_time,
            ):
                continue
            edge_cost = costs.get(neighbor)
            if (
                isinstance(edge_cost, bool)
                or not isinstance(edge_cost, (int, float))
                or not math.isfinite(edge_cost)
                or edge_cost <= 0
            ):
                continue
            next_distance = distance_m + float(edge_cost)
            next_path = path + (neighbor,)
            candidate = (next_distance, next_path)
            if neighbor not in best or candidate < best[neighbor]:
                best[neighbor] = candidate
                heappush(pending, (next_distance, next_path, neighbor))
    return None


def _location_record(
    node_id: str,
    regions: Mapping[str, Mapping[str, Any]],
    access_zones: Mapping[str, Mapping[str, Any]],
    registry_by_node: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    region = regions[node_id]
    zone_id = region.get("access_zone_id")
    registry_item = registry_by_node.get(node_id, {})
    return {
        "node_id": node_id,
        "name_zh": registry_item.get(
            "canonical_name", region.get("display_name", node_id)
        ),
        "map_display_name_zh": region.get("display_name", node_id),
        "aliases": sorted(registry_item.get("aliases", ())),
        "location_class": registry_item.get("location_class"),
        "region_type": region.get("type"),
        "access_zone": zone_id,
        "required_permissions": sorted(_zone_permissions(region, access_zones)),
        "route_policy": region.get("route_policy", "ALLOW_TRANSIT"),
        "safe_stop": bool(region.get("safe_stop", False)),
    }


def build_route_catalog(
    scene: Any,
    *,
    registry_by_node: Mapping[str, Mapping[str, Any]] | None = None,
    business_only: bool = False,
) -> dict[str, Any]:
    data = scene.data
    regions = data["regions"]
    access_zones = data["access_zones"]
    robot_permissions = set(data["robot_state"]["access_permissions"])
    current_time = _parse_datetime(data["current_time"])
    facility = data["facility"]
    if facility["emergency_status"] != "NONE" or facility["hazards"]:
        raise RuntimeError(
            "facility emergency or hazard is active; no current routes may be exported"
        )

    registry_by_node = registry_by_node or {}
    endpoint_candidates = sorted(
        node_id
        for node_id, region in regions.items()
        if not business_only or region.get("type") != CORRIDOR_TYPE
    )
    legal_location_ids: list[str] = []
    excluded_locations: list[dict[str, str]] = []
    for node_id in endpoint_candidates:
        reason = _base_node_block_reason(
            node_id,
            regions=regions,
            access_zones=access_zones,
            robot_permissions=robot_permissions,
            current_time=current_time,
        )
        if reason:
            excluded_locations.append({"node_id": node_id, "reason_code": reason})
        else:
            legal_location_ids.append(node_id)

    locations = [
        _location_record(node_id, regions, access_zones, registry_by_node)
        for node_id in legal_location_ids
    ]
    location_by_id = {item["node_id"]: item for item in locations}
    preferred_names = {
        node_id: registry_by_node.get(node_id, {}).get(
            "canonical_name", region.get("display_name", node_id)
        )
        for node_id, region in regions.items()
    }
    routes: list[dict[str, Any]] = []
    route_number = 0
    for source in legal_location_ids:
        for destination in legal_location_ids:
            if source == destination:
                continue
            route_number += 1
            found = deterministic_legal_path(
                regions=regions,
                access_zones=access_zones,
                source=source,
                destination=destination,
                robot_permissions=robot_permissions,
                current_time=current_time,
            )
            base = {
                "route_id": f"route-{route_number:04d}",
                "source_id": source,
                "source_name_zh": location_by_id[source]["name_zh"],
                "destination_id": destination,
                "destination_name_zh": location_by_id[destination]["name_zh"],
            }
            if found is None:
                routes.append(
                    {
                        **base,
                        "decision": "REJECT",
                        "reason_code": "ROUTE_NOT_FOUND",
                        "distance_m": None,
                        "hop_count": None,
                        "node_ids": [],
                        "node_names_zh": [],
                        "edge_distances_m": [],
                        "traversed_access_zones": [],
                        "route_policies_checked": [],
                    }
                )
                continue

            path, distance_m = found
            edge_distances = [
                float(regions[left]["connection_costs"][right])
                for left, right in zip(path, path[1:])
            ]
            routes.append(
                {
                    **base,
                    "decision": "ALLOW",
                    "reason_code": "LEGAL_SHORTEST_ROUTE",
                    "distance_m": round(distance_m, 3),
                    "hop_count": len(path) - 1,
                    "node_ids": list(path),
                    "node_names_zh": [
                        preferred_names[node_id] for node_id in path
                    ],
                    "edge_distances_m": [round(value, 3) for value in edge_distances],
                    "traversed_access_zones": sorted(
                        {regions[node_id].get("access_zone_id") for node_id in path}
                    ),
                    "route_policies_checked": sorted(
                        {
                            regions[node_id].get("route_policy", "ALLOW_TRANSIT")
                            for node_id in path
                        }
                    ),
                }
            )

    allowed = [route for route in routes if route["decision"] == "ALLOW"]
    rejected = [route for route in routes if route["decision"] != "ALLOW"]
    return {
        "schema_version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "map_id": "hospital_1f",
        "scene_id": scene.scene_id,
        "scene_snapshot_id": scene.snapshot_id,
        "scene_snapshot_hash": scene.snapshot_hash,
        "scene_current_time": data["current_time"],
        "algorithm": "deterministic_dijkstra_with_pre_search_policy_filtering",
        "scope": (
            "All ordered pairs of currently legal non-corridor business locations; "
            "corridor_structure nodes remain available as route transit nodes."
            if business_only
            else "All ordered pairs of currently legal registered map nodes, including corridor structure nodes."
        ),
        "api_calls": {"deepseek": 0, "medical_safegate": 0},
        "robot_id": data["robot_state"]["id"],
        "robot_access_permissions": sorted(robot_permissions),
        "rules_applied": [
            "ROUTE_CONNECTIVITY",
            "ROUTE_REGION_AVAILABLE",
            "ROUTE_STATUS_CURRENT",
            "ROUTE_ACCESS_PERMISSION",
            "MANDATORY_ROUTE_POLICY",
            "DESTINATION_ONLY",
        ],
        "total_map_node_count": len(regions),
        "corridor_transit_node_count": sum(
            1 for region in regions.values() if region.get("type") == CORRIDOR_TYPE
        ),
        "endpoint_candidate_count": len(endpoint_candidates),
        "legal_location_count": len(legal_location_ids),
        "legal_business_location_count": sum(
            1
            for node_id in legal_location_ids
            if regions[node_id].get("type") != CORRIDOR_TYPE
        ),
        "legal_corridor_location_count": sum(
            1
            for node_id in legal_location_ids
            if regions[node_id].get("type") == CORRIDOR_TYPE
        ),
        "business_only": business_only,
        "excluded_locations": excluded_locations,
        "ordered_pair_count": len(routes),
        "allowed_route_count": len(allowed),
        "rejected_route_count": len(rejected),
        "locations": locations,
        "routes": routes,
    }


def write_json(catalog: Mapping[str, Any], path: Path) -> None:
    path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_routes_csv(catalog: Mapping[str, Any], path: Path) -> None:
    fields = [
        "route_id",
        "source_id",
        "source_name_zh",
        "destination_id",
        "destination_name_zh",
        "decision",
        "reason_code",
        "distance_m",
        "hop_count",
        "node_ids",
        "node_names_zh",
        "edge_distances_m",
        "traversed_access_zones",
        "route_policies_checked",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for route in catalog["routes"]:
            row = dict(route)
            for key in (
                "node_ids",
                "node_names_zh",
                "edge_distances_m",
                "traversed_access_zones",
                "route_policies_checked",
            ):
                row[key] = " -> ".join(str(value) for value in row[key])
            writer.writerow({field: row.get(field) for field in fields})


def write_distance_matrix(catalog: Mapping[str, Any], path: Path) -> None:
    locations = catalog["locations"]
    route_lookup = {
        (route["source_id"], route["destination_id"]): route
        for route in catalog["routes"]
    }
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["source_id", "source_name_zh"]
            + [f'{item["node_id"]}|{item["name_zh"]}' for item in locations]
        )
        for source in locations:
            cells: list[Any] = []
            for destination in locations:
                if source["node_id"] == destination["node_id"]:
                    cells.append(0)
                    continue
                route = route_lookup[(source["node_id"], destination["node_id"])]
                cells.append(
                    route["distance_m"]
                    if route["decision"] == "ALLOW"
                    else "UNREACHABLE"
                )
            writer.writerow([source["node_id"], source["name_zh"], *cells])


def write_summary(catalog: Mapping[str, Any], path: Path) -> None:
    allowed = [route for route in catalog["routes"] if route["decision"] == "ALLOW"]
    distances = [float(route["distance_m"]) for route in allowed]
    shortest = min(allowed, key=lambda item: (item["distance_m"], item["route_id"]))
    longest = max(allowed, key=lambda item: (item["distance_m"], item["route_id"]))
    lines = [
        "# RoboGuard 全部合法地点路径汇总",
        "",
        f'- 地图：`{catalog["map_id"]}`',
        f'- 场景快照：`{catalog["scene_snapshot_id"]}`',
        f'- 场景哈希：`{catalog["scene_snapshot_hash"]}`',
        f'- 地图节点：{catalog["total_map_node_count"]}',
        f'- 走廊中转节点：{catalog["corridor_transit_node_count"]}',
        f'- 当前合法注册地点：{catalog["legal_location_count"]}',
        f'- 其中业务地点：{catalog["legal_business_location_count"]}',
        f'- 其中走廊结构地点：{catalog["legal_corridor_location_count"]}',
        f'- 有向地点对：{catalog["ordered_pair_count"]}',
        f'- 成功路线：{catalog["allowed_route_count"]}',
        f'- 不可达/拒绝路线：{catalog["rejected_route_count"]}',
        "",
        "## 口径",
        "",
        "合法地点是当前地图注册表中状态有效、当前开放且机器人权限允许进入的节点。默认结果包含业务地点和走廊结构地点，因此也覆盖“门诊大厅”等绑定在走廊结构节点上的合法起点。临床受控区按当前机器人权限检查；节点开放状态、状态有效期和强制路线策略在搜索前过滤。`DESTINATION_ONLY` 节点只能作为本条路线的终点或起点，不能作为无关路径的中转捷径。",
        "",
        "本次批量计算不调用 DeepSeek，也不为每个地点对重复请求 Medical SafeGate。它使用 RoboGuard 当前可信场景快照和已经绑定到机器人状态的权限，因此结果可重复，适合做算法基线与离线评测。",
        "",
        "## 距离统计",
        "",
        f'- 最短路线：{shortest["source_name_zh"]} → {shortest["destination_name_zh"]}，{shortest["distance_m"]:.3f} m',
        f'- 最长路线：{longest["source_name_zh"]} → {longest["destination_name_zh"]}，{longest["distance_m"]:.3f} m',
        f'- 平均距离：{sum(distances) / len(distances):.3f} m',
        "",
        "## 合法地点",
        "",
        "| 节点 ID | 中文名称 | 类型 | 权限区域 | 路线策略 |",
        "|---|---|---|---|---|",
    ]
    for item in catalog["locations"]:
        lines.append(
            f'| `{item["node_id"]}` | {item["name_zh"]} | `{item["region_type"]}` | '
            f'`{item["access_zone"]}` | `{item["route_policy"]}` |'
        )
    lines.extend(
        [
            "",
            "## 输出文件",
            "",
            "- `all_legal_routes.json`：完整结构化路线、逐边距离、节点名称、权限区和策略。",
            "- `all_legal_routes.csv`：每个有向地点对一行，便于筛选和统计。",
            "- `all_legal_routes_distance_matrix.csv`：全部地点间最短合法距离矩阵。",
            "- `all_legal_routes_summary.md`：本汇总。",
            "",
            "## 重新生成",
            "",
            "```bash",
            "python -B RoboGuard-new/scripts/export_all_legal_routes.py",
            "```",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def export_catalog(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Path]]:
    provider = NavigationSandboxSceneProvider(
        args.base_scene, args.sandbox, args.bindings
    )
    scene = provider.load_scene()
    registry_data = json.loads(
        (args.sandbox / "semantics/location_registry.json").read_text(
            encoding="utf-8"
        )
    )
    registry_by_node = {
        item["node_id"]: item for item in registry_data.get("locations", [])
    }
    catalog = build_route_catalog(
        scene,
        registry_by_node=registry_by_node,
        business_only=args.business_only,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "json": args.output_dir / "all_legal_routes.json",
        "csv": args.output_dir / "all_legal_routes.csv",
        "matrix": args.output_dir / "all_legal_routes_distance_matrix.csv",
        "summary": args.output_dir / "all_legal_routes_summary.md",
    }
    write_json(catalog, outputs["json"])
    write_routes_csv(catalog, outputs["csv"])
    write_distance_matrix(catalog, outputs["matrix"])
    write_summary(catalog, outputs["summary"])
    return catalog, outputs


def main() -> int:
    catalog, outputs = export_catalog(parse_args())
    print(
        "Export complete: "
        f'{catalog["legal_location_count"]} legal locations, '
        f'{catalog["ordered_pair_count"]} ordered pairs, '
        f'{catalog["allowed_route_count"]} allowed, '
        f'{catalog["rejected_route_count"]} rejected.'
    )
    for label, output in outputs.items():
        print(f"{label}: {output}")
    return 0 if catalog["rejected_route_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
