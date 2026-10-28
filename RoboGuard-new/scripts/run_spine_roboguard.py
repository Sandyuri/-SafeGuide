"""Run Learned SafeGate, SPINE planning, and RoboGuard navigation review."""

from __future__ import annotations

import argparse
from heapq import heappop, heappush
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_SOURCE = PROJECT_ROOT / "src"
if str(LOCAL_SOURCE) not in sys.path:
    sys.path.insert(0, str(LOCAL_SOURCE))


from roboguard.medical.providers import NavigationSandboxSceneProvider
from roboguard.medical.scene import MedicalScene, MedicalSceneError
from roboguard.medical.joint_planner import DynamicEventSet, SUPPORTED_PLANNERS
from roboguard.medical.safety_barrier import require_execution_permit
from roboguard.medical.temporal_guard import MedicalTemporalGuard
from roboguard.navigation import (
    NavigationExecutionGuard,
    build_navigation_view,
    navigation_spine_graph,
    navigation_task,
)
from roboguard.lsg_adapter import (
    LSGBlocked,
    authorize_lsg_response_for_roboguard,
    load_lsg_default_context,
    load_lsg_expected_provenance,
    request_lsg_json,
)
from roboguard.spine_adapter import adapt_spine_plan
from show_navigation_route import render_svg, route_costs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Authorize with Learned SafeGate, plan with SPINE, and review the "
            "hospital route with RoboGuard-new."
        )
    )
    parser.add_argument(
        "--from",
        dest="source",
        help="Optional start override; defaults to the robot's current scene location.",
    )
    parser.add_argument(
        "--to",
        dest="destination",
        help="Optional destination override; inferred from --text when omitted.",
    )
    parser.add_argument(
        "--text",
        help=(
            "Natural-language navigation request. It may contain both the start "
            "and destination, so --from and --to can be omitted."
        ),
    )
    parser.add_argument(
        "--sandbox", type=Path, default=PROJECT_ROOT / "navigation_sandbox"
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
        "--lsg-root",
        type=Path,
        default=PROJECT_ROOT.parent / "LSG" / "LSG",
        help="Learned SafeGate package root used for schemas and trusted context",
    )
    parser.add_argument(
        "--lsg-url",
        default="http://127.0.0.1:8765",
        help="Base URL of the running Learned SafeGate service",
    )
    parser.add_argument(
        "--lsg-timeout",
        type=float,
        default=130.0,
        help="LSG HTTP timeout in seconds",
    )
    parser.add_argument(
        "--synthetic-goto",
        action="store_true",
        help="Bypass the LLM and inject the expected goto for offline wiring tests.",
    )
    parser.add_argument(
        "--dump-spine-graph",
        type=Path,
        help="Optionally save the anonymized graph sent to SPINE.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "artifacts/spine_roboguard_route.svg",
        help="Approved route map SVG output path.",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the generated SVG after RoboGuard approves the route.",
    )
    parser.add_argument("--temporal-mode", choices=("hybrid", "fixed", "off"), default="hybrid")
    parser.add_argument("--rules", type=Path, help="Medical navigation rules JSON")
    parser.add_argument("--model", help="OpenAI-compatible model; defaults to ROBOGUARD_MODEL")
    parser.add_argument("--show-ltl", action="store_true")
    parser.add_argument(
        "--planner",
        choices=SUPPORTED_PLANNERS,
        default="joint",
        help=(
            "Route method. pareto generates and selects by task profile; "
            "joint is the risk-first A*; dijkstra reproduces the baseline."
        ),
    )
    parser.add_argument(
        "--events",
        type=Path,
        help="Dynamic event JSON (closures, congestion, emergency corridor, elevator state).",
    )
    parser.add_argument(
        "--replanner",
        choices=("none", "dstar_lite"),
        default="none",
        help=(
            "Optional dynamic replanner. dstar_lite first creates an approved "
            "baseline plan, then repairs it for --events; major changes fall "
            "back to a full Pareto replan."
        ),
    )
    parser.add_argument(
        "--audit-output",
        type=Path,
        help="Write the JSON safety certificate, including rejected and paused results.",
    )
    return parser.parse_args()


def write_audit(
    path: Path,
    *,
    result,
    scene,
    view,
    planner,
    replanner,
    events,
) -> None:
    routes = []
    for route in result.checked_routes:
        routes.append(
            {
                "planner": route.planner,
                "plan_version": route.plan_version,
                "route_refs": list(route.regions),
                "route_region_ids": [view.resolve_reference(item) for item in route.regions],
                "distance_m": route.distance_m,
                "estimated_time_s": route.estimated_time_s,
                "risk_score": route.risk_score,
                "patient_burden": route.patient_burden,
                "risk_components": dict(route.risk_components),
                "workflow_trace": [list(item) for item in route.workflow_trace],
                "selection_profile": route.selection_profile,
                "selection_reason": route.selection_reason,
                "candidate_count": route.candidate_count,
                "candidate_comparisons": list(route.candidate_comparisons),
                "execution_authorized": route.execution_authorized,
                "safety_certificate_id": route.safety_certificate_id,
                "safety_certificate": dict(route.safety_certificate),
            }
        )
    payload = {
        "schema_version": "1.0",
        "authorization_id": view.payload["evaluation_id"],
        "request_id": view.payload["request_id"],
        "scene_snapshot_id": scene.snapshot_id,
        "scene_snapshot_hash": scene.snapshot_hash,
        "planner": planner,
        "replanner": replanner,
        "events": events.to_dict(),
        "decision": result.decision,
        "reason_code": result.reason_code,
        "message": result.message,
        "routes": routes,
        "checked_rules": sorted({
            item
            for route in result.checked_routes
            for item in route.checked_properties
        }),
        "checked_ltlf": list(result.checked_ltl),
        "violated_rules": list(result.violated_rules),
        "failed_route_node": result.failed_route_node,
        "replan_allowed": result.replan_allowed,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def request_text(args: argparse.Namespace) -> str:
    """Return one auditable request string for LSG evaluation."""

    if args.text is None:
        if not args.destination:
            raise MedicalSceneError("必须提供 --text，或至少提供 --to")
        if args.source:
            return f"请引导我从{args.source}前往{args.destination}。"
        return f"请引导我前往{args.destination}。"
    text = args.text.strip()
    if not text:
        raise MedicalSceneError("导航请求文本不能为空")
    if args.destination and args.destination not in text:
        raise MedicalSceneError(
            "--text 必须包含 --to 指定的目的地，避免文本请求与路径参数不一致"
        )
    return text


def authorize_navigation(
    *,
    args: argparse.Namespace,
    scene: MedicalScene,
    destination: str,
):
    """Request and locally revalidate one LSG navigation authorization."""

    text = request_text(args)
    trusted_context = load_lsg_default_context(args.lsg_root)
    expected_provenance = load_lsg_expected_provenance(args.lsg_root)
    external_request_id = "roboguard-lsg-" + str(uuid4())
    model_info = request_lsg_json(
        args.lsg_url,
        "/model-info",
        timeout=args.lsg_timeout,
    )
    if (
        model_info.get("loaded") is not True
        or model_info.get("provenance") != expected_provenance
    ):
        raise LSGBlocked("LSG service is not ready")
    response = request_lsg_json(
        args.lsg_url,
        "/evaluate",
        payload={
            "text": text,
            "session_id": None,
            "request_id": external_request_id,
            "trusted_context": trusted_context,
            "dispatch": True,
        },
        timeout=args.lsg_timeout,
    )

    session_id = None
    deferred = False
    for _ in range(3):
        native = response.get("native_result")
        if not isinstance(native, dict):
            raise LSGBlocked("LSG response has no native_result")
        decision = native.get("aggregate_decision")
        if native.get("request_id") != external_request_id:
            raise LSGBlocked("LSG response request identifier mismatch")
        if session_id is None:
            session_id = native.get("session_id")
        elif native.get("session_id") != session_id:
            raise LSGBlocked("LSG session changed during continuation")
        print(f"LSG: {decision}")
        if decision != "DEFER":
            break
        deferred = True
        followups = (native.get("dispatch_plan") or {}).get("followups") or []
        actionable = [
            item
            for item in followups
            if item.get("type") == "NEEDS_USER_INPUT"
            and isinstance(item.get("intent_index"), int)
        ]
        if not actionable:
            break
        supplements = {}
        for item in actionable:
            print(item.get("message", "请补充信息。"))
            supplement = input("补充信息: ").strip()
            while not supplement:
                supplement = input("补充信息: ").strip()
            supplements[str(item["intent_index"])] = supplement
        response = request_lsg_json(
            args.lsg_url,
            "/continue",
            payload={
                "session_id": native.get("session_id"),
                "request_id": external_request_id,
                "supplements": supplements,
                "trusted_context": trusted_context,
                "dispatch": True,
            },
            timeout=args.lsg_timeout,
        )
    else:
        raise LSGBlocked("LSG exceeded the maximum continuation count")

    return authorize_lsg_response_for_roboguard(
        response=response,
        trusted_context=trusted_context,
        expected_provenance=expected_provenance,
        expected_destination_id=destination,
        scene_snapshot_id=scene.snapshot_id,
        lsg_root=args.lsg_root,
        expected_request_id=external_request_id,
        expected_session_id=session_id,
        expected_raw_input=None if deferred else text,
    )


def resolve_location(provider, scene, value: str) -> str:
    if value in scene.data["regions"]:
        return value
    resolved = provider.resolve_location(value)
    if resolved is None:
        raise MedicalSceneError(f"无法解析地点名称：{value}")
    return resolved


def infer_source(provider, scene: MedicalScene, text: str) -> str:
    """Use an explicit ``从/由 <location>`` phrase or the live robot position."""

    candidates = {
        node_id
        for alias, node_id in provider.location_aliases.items()
        if f"从{alias}" in text or f"由{alias}" in text
    }
    if len(candidates) > 1:
        raise MedicalSceneError("导航请求包含多个不同的起点")
    if candidates:
        return next(iter(candidates))
    return scene.data["robot_state"]["current_region_id"]


def infer_destination_text(provider, text: str, source: str) -> str:
    """Select the last registry location mention as the navigation target.

    A location introduced by ``从/由`` is treated as an origin. This keeps the
    resolver deterministic while allowing LSG to receive the original
    natural-language request unchanged.
    """

    mentions = []
    for alias, node_id in provider.location_aliases.items():
        offset = 0
        while True:
            index = text.find(alias, offset)
            if index < 0:
                break
            is_origin = (
                text[max(0, index - 1) : index] in {"从", "由"}
                and node_id == source
            )
            mentions.append((index, len(alias), alias, node_id, is_origin))
            offset = index + 1
    # Discard aliases contained in a longer mention at the same occurrence.
    # For example, 卫生间 must not outrank 无障碍卫生间 just because it
    # begins later in the sentence.
    mentions = [
        item
        for item in mentions
        if not any(
            other[1] > item[1]
            and other[0] <= item[0]
            and item[0] + item[1] <= other[0] + other[1]
            for other in mentions
        )
    ]
    targets = [item for item in mentions if not item[4]]
    if not targets:
        targets = mentions
    if not targets:
        raise MedicalSceneError("无法从 --text 解析目的地；请使用地图中的地点名称")
    _, _, alias, _, _ = max(targets, key=lambda item: (item[0], item[1]))
    return alias


def move_robot(scene: MedicalScene, source: str) -> MedicalScene:
    data = scene.to_dict()
    data["robot_state"]["current_region_id"] = source
    data["snapshot"]["version"] += 1
    return MedicalScene.from_mapping(data)


def shortest_allowed_distance(scene: MedicalScene, source: str, target: str) -> float:
    regions = scene.data["regions"]
    permissions = set(scene.data["robot_state"]["access_permissions"])
    queue = [(0.0, source)]
    best = {source: 0.0}
    while queue:
        cost, node_id = heappop(queue)
        if cost != best[node_id]:
            continue
        if node_id == target:
            return cost
        for neighbor in regions[node_id]["connections"]:
            region = regions[neighbor]
            required = set(
                scene.data["access_zones"][region["access_zone_id"]][
                    "required_permissions"
                ]
            )
            if region.get("navigation_status") != "OPEN" or not required <= permissions:
                continue
            edge_cost = regions[node_id].get("connection_costs", {}).get(neighbor)
            if not isinstance(edge_cost, (int, float)) or not math.isfinite(edge_cost):
                continue
            candidate = cost + float(edge_cost)
            if candidate < best.get(neighbor, math.inf):
                best[neighbor] = candidate
                heappush(queue, (candidate, neighbor))
    return math.inf


def resolve_destination(provider, scene, sandbox: Path, text: str, source: str) -> str:
    """Resolve ordinary aliases and nearest-candidate destination groups."""

    registry_path = sandbox / "semantics/location_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    matches = []
    for location in registry.get("locations", []):
        names = [location.get("canonical_name"), *location.get("aliases", [])]
        for name in names:
            if isinstance(name, str) and name and name in text:
                matches.append((len(name), location))
    if matches:
        longest = max(length for length, _ in matches)
        winners = [item for length, item in matches if length == longest]
        candidate_sets = {
            tuple(item.get("navigation_resolution", {}).get("candidate_node_ids", ()))
            for item in winners
            if item.get("navigation_resolution", {}).get("mode")
            == "nearest_reachable_candidate"
        }
        candidate_sets.discard(())
        if len(candidate_sets) > 1:
            raise MedicalSceneError("导航请求匹配到多个不同的就近目的地组")
        if candidate_sets:
            candidates = next(iter(candidate_sets))
            ranked = sorted(
                (
                    shortest_allowed_distance(scene, source, node_id),
                    index,
                    node_id,
                )
                for index, node_id in enumerate(candidates)
                if node_id in scene.data["regions"]
            )
            if ranked and math.isfinite(ranked[0][0]):
                distance_m, _, node_id = ranked[0]
                print(f"就近目的地解析：{text} -> {node_id}（{distance_m:.2f} m）")
                return node_id
            raise MedicalSceneError(f"{text} 的所有候选地点均不可达")
    return resolve_location(provider, scene, text)


def route_cost(scene, node_ids) -> float:
    total = 0.0
    regions = scene.data["regions"]
    for left, right in zip(node_ids, node_ids[1:]):
        total += float(regions[left]["connection_costs"][right])
    return total


def open_output(path: Path) -> None:
    resolved = path.resolve()
    if os.name == "nt":
        os.startfile(resolved)
        return
    if shutil.which("wslpath") and shutil.which("explorer.exe"):
        completed = subprocess.run(
            ["wslpath", "-w", str(resolved)],
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.Popen(["explorer.exe", completed.stdout.strip()])
        return
    opener = shutil.which("xdg-open")
    if opener:
        subprocess.Popen([opener, str(resolved)])
        return
    print("无法自动打开路径图，请在 VS Code 中点击下面的文件路径。")


def request_spine_plan(graph: dict, task: str) -> tuple[dict, list[str]]:
    try:
        from spine.mapping.graph_util import GraphHandler
        from spine.spine import SPINE
    except ImportError as error:
        raise RuntimeError(
            "无法导入 SPINE；请在当前环境执行：python -m pip install -e ../SPINE-main"
        ) from error

    descriptor, raw_path = tempfile.mkstemp(prefix="hospital_spine_", suffix=".json")
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(graph, stream, ensure_ascii=False, indent=2)
        handler = GraphHandler(
            graph_path=str(path), init_node=graph["current_location"]
        )
        response, success, logs = SPINE(graph=handler).request(f"task: {task}")
    finally:
        path.unlink(missing_ok=True)
    if not success:
        raise RuntimeError("SPINE 规划失败：" + " | ".join(str(item) for item in logs))
    if not isinstance(response, dict) or "plan" not in response:
        raise RuntimeError("SPINE 响应缺少 plan 字段")
    return response, [str(item) for item in logs]


def main() -> int:
    args = parse_args()
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure:
                reconfigure(encoding="utf-8")
    try:
        provider = NavigationSandboxSceneProvider(
            args.base_scene, args.sandbox, args.bindings
        )
        scene = provider.load_scene()
        text = request_text(args)
        if args.source:
            source = resolve_location(provider, scene, args.source)
            scene = move_robot(scene, source)
        else:
            source = infer_source(provider, scene, text)
            if source != scene.data["robot_state"]["current_region_id"]:
                scene = move_robot(scene, source)
        destination_text = args.destination or infer_destination_text(
            provider, text, source
        )
        destination = resolve_destination(
            provider, scene, args.sandbox, destination_text, source
        )
        task = authorize_navigation(
            args=args,
            scene=scene,
            destination=destination,
        )
        view = build_navigation_view(
            scene,
            task,
            destination_aliases={destination_text: destination},
        )
        graph = navigation_spine_graph(view)
        spine_task = navigation_task(view)
        if args.dump_spine_graph:
            args.dump_spine_graph.parent.mkdir(parents=True, exist_ok=True)
            args.dump_spine_graph.write_text(
                json.dumps(graph, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        destination_ref = view.payload["task"]["destination_region_ref"]
        if args.synthetic_goto:
            destination_refs = list(
                view.payload["task"].get(
                    "ordered_destination_region_refs", (destination_ref,)
                )
            )
            response = {
                "reasoning": "offline synthetic goto for interface validation",
                "plan": [("goto", item) for item in destination_refs],
            }
        else:
            response, _ = request_spine_plan(graph, spine_task)
        actions = adapt_spine_plan(response["plan"])

        print("SPINE action plan:")
        for index, action in enumerate(actions, 1):
            print(f"  {index}. {action.display()}")

        temporal_guard = MedicalTemporalGuard(
            mode=args.temporal_mode,
            rules_path=args.rules,
            model=args.model,
        )
        events = DynamicEventSet.from_path(args.events) if args.events else DynamicEventSet.empty()
        if args.replanner == "dstar_lite":
            if not args.events:
                raise MedicalSceneError("--replanner dstar_lite requires --events")
            baseline_events = DynamicEventSet.empty()
            baseline = NavigationExecutionGuard(
                temporal_guard=temporal_guard,
                planner=args.planner,
                events=baseline_events,
            ).review_plan(
                scene=scene,
                authorized_task=task,
                planner_view=view,
                actions=actions,
            )
            if (
                not baseline.allowed
                or not baseline.checked_routes
                or baseline.planning_result is None
            ):
                print(
                    "D* Lite baseline failed: "
                    f"{baseline.decision} / {baseline.reason_code}: {baseline.message}"
                )
                return 3
            require_execution_permit(
                baseline.checked_routes[0],
                expected_plan_version=baseline_events.plan_version,
            )
            result = NavigationExecutionGuard(
                temporal_guard=temporal_guard,
                planner=args.planner,
                events=events,
            ).review_plan(
                scene=scene,
                authorized_task=task,
                planner_view=view,
                actions=actions,
                previous_plan=baseline.planning_result,
            )
        else:
            result = NavigationExecutionGuard(
                temporal_guard=temporal_guard,
                planner=args.planner,
                events=events,
            ).review_plan(
                scene=scene,
                authorized_task=task,
                planner_view=view,
                actions=actions,
            )
        print(f"RoboGuard: {result.decision} / {result.reason_code}")
        if args.audit_output:
            write_audit(
                args.audit_output,
                result=result,
                scene=scene,
                view=view,
                planner=args.planner,
                replanner=args.replanner,
                events=events,
            )
            print(f"安全证书：{args.audit_output.resolve()}")
        if not result.allowed or not result.checked_routes:
            print(result.message)
            return 3

        route = result.checked_routes[0]
        require_execution_permit(
            route,
            expected_plan_version=events.plan_version,
        )
        node_ids = [view.resolve_reference(ref) for ref in route.regions]
        edge_costs = route_costs(view, route.regions)
        print("批准路径：")
        for index, node_id in enumerate(node_ids, 1):
            name = scene.data["regions"][node_id].get("display_name", node_id)
            print(f"  {index}. {name} ({node_id})")
        print(f"总距离：{route_cost(scene, node_ids):.2f} m")
        print(f"规划器：{route.planner}；风险分：{route.risk_score:.2f}；计划版本：{route.plan_version}")
        render_svg(
            scene,
            view,
            route,
            edge_costs,
            args.output,
            args.sandbox
            / "artifacts/floorplan/source/hospital_1f_floorplan.jpg",
            args.sandbox
            / "artifacts/floorplan/hospital_1f.floorplan_anchor_mapping.json",
            args.sandbox
            / "artifacts/floorplan/hospital_1f.edge_routes.json",
            args.sandbox / "maps/hospital_1f.navigation.json",
        )
        print(f"路径图：{args.output.resolve()}")
        if args.show_ltl:
            print("检查的LTL：")
            for formula in result.checked_ltl:
                print(f"  - {formula}")
            print("时序轨迹：")
            for step, allowed in result.temporal_trace:
                print(f"  - {step}: {'ALLOW' if allowed else 'REJECT'}")
        if args.open:
            open_output(args.output)
        print("Final: PLAN_ALLOWED")
        return 0
    except LSGBlocked as error:
        print(f"安全门阻止进入规划：{error}", file=sys.stderr)
        print("Final: PAUSE")
        return 3
    except (MedicalSceneError, OSError, ValueError, RuntimeError) as error:
        print(f"运行失败：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
