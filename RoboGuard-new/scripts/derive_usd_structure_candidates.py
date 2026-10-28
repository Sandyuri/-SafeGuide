"""Derive reviewable corridor-structure candidates from the composed hospital USD.

Uses the operator-approved eight-node structure as semantic seeds. Static USD
collision meshes supply the geometry used to move each seed to local free space.
This intentionally writes a candidate file, not the executable navigation map.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from pxr import Gf, Usd, UsdGeom, UsdPhysics


ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "navigation_sandbox" / "scene" / "roboguard_hospital_sandbox.usda"
NAV = ROOT / "navigation_sandbox" / "maps" / "hospital_1f.navigation.json"
COORD = ROOT / "navigation_sandbox" / "maps" / "hospital_1f.coordinate_mapping.json"
OUTPUT = ROOT / "navigation_sandbox" / "maps" / "hospital_1f.usd_structure_candidates.json"
CELL = 0.20
X_MIN, X_MAX = -35.8, 23.2
Y_MIN, Y_MAX = -28.6, 30.6
WIDTH = math.ceil((X_MAX - X_MIN) / CELL)
HEIGHT = math.ceil((Y_MAX - Y_MIN) / CELL)
ROBOT_RADIUS_M = 0.45
LOCAL_SEARCH_M = 2.0


def cell_of(x: float, y: float) -> tuple[int, int]:
    return round((x - X_MIN) / CELL), round((y - Y_MIN) / CELL)


def xy_of(ix: int, iy: int) -> tuple[float, float]:
    return X_MIN + ix * CELL, Y_MIN + iy * CELL


def mark_disc(blocked: set[tuple[int, int]], x: float, y: float, radius: float) -> None:
    cx, cy = cell_of(x, y)
    reach = math.ceil(radius / CELL)
    for dx in range(-reach, reach + 1):
        for dy in range(-reach, reach + 1):
            if dx * dx + dy * dy <= (radius / CELL + 0.5) ** 2:
                ix, iy = cx + dx, cy + dy
                if 0 <= ix < WIDTH and 0 <= iy < HEIGHT:
                    blocked.add((ix, iy))


def distance_to_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    denom = vx * vx + vy * vy
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / denom)) if denom else 0.0
    return math.hypot(px - ax - t * vx, py - ay - t * vy)


def mark_triangle(blocked: set[tuple[int, int]], triangle: list[tuple[float, float, float]]) -> None:
    if max(p[2] for p in triangle) < 0.25 or min(p[2] for p in triangle) > 1.25:
        return
    a, b, c = triangle
    min_x = max(0, math.floor((min(p[0] for p in triangle) - X_MIN - CELL) / CELL))
    max_x = min(WIDTH - 1, math.ceil((max(p[0] for p in triangle) - X_MIN + CELL) / CELL))
    min_y = max(0, math.floor((min(p[1] for p in triangle) - Y_MIN - CELL) / CELL))
    max_y = min(HEIGHT - 1, math.ceil((max(p[1] for p in triangle) - Y_MIN + CELL) / CELL))
    if min_x > max_x or min_y > max_y:
        return
    ax, ay = a[:2]
    bx, by = b[:2]
    cx, cy = c[:2]
    area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    for ix in range(min_x, max_x + 1):
        x, _ = xy_of(ix, 0)
        for iy in range(min_y, max_y + 1):
            _, y = xy_of(0, iy)
            if abs(area) < 1e-7:
                hit = min(
                    distance_to_segment(x, y, ax, ay, bx, by),
                    distance_to_segment(x, y, bx, by, cx, cy),
                    distance_to_segment(x, y, cx, cy, ax, ay),
                ) <= CELL * 0.6
            else:
                u = ((bx - x) * (cy - y) - (by - y) * (cx - x)) / area
                v = ((cx - x) * (ay - y) - (cy - y) * (ax - x)) / area
                w = 1 - u - v
                hit = min(u, v, w) >= -0.03
            if hit:
                blocked.add((ix, iy))


def main() -> None:
    stage = Usd.Stage.Open(str(SCENE))
    if stage is None:
        raise RuntimeError(f"USD stage could not be opened: {SCENE}")
    xforms = UsdGeom.XformCache(Usd.TimeCode.Default())
    blocked: set[tuple[int, int]] = set()
    mesh_count = 0
    face_count = 0
    for prim in stage.Traverse():
        path = str(prim.GetPath())
        # Wall2 is the authored building shell. Columns and service counters
        # are separate fixed obstacles. Door meshes are frames/leafs and are
        # excluded here until door open/closed state is verified.
        if prim.GetTypeName() != "Mesh" or not any(
            f"/{part}/" in path for part in ("Wall2", "ZhuZi", "FuWuTai")
        ):
            continue
        collision = UsdPhysics.CollisionAPI(prim)
        if not collision or collision.GetCollisionEnabledAttr().Get() is False:
            continue
        mesh = UsdGeom.Mesh(prim)
        local_points = mesh.GetPointsAttr().Get() or []
        counts = mesh.GetFaceVertexCountsAttr().Get() or []
        indices = mesh.GetFaceVertexIndicesAttr().Get() or []
        transform = xforms.GetLocalToWorldTransform(prim)
        points = [transform.Transform(Gf.Vec3d(p)) for p in local_points]
        mesh_count += 1
        pos = 0
        for count in counts:
            face = [points[i] for i in indices[pos : pos + count]]
            pos += count
            if len(face) < 3:
                continue
            for i in range(1, len(face) - 1):
                mark_triangle(blocked, [tuple(face[j]) for j in (0, i, i + 1)])
                face_count += 1

    nav = json.loads(NAV.read_text(encoding="utf-8"))
    coords = json.loads(COORD.read_text(encoding="utf-8"))["node_coordinates"]
    names = {node["id"]: node["name_zh"] for node in nav["nodes"]}
    nodes = []
    candidate_ids = [f"nav-f1-spine-{i:02d}" for i in range(1, 9)]
    candidate_ids += ["nav-f1-safe-stop-west", "nav-f1-safe-stop-east"]
    for node_id in candidate_ids:
        seed = coords[node_id]["usd_xyz_m"][:2]
        seed_cell = cell_of(*seed)
        candidates = []
        reach = math.ceil(LOCAL_SEARCH_M / CELL)
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                ix, iy = seed_cell[0] + dx, seed_cell[1] + dy
                if not (0 <= ix < WIDTH and 0 <= iy < HEIGHT) or (ix, iy) in blocked:
                    continue
                x, y = xy_of(ix, iy)
                drift = math.hypot(x - seed[0], y - seed[1])
                if drift > LOCAL_SEARCH_M:
                    continue
                # Clearance within 1.2 m of each candidate. This is a local
                # geometry score, not a global collision or NavMesh proof.
                clearance = 1.2
                for sx in range(-6, 7):
                    for sy in range(-6, 7):
                        if (ix + sx, iy + sy) in blocked:
                            clearance = min(clearance, math.hypot(sx, sy) * CELL)
                score = min(clearance, 0.9) - drift * 0.32
                candidates.append((score, clearance, -drift, x, y))
        if not candidates:
            raise RuntimeError(f"No free candidate within {LOCAL_SEARCH_M} m of {node_id}")
        _, clearance, _, x, y = max(candidates)
        nodes.append(
            {
                "id": node_id,
                "name_zh": names[node_id],
                "usd_xyz_m": [round(x, 3), round(y, 3), 0.0],
                "seed_usd_xy_m": [round(v, 3) for v in seed],
                "seed_displacement_m": round(math.hypot(x - seed[0], y - seed[1]), 3),
                "local_wall_clearance_m": round(clearance, 3),
                "clearance_meets_robot_radius": clearance >= ROBOT_RADIUS_M,
                "derivation": "local_clearance_search_on_usd_static_collision_meshes",
                "node_kind": "safe_stop" if "safe-stop" in node_id else "corridor_structure",
            }
        )

    structure_ids = {node["id"] for node in nodes}
    edges = [
        {"id": edge["id"], "from": edge["from"], "to": edge["to"], "source": "operator_approved_redline_topology"}
        for edge in nav["edges"]
        if edge["from"] in structure_ids and edge["to"] in structure_ids
    ]
    result = {
        "schema_version": "1.0.0",
        "map_id": "hospital_1f",
        "source_usd": str(SCENE),
        "source_navigation": str(NAV),
        "units": "meters",
        "robot_radius_m": ROBOT_RADIUS_M,
        "grid_resolution_m": CELL,
        "usd_collision_meshes_used": mesh_count,
        "usd_triangles_processed": face_count,
        "nodes": nodes,
        "edges": edges,
        "verification_status": "candidate_static_mesh_local_clearance_only",
        "limitations": [
            "Chinese names and edge topology come from the operator-approved navigation map, not from USD prim labels.",
            "Only Wall2, ZhuZi, and FuWuTai collision meshes are rasterized; other furniture and door states are not yet evaluated.",
            "Local free space does not prove corridor connectivity; edges must be routed and validated on a full occupancy map or NavMesh.",
        ],
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT}; {len(nodes)} nodes, {len(edges)} topology edges; {mesh_count} collision meshes, {face_count} triangles")
    for node in nodes:
        print(node["id"], node["name_zh"], node["usd_xyz_m"], "clearance", node["local_wall_clearance_m"])


if __name__ == "__main__":
    main()
