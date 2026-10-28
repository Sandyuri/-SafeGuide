"""Rebuild the hospital 1F graph as a point-robot, USD-checked topology.

This script is intentionally deterministic.  It reads the composed OpenUSD
scene, rasterizes static collision meshes at navigation height, snaps authored
floor-plan seeds to the connected free-space component, routes every graph
edge with A*, simplifies the result into collision-free polylines, and writes
all synchronized sandbox artifacts.
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import shutil
from collections import deque
from pathlib import Path

from pxr import Gf, Usd, UsdGeom, UsdPhysics

from densify_navigation_topology import densify_artifacts


IMAGE_WIDTH = 6709
IMAGE_HEIGHT = 6702
X_MIN, X_MAX = -35.468066, 22.740802
Y_MIN, Y_MAX = -28.238046, 30.183436
CELL = 0.15
GRID_X_MIN, GRID_X_MAX = -36.0, 23.5
GRID_Y_MIN, GRID_Y_MAX = -28.8, 30.8
GRID_WIDTH = math.ceil((GRID_X_MAX - GRID_X_MIN) / CELL) + 1
GRID_HEIGHT = math.ceil((GRID_Y_MAX - GRID_Y_MIN) / CELL) + 1
Z_MIN, Z_MAX = 0.12, 1.85


# Eight retained structural nodes plus twenty-three new corridor / junction /
# entrance-approach nodes.  Pixel seeds are authored in the 6709x6702 source
# floor-plan frame; USD collision geometry is authoritative for final poses.
STRUCTURE_NODES = (
    ("nav-f1-spine-01", "北侧候诊通道西节点", (3120, 800), 0.0),
    ("nav-f1-spine-02", "北侧候诊通道东节点", (4240, 800), 0.0),
    ("nav-f1-spine-03", "东侧主通道节点", (4240, 2220), -90.0),
    ("nav-f1-spine-04", "门诊大厅中央通道", (3120, 2220), 180.0),
    ("nav-f1-spine-05", "西侧主通道节点", (600, 2220), 180.0),
    ("nav-f1-spine-06", "南侧功能检查通道节点", (4240, 4320), 180.0),
    ("nav-f1-spine-07", "南侧西端通道节点", (600, 4320), 0.0),
    ("nav-f1-spine-08", "东侧门诊连接节点", (5840, 2220), 0.0),
    ("nav-f1-corridor-01", "北侧康复走廊西接近点", (2700, 760), 90.0),
    ("nav-f1-corridor-02", "北侧康复走廊中西接近点", (3460, 760), 90.0),
    ("nav-f1-corridor-03", "北侧康复走廊中央接近点", (4140, 760), 90.0),
    ("nav-f1-corridor-04", "北侧康复走廊中东接近点", (5050, 760), 90.0),
    ("nav-f1-corridor-05", "北侧康复走廊东接近点", (5700, 760), 90.0),
    ("nav-f1-corridor-06", "大厅西北通道节点", (2800, 1466), 180.0),
    ("nav-f1-corridor-07", "大厅北部中央通道节点", (3150, 1120), -90.0),
    ("nav-f1-corridor-08", "大厅东北通道节点", (4300, 1040), -90.0),
    ("nav-f1-corridor-09", "挂号收费南侧通道节点", (5200, 2220), 90.0),
    ("nav-f1-corridor-11", "西侧中部路口", (1500, 2460), -90.0),
    ("nav-f1-corridor-12", "药房北侧接近节点", (2760, 2460), -90.0),
    ("nav-f1-corridor-13", "电梯厅西侧绕行节点", (760, 3450), -90.0),
    ("nav-f1-corridor-14", "药房东侧通道节点", (4040, 3450), -90.0),
    ("nav-f1-corridor-15", "东侧中部通道节点", (5840, 3450), -90.0),
    ("nav-f1-corridor-16", "南侧西部路口", (1500, 4320), 0.0),
    ("nav-f1-corridor-17", "功能检查西接近节点", (2860, 4650), -90.0),
    ("nav-f1-corridor-18", "功能检查东接近节点", (5050, 4650), -90.0),
    ("nav-f1-corridor-19", "左下设施接近节点", (900, 5400), -90.0),
    ("nav-f1-corridor-20", "右下设施接近节点", (5840, 5150), -90.0),
    ("nav-f1-corridor-21", "抢救室门口上方接近节点", (1806, 2465), -90.0),
    ("nav-f1-corridor-22", "药房东南侧绕行节点", (4040, 4320), -90.0),
    # Operator-requested four-way junction between display nodes 54 and 43,
    # aligned with display nodes 53 and 14.
    ("nav-f1-corridor-23", "候诊区横向通道交汇节点", (3120, 1475), 0.0),
    # Collision-free intermediate point on the USD shortest public route from
    # display node 22 to display node 34; it stays outside elevator shafts.
    ("nav-f1-corridor-24", "服务中心至功能检查绕行节点", (4849, 4392), -90.0),
)


# Local adjacency only.  A* supplies the actual bends around USD walls,
# columns, fixed furniture, and counters.
BACKBONE_LINKS = (
    ("nav-f1-corridor-01", "nav-f1-corridor-02"),
    ("nav-f1-corridor-01", "nav-f1-spine-01"),
    ("nav-f1-corridor-01", "nav-f1-corridor-06"),
    ("nav-f1-corridor-02", "nav-f1-corridor-03"),
    ("nav-f1-corridor-03", "nav-f1-corridor-04"),
    ("nav-f1-corridor-04", "nav-f1-corridor-05"),
    ("nav-f1-corridor-03", "nav-f1-spine-02"),
    ("nav-f1-spine-01", "nav-f1-corridor-06"),
    ("nav-f1-spine-01", "nav-f1-corridor-07"),
    ("nav-f1-spine-02", "nav-f1-corridor-08"),
    ("nav-f1-corridor-07", "nav-f1-corridor-08"),
    # The operator-requested junction replaces the direct 54-to-43 edge and
    # provides the shared turn for display nodes 53 and 14.
    ("nav-f1-corridor-06", "nav-f1-corridor-23"),
    ("nav-f1-corridor-07", "nav-f1-corridor-23"),
    ("nav-f1-corridor-23", "nav-f1-spine-04"),
    ("nav-f1-corridor-08", "nav-f1-spine-03"),
    ("nav-f1-spine-04", "nav-f1-spine-03"),
    ("nav-f1-spine-03", "nav-f1-corridor-09"),
    ("nav-f1-corridor-09", "nav-f1-spine-08"),
    ("nav-f1-spine-04", "nav-f1-corridor-12"),
    ("nav-f1-spine-04", "nav-f1-spine-05"),
    ("nav-f1-spine-05", "nav-f1-corridor-11"),
    ("nav-f1-corridor-11", "nav-f1-corridor-21"),
    ("nav-f1-corridor-21", "nav-f1-corridor-12"),
    ("nav-f1-corridor-14", "nav-f1-spine-03"),
    ("nav-f1-corridor-14", "nav-f1-corridor-22"),
    ("nav-f1-corridor-22", "nav-f1-corridor-18"),
    # Operator-requested lower public-corridor connections: display node 69
    # to 45, and display node 63 to 69.
    ("nav-f1-corridor-22", "nav-f1-spine-06"),
    ("nav-f1-corridor-16", "nav-f1-corridor-22"),
    ("nav-f1-corridor-11", "nav-f1-corridor-13"),
    ("nav-f1-corridor-13", "nav-f1-corridor-16"),
    # Operator-requested direct public-side connection: display node 60 to 46.
    ("nav-f1-corridor-13", "nav-f1-spine-07"),
    # Operator-requested public connection from display node 71 to 46.
    # A* computes the obstacle-avoiding path across the south corridor.
    ("nav-f1-corridor-24", "nav-f1-spine-07"),
    # Operator-requested public connections from display node 71 to the
    # functional-exam corridor (45), west approach (64), and pharmacy-side
    # bypass (69).  Each route is collision-checked independently.
    ("nav-f1-corridor-24", "nav-f1-spine-06"),
    ("nav-f1-corridor-24", "nav-f1-corridor-17"),
    ("nav-f1-corridor-24", "nav-f1-corridor-22"),
    ("nav-f1-corridor-16", "nav-f1-spine-07"),
    ("nav-f1-corridor-16", "nav-f1-corridor-17"),
    ("nav-f1-corridor-17", "nav-f1-spine-06"),
    ("nav-f1-spine-06", "nav-f1-corridor-18"),
    ("nav-f1-corridor-17", "nav-f1-corridor-18"),
    ("nav-f1-spine-03", "nav-f1-spine-06"),
    ("nav-f1-spine-08", "nav-f1-corridor-15"),
    ("nav-f1-corridor-18", "nav-f1-corridor-20"),
    ("nav-f1-spine-07", "nav-f1-corridor-19"),
)


BUSINESS_ATTACHMENTS = {
    "sg-f1-information-desk": "nav-f1-corridor-01",
    "sg-f1-mental-health": "nav-f1-corridor-02",
    "sg-f1-occupational-rehabilitation": "nav-f1-corridor-02",
    "sg-f1-neurological-rehabilitation": "nav-f1-corridor-03",
    "sg-f1-cardiopulmonary-rehabilitation": "nav-f1-corridor-03",
    "sg-f1-spinal-cord-rehabilitation": "nav-f1-corridor-04",
    "sg-f1-spinal-cord-rehabilitation-east": "nav-f1-corridor-04",
    "sg-f1-orthopedic-rehabilitation": "nav-f1-corridor-05",
    "sg-f1-restroom": "nav-f1-corridor-05",
    "sg-f1-accessible-restroom": "nav-f1-corridor-05",
    "sg-f1-consultation-rooms": "nav-f1-corridor-06",
    "sg-f1-queue-ticket-machine": "nav-f1-corridor-07",
    "sg-f1-notice-board": "nav-f1-spine-04",
    "sg-f1-outpatient-waiting": "nav-f1-corridor-07",
    "sg-f1-registration-payment-counter": "nav-f1-corridor-09",
    "sg-f1-consultation-room": "nav-f1-corridor-09",
    "sg-f1-public-elevator-hall": "nav-f1-corridor-11",
    "sg-f1-nebulization-room": "nav-f1-corridor-16",
    "sg-f1-emergency-room-entrance": "nav-f1-corridor-21",
    "sg-f1-medication-consultation": "nav-f1-corridor-14",
    "sg-f1-pharmacy": "nav-f1-corridor-12",
    "sg-f1-patient-service-center": "nav-f1-corridor-14",
    "sg-f1-self-service-registration-payment": "nav-f1-corridor-15",
    "sg-f1-self-service-registration-payment-south": "nav-f1-spine-07",
    # Display node 14 enters through the new 54/43/53/14 junction.
    "sg-f1-outpatient-waiting": "nav-f1-corridor-23",
    "sg-f1-self-service-report-machine": "nav-f1-corridor-20",
    "sg-f1-escalator": "nav-f1-corridor-15",
    "sg-f1-functional-exam-waiting": "nav-f1-corridor-17",
    "sg-f1-stairs": "nav-f1-corridor-19",
    "nav-f1-safe-stop-west": "nav-f1-spine-04",
    "nav-f1-safe-stop-east": "nav-f1-spine-03",
    "sg-f1-functional-exam-waiting-east": "nav-f1-corridor-18",
    "sg-f1-stairs-lower-right": "nav-f1-corridor-20",
    "sg-f1-restroom-lower-left": "nav-f1-corridor-19",
    "sg-f1-accessible-restroom-lower-left": "nav-f1-corridor-19",
    "sg-f1-accessible-restroom-lower-right": "nav-f1-corridor-20",
    "sg-f1-infusion-room": "nav-f1-corridor-16",
}


# Operator-confirmed direct visibility/connectivity along the north corridor.
# These supplement the consultation desk's ordinary nearby approach edge.
EXTRA_DIRECT_LINKS = (
    ("sg-f1-information-desk", "nav-f1-spine-01"),
    ("sg-f1-information-desk", "nav-f1-corridor-02"),
    # Operator-requested connection: display node 40 to the queue-ticket
    # machine (display node 12).
    ("sg-f1-queue-ticket-machine", "nav-f1-spine-01"),
    # Retained operator-requested near-side connection from display node 17
    # (public elevator hall) to display node 60, alongside its 17-to-58
    # obstacle-avoiding primary approach.
    ("sg-f1-public-elevator-hall", "nav-f1-corridor-13"),
    # Operator-requested public-corridor approach from display node 56 to
    # consultation room (display node 16).
    ("sg-f1-consultation-room", "nav-f1-corridor-09"),
    # Operator-requested direct route from display node 14 to display node 43.
    # A* follows the collision-free equivalent of the 14-to-54 public path.
    ("sg-f1-outpatient-waiting", "nav-f1-spine-04"),
    # Operator-requested connection: display node 51 to orthopedic
    # rehabilitation entrance (display node 08).
    ("sg-f1-orthopedic-rehabilitation", "nav-f1-corridor-04"),
    # Operator-requested connection: display node 50 to spinal-cord
    # rehabilitation west entrance (display node 06).
    ("sg-f1-spinal-cord-rehabilitation", "nav-f1-corridor-03"),
    # Operator-requested connection: display node 47 to the north self-service
    # registration/payment machine (display node 23).
    ("sg-f1-self-service-registration-payment", "nav-f1-spine-08"),
    # Operator-requested connection: display node 49 to neurological
    # rehabilitation (display node 04).
    ("sg-f1-neurological-rehabilitation", "nav-f1-corridor-02"),
    # Operator-requested direct connection between display node 22 (patient
    # service center) and display node 23 (north self-service machine).
    ("sg-f1-patient-service-center", "sg-f1-self-service-registration-payment"),
    # Operator-requested direct connection between display node 22 (patient
    # service center) and display node 26 (escalator).
    ("sg-f1-patient-service-center", "sg-f1-escalator"),
    # Operator-requested direct connection between display node 22 (patient
    # service center) and display node 16 (consultation room).
    ("sg-f1-patient-service-center", "sg-f1-consultation-room"),
    # Operator-requested connection between display node 18 (nebulization
    # room) and display node 64 (functional-exam west approach).
    ("sg-f1-nebulization-room", "nav-f1-corridor-17"),
    # The 22-to-34 shortest public route is split at a structural relay rather
    # than represented as a business-to-business edge.
    ("sg-f1-patient-service-center", "nav-f1-corridor-24"),
    ("sg-f1-functional-exam-waiting-east", "nav-f1-corridor-24"),
    # Operator-requested public connection from display node 36 to 46.
    ("sg-f1-restroom-lower-left", "nav-f1-spine-07"),
    # Operator-requested connection between the public-side entrance poses of
    # the nebulization room and medication consultation room. It is routed on
    # the collision grid; corridor risk semantics still require review.
    # Operator-requested connection between display node 34 (east functional-
    # exam waiting area) and display node 123 (waypoint 052).
    ("sg-f1-functional-exam-waiting-east", "nav-f1-waypoint-052"),
    # Operator-requested connection between display node 21 (pharmacy pickup
    # display) and display node 89 (waypoint 018).
    ("sg-f1-pharmacy", "nav-f1-waypoint-018"),
    # Operator-requested connection between display node 34 (east functional-
    # exam waiting area) and display node 25 (self-service report machine).
    ("sg-f1-functional-exam-waiting-east", "sg-f1-self-service-report-machine"),
    # Operator-requested connection between display node 13 (notice board)
    # and display node 84 (waypoint 013).
    ("sg-f1-notice-board", "nav-f1-waypoint-013"),
    # Operator-requested corridor links: display nodes 86-to-87 and 87-to-95.
    ("nav-f1-waypoint-015", "nav-f1-waypoint-016"),
    ("nav-f1-waypoint-016", "nav-f1-waypoint-024"),
)


# Operator-corrected business approach poses in source floor-plan pixels.
BUSINESS_SEED_PIXEL_OVERRIDES = {
    # Operator correction: display node 14 is colocated with display node 70.
    "sg-f1-outpatient-waiting": (3120, 1466),
    "sg-f1-functional-exam-waiting": (2860, 5097),
    # Operator correction: the public-side position of the registration and
    # payment counter is the former corridor-09 position (display node 56).
    "sg-f1-registration-payment-counter": (5200, 2050),
    # Operator correction: align the patient-service approach with the
    # direct southbound public corridor from display node 56.
    "sg-f1-patient-service-center": (5300, 3256),
    # Operator correction: display node 24 is colocated with display node 29
    # and shares its public-corridor approach.
    "sg-f1-self-service-registration-payment-south": (319, 4598),
}


# These business approaches must attach to the stated public corridor node.
# In particular, registration/payment must not reconnect to corridor-04
# (display node 51) through the file-storage / volunteer-office rooms.
FORCED_BUSINESS_ATTACHMENTS = {
    "sg-f1-registration-payment-counter": "nav-f1-corridor-09",
    # Operator-requested replacement for removed display node 57:
    # display node 13 connects directly to display node 43.
    "sg-f1-notice-board": "nav-f1-spine-04",
    # Display node 56 connects directly down to display node 22.
    "sg-f1-patient-service-center": "nav-f1-corridor-09",
    "sg-f1-self-service-registration-payment-south": "nav-f1-spine-07",
    # Operator-requested connection from display node 17 (public elevator hall)
    # to display node 58.  A* must route around the elevator-bank obstacles.
    "sg-f1-public-elevator-hall": "nav-f1-corridor-11",
    # Infusion and nebulization are reached from the side-door approach after
    # travelling around the central clinical block via corridor-13 (60) and
    # corridor-16 (63), never through the rooms themselves.
    "sg-f1-infusion-room": "nav-f1-corridor-16",
    "sg-f1-nebulization-room": "nav-f1-corridor-16",
    # Operator-requested replacement: display node 20 connects to display
    # node 90 instead of display nodes 69 and 45.
    "sg-f1-medication-consultation": "nav-f1-waypoint-019",
}


# Removed at the operator's request.  Keeping this list in the generator
# prevents a future rebuild from restoring obsolete nodes from old JSON.
REMOVED_NODE_IDS = {"nav-f1-corridor-10", "sg-f1-rest-area"}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def pixel_to_usd(pixel: tuple[float, float] | list[float]) -> tuple[float, float, float]:
    px, py = pixel
    return (
        X_MIN + px / IMAGE_WIDTH * (X_MAX - X_MIN),
        Y_MAX - py / IMAGE_HEIGHT * (Y_MAX - Y_MIN),
        0.0,
    )


def usd_to_pixel(point: tuple[float, float] | list[float]) -> tuple[int, int]:
    x, y = point[:2]
    return (
        round((x - X_MIN) / (X_MAX - X_MIN) * IMAGE_WIDTH),
        round((Y_MAX - y) / (Y_MAX - Y_MIN) * IMAGE_HEIGHT),
    )


def cell_of(x: float, y: float) -> tuple[int, int]:
    return round((x - GRID_X_MIN) / CELL), round((y - GRID_Y_MIN) / CELL)


def xy_of(cell: tuple[int, int]) -> tuple[float, float]:
    return GRID_X_MIN + cell[0] * CELL, GRID_Y_MIN + cell[1] * CELL


def valid_cell(cell: tuple[int, int]) -> bool:
    return 0 <= cell[0] < GRID_WIDTH and 0 <= cell[1] < GRID_HEIGHT


def point_segment_distance(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    denom = vx * vx + vy * vy
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / denom)) if denom else 0.0
    return math.hypot(px - ax - t * vx, py - ay - t * vy)


def mark_triangle(blocked: set[tuple[int, int]], triangle) -> None:
    if max(p[2] for p in triangle) < Z_MIN or min(p[2] for p in triangle) > Z_MAX:
        return
    min_x = max(0, math.floor((min(p[0] for p in triangle) - GRID_X_MIN - CELL) / CELL))
    max_x = min(GRID_WIDTH - 1, math.ceil((max(p[0] for p in triangle) - GRID_X_MIN + CELL) / CELL))
    min_y = max(0, math.floor((min(p[1] for p in triangle) - GRID_Y_MIN - CELL) / CELL))
    max_y = min(GRID_HEIGHT - 1, math.ceil((max(p[1] for p in triangle) - GRID_Y_MIN + CELL) / CELL))
    if min_x > max_x or min_y > max_y:
        return
    a, b, c = triangle
    ax, ay = a[:2]
    bx, by = b[:2]
    cx, cy = c[:2]
    area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    for ix in range(min_x, max_x + 1):
        x = GRID_X_MIN + ix * CELL
        for iy in range(min_y, max_y + 1):
            y = GRID_Y_MIN + iy * CELL
            if abs(area) < 1e-8:
                hit = min(
                    point_segment_distance(x, y, ax, ay, bx, by),
                    point_segment_distance(x, y, bx, by, cx, cy),
                    point_segment_distance(x, y, cx, cy, ax, ay),
                ) <= CELL * 0.58
            else:
                u = ((bx - x) * (cy - y) - (by - y) * (cx - x)) / area
                v = ((cx - x) * (ay - y) - (cy - y) * (ax - x)) / area
                w = 1.0 - u - v
                hit = min(u, v, w) >= -0.02
            if hit:
                blocked.add((ix, iy))


def point_in_polygon(point, polygon):
    x, y = point
    inside = False
    for index, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(index + 1) % len(polygon)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def load_no_through_zones(path: Path):
    data = load(path)
    if data.get("source_image_size_px") != [IMAGE_WIDTH, IMAGE_HEIGHT]:
        raise RuntimeError(f"no-through zone image frame mismatch: {path}")
    zones = data.get("zones", [])
    if not zones:
        raise RuntimeError(f"no no-through zones found: {path}")
    for zone in zones:
        polygon = zone.get("polygon_pixel_xy")
        if not isinstance(polygon, list) or len(polygon) < 3:
            raise RuntimeError(f"invalid no-through polygon: {zone.get('id')}")
    return zones


def mark_no_through_zones(blocked, zones):
    counts = {}
    for zone in zones:
        polygon = [pixel_to_usd(point)[:2] for point in zone["polygon_pixel_xy"]]
        min_x, max_x = min(point[0] for point in polygon), max(point[0] for point in polygon)
        min_y, max_y = min(point[1] for point in polygon), max(point[1] for point in polygon)
        lo = cell_of(min_x, min_y)
        hi = cell_of(max_x, max_y)
        count = 0
        for ix in range(max(0, lo[0] - 1), min(GRID_WIDTH, hi[0] + 2)):
            for iy in range(max(0, lo[1] - 1), min(GRID_HEIGHT, hi[1] + 2)):
                cell = ix, iy
                if point_in_polygon(xy_of(cell), polygon):
                    blocked.add(cell)
                    count += 1
        counts[zone["id"]] = count
    return counts


def build_occupancy(scene: Path, no_through_zones_path: Path | None = None):
    stage = Usd.Stage.Open(str(scene))
    if stage is None:
        raise RuntimeError(f"USD stage could not be opened: {scene}")
    xforms = UsdGeom.XformCache(Usd.TimeCode.Default())
    blocked: set[tuple[int, int]] = set()
    mesh_count = triangle_count = 0
    for prim in stage.Traverse():
        path = str(prim.GetPath())
        if prim.GetTypeName() != "Mesh":
            continue
        if any(token in path for token in ("/World/AA/", "/Door/", "/DaMen/", "/Floor_")):
            continue
        collision = UsdPhysics.CollisionAPI(prim)
        if not collision or collision.GetCollisionEnabledAttr().Get() is False:
            continue
        mesh = UsdGeom.Mesh(prim)
        local_points = mesh.GetPointsAttr().Get() or []
        counts = mesh.GetFaceVertexCountsAttr().Get() or []
        indices = mesh.GetFaceVertexIndicesAttr().Get() or []
        if not local_points or not counts:
            continue
        transform = xforms.GetLocalToWorldTransform(prim)
        points = [transform.Transform(Gf.Vec3d(point)) for point in local_points]
        mesh_count += 1
        pos = 0
        for count in counts:
            face = [points[index] for index in indices[pos : pos + count]]
            pos += count
            if len(face) < 3:
                continue
            for index in range(1, len(face) - 1):
                mark_triangle(blocked, [tuple(face[j]) for j in (0, index, index + 1)])
                triangle_count += 1
    if no_through_zones_path is not None:
        counts = mark_no_through_zones(blocked, load_no_through_zones(no_through_zones_path))
        print(f"  no-through semantic cells={counts}")
    return blocked, mesh_count, triangle_count


NEIGHBORS = (
    (-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
    (-1, -1, math.sqrt(2)), (-1, 1, math.sqrt(2)),
    (1, -1, math.sqrt(2)), (1, 1, math.sqrt(2)),
)


def can_step(left, right, blocked):
    if not valid_cell(right) or right in blocked:
        return False
    dx, dy = right[0] - left[0], right[1] - left[1]
    if dx and dy:
        if (left[0] + dx, left[1]) in blocked or (left[0], left[1] + dy) in blocked:
            return False
    return True


def nearest_free(seed, blocked, allowed=None, max_distance_m=5.0):
    sx, sy = cell_of(*seed)
    reach = math.ceil(max_distance_m / CELL)
    best = None
    for dx in range(-reach, reach + 1):
        for dy in range(-reach, reach + 1):
            cell = sx + dx, sy + dy
            if not valid_cell(cell) or cell in blocked or (allowed is not None and cell not in allowed):
                continue
            distance = math.hypot(dx, dy) * CELL
            if distance <= max_distance_m and (best is None or distance < best[0]):
                best = distance, cell
    if best is None:
        raise RuntimeError(f"no free USD point within {max_distance_m} m of {seed}")
    return best[1], best[0]


def connected_component(start, blocked):
    result = {start}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for dx, dy, _ in NEIGHBORS:
            nxt = current[0] + dx, current[1] + dy
            if nxt not in result and can_step(current, nxt, blocked):
                result.add(nxt)
                queue.append(nxt)
    return result


def astar(start, goal, blocked, allowed):
    queue = [(0.0, start)]
    came = {}
    cost = {start: 0.0}
    while queue:
        _, current = heapq.heappop(queue)
        if current == goal:
            path = [current]
            while current in came:
                current = came[current]
                path.append(current)
            return list(reversed(path))
        base = cost[current]
        for dx, dy, factor in NEIGHBORS:
            nxt = current[0] + dx, current[1] + dy
            if nxt not in allowed or not can_step(current, nxt, blocked):
                continue
            new_cost = base + factor * CELL
            if new_cost + 1e-9 < cost.get(nxt, math.inf):
                cost[nxt] = new_cost
                came[nxt] = current
                heuristic = math.hypot(goal[0] - nxt[0], goal[1] - nxt[1]) * CELL
                heapq.heappush(queue, (new_cost + heuristic, nxt))
    raise RuntimeError(f"no collision-free route from {start} to {goal}")


def nearest_goal_path(start, goals, blocked, allowed):
    """Dijkstra route from one business pose to the nearest structure pose."""
    candidates = {cell: node_id for node_id, cell in goals.items() if cell != start}
    if not candidates:
        raise RuntimeError(f"no distinct structure goal for {start}")
    queue = [(0.0, start)]
    came = {}
    cost = {start: 0.0}
    while queue:
        current_cost, current = heapq.heappop(queue)
        if current_cost > cost[current] + 1e-9:
            continue
        if current in candidates:
            path = [current]
            while current in came:
                current = came[current]
                path.append(current)
            return candidates[path[0]], list(reversed(path))
        for dx, dy, factor in NEIGHBORS:
            nxt = current[0] + dx, current[1] + dy
            if nxt not in allowed or not can_step(current, nxt, blocked):
                continue
            new_cost = current_cost + factor * CELL
            if new_cost + 1e-9 < cost.get(nxt, math.inf):
                cost[nxt] = new_cost
                came[nxt] = current
                heapq.heappush(queue, (new_cost, nxt))
    raise RuntimeError(f"no structure reachable from business pose {start}")


def line_clear(left, right, blocked):
    ax, ay = xy_of(left)
    bx, by = xy_of(right)
    length = math.hypot(bx - ax, by - ay)
    samples = max(1, math.ceil(length / (CELL * 0.28)))
    previous = left
    for index in range(samples + 1):
        ratio = index / samples
        cell = cell_of(ax + (bx - ax) * ratio, ay + (by - ay) * ratio)
        if cell in blocked or not valid_cell(cell):
            return False
        if cell != previous and not can_step(previous, cell, blocked):
            return False
        previous = cell
    return True


def simplify_path(path, blocked):
    if len(path) <= 2:
        return path
    result = [path[0]]
    anchor = 0
    while anchor < len(path) - 1:
        candidate = len(path) - 1
        while candidate > anchor + 1 and not line_clear(path[anchor], path[candidate], blocked):
            candidate -= 1
        result.append(path[candidate])
        anchor = candidate
    return result


def polyline_length(points):
    return sum(math.dist(left[:2], right[:2]) for left, right in zip(points, points[1:]))


def route_edge(left_id, right_id, cells, blocked, component):
    raw = astar(cells[left_id], cells[right_id], blocked, component)
    simplified = simplify_path(raw, blocked)
    usd_points = [(*xy_of(cell), 0.0) for cell in simplified]
    pixel_points = [list(usd_to_pixel(point)) for point in usd_points]
    return pixel_points, round(polyline_length(usd_points), 3), len(raw)


def route_from_cells(path, blocked):
    simplified = simplify_path(path, blocked)
    usd_points = [(*xy_of(cell), 0.0) for cell in simplified]
    pixel_points = [list(usd_to_pixel(point)) for point in usd_points]
    return pixel_points, round(polyline_length(usd_points), 3), len(path)


def write_scene(
    path: Path,
    scene_sublayer: str,
    nav: dict,
    coords: dict,
    structure_ids: set[str],
    safe_stop_ids: set[str],
):
    lines = [
        "#usda 1.0",
        "(",
        "    metersPerUnit = 1",
        '    upAxis = "Z"',
        f"    subLayers = [@{scene_sublayer}@]",
        ")",
        'def Xform "RoboGuardNavigation"',
        "{",
        '    custom string mapId = "hospital_1f"',
        '    custom string navigationModel = "point_robot_openusd_static_collision"',
        '    def Scope "NavigationNodes"',
        "    {",
    ]
    for index, node in enumerate(nav["nodes"], 1):
        node_id = node["id"]
        x, y, _ = coords["node_coordinates"][node_id]["usd_xyz_m"]
        if node_id in structure_ids:
            color, radius = "(0.08, 0.45, 0.75)", 0.18
        elif node_id in safe_stop_ids:
            color, radius = "(0.10, 0.65, 0.32)", 0.22
        else:
            color, radius = "(0.95, 0.55, 0.10)", 0.20
        name = node["name_zh"].replace('"', '\\"')
        lines.extend([
            f'        def Sphere "N{index:03d}"',
            "        {",
            f'            custom string nodeId = "{node_id}"',
            f'            custom string nameZh = "{name}"',
            f"            color3f[] primvars:displayColor = [{color}]",
            f"            double radius = {radius}",
            f"            double3 xformOp:translate = ({x:.6f}, {y:.6f}, 0.08)",
            '            uniform token[] xformOpOrder = ["xformOp:translate"]',
            "        }",
        ])
    lines.extend(["    }", "}", ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True, help="navigation_sandbox directory")
    parser.add_argument("--usd", type=Path, required=True, help="source composed USD scene")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()

    nav_path = root / "maps/hospital_1f.navigation.json"
    coord_path = root / "maps/hospital_1f.coordinate_mapping.json"
    entrance_path = root / "maps/hospital_1f.entrance_mapping.json"
    candidates_path = root / "maps/hospital_1f.usd_structure_candidates.json"
    registry_path = root / "semantics/location_registry.json"
    zones_path = root / "semantics/access_zones.json"
    stops_path = root / "semantics/safe_stops.json"
    anchors_path = root / "artifacts/floorplan/hospital_1f.floorplan_anchor_mapping.json"
    routes_path = root / "artifacts/floorplan/hospital_1f.edge_routes.json"
    route_semantics_path = root / "semantics/route_semantics.json"
    no_through_path = root / "maps/hospital_1f.no_through_zones.json"
    no_through_zones = load_no_through_zones(no_through_path)
    public_approach_seeds = {
        zone["destination_node_id"]: zone["public_approach_seed_pixel_xy"]
        for zone in no_through_zones
        if zone.get("destination_node_id") and zone.get("public_approach_seed_pixel_xy")
    }

    nav = load(nav_path)
    coords = load(coord_path)
    entrances = load(entrance_path)
    registry = load(registry_path)
    zones = load(zones_path)
    stops = load(stops_path)
    anchors = load(anchors_path)
    route_semantics = load(route_semantics_path)
    route_semantics["nodes"] = {
        node_id: value
        for node_id, value in route_semantics.get("nodes", {}).items()
        if not node_id.startswith("nav-f1-waypoint-")
    }
    emergency_semantic_node_ids = {
        node_id
        for node_id, value in route_semantics["nodes"].items()
        if "EMERGENCY_CORRIDOR" in value.get("semantic_tags", ())
    }

    # Generated degree-two waypoints are rebuilt deterministically after the
    # authored topology.  They must never be mistaken for business locations.
    existing_nodes = {
        node["id"]: node
        for node in nav["nodes"]
        if node["id"] not in REMOVED_NODE_IDS
        and not node["id"].startswith("nav-f1-waypoint-")
    }
    existing_structure = {node_id for node_id in existing_nodes if node_id.startswith("nav-f1-spine-") or node_id.startswith("nav-f1-corridor-")}
    business_ids = set(existing_nodes) - existing_structure
    if set(BUSINESS_ATTACHMENTS) != business_ids:
        missing = sorted(business_ids - set(BUSINESS_ATTACHMENTS))
        extra = sorted(set(BUSINESS_ATTACHMENTS) - business_ids)
        raise RuntimeError(f"business attachment table mismatch; missing={missing}, extra={extra}")

    print("Rasterizing OpenUSD collision geometry ...")
    blocked, mesh_count, triangle_count = build_occupancy(args.usd, no_through_path)
    print(f"  meshes={mesh_count}, triangles={triangle_count}, blocked_cells={len(blocked)}")

    structure_seed = {node_id: pixel_to_usd(pixel)[:2] for node_id, _, pixel, _ in STRUCTURE_NODES}
    lobby_seed = structure_seed["nav-f1-spine-04"]
    lobby_cell, _ = nearest_free(lobby_seed, blocked, max_distance_m=3.0)
    component = connected_component(lobby_cell, blocked)
    print(f"  connected_free_cells={len(component)}")
    if len(component) < 1000:
        raise RuntimeError("USD free-space component is unexpectedly small")

    node_seed = {}
    old_coords = coords.get("node_coordinates", {})
    for node_id in business_ids:
        if node_id in public_approach_seeds:
            node_seed[node_id] = pixel_to_usd(public_approach_seeds[node_id])[:2]
            continue
        if node_id in BUSINESS_SEED_PIXEL_OVERRIDES:
            node_seed[node_id] = pixel_to_usd(BUSINESS_SEED_PIXEL_OVERRIDES[node_id])[:2]
            continue
        point = old_coords.get(node_id, {}).get("usd_xyz_m")
        if not isinstance(point, list) or len(point) != 3:
            raise RuntimeError(f"missing existing USD approach pose for {node_id}")
        node_seed[node_id] = tuple(point[:2])
    node_seed.update(structure_seed)

    cells = {}
    snap_distance = {}
    for node_id, seed in node_seed.items():
        cell, displacement = nearest_free(seed, blocked, allowed=component, max_distance_m=5.0)
        cells[node_id] = cell
        snap_distance[node_id] = round(displacement, 3)
    worst = sorted(snap_distance.items(), key=lambda item: item[1], reverse=True)[:10]
    print("  largest pose adjustments:", worst)

    structure_by_id = {node_id: (name, pixel, yaw) for node_id, name, pixel, yaw in STRUCTURE_NODES}
    structure_ids = set(structure_by_id)
    # Retain business order and replace prior structure definitions.
    nav["nodes"] = [node for node in nav["nodes"] if node["id"] in business_ids]
    for node in nav["nodes"]:
        for redundant_field in (
            "map_annotation", "safe_stop", "emergency_route", "waiting_area"
        ):
            node.pop(redundant_field, None)
    for node_id, name, _, _ in STRUCTURE_NODES:
        nav["nodes"].append({
            "id": node_id,
            "name_zh": name,
            "region_type": "corridor_structure",
            "access_zone": "public",
            "navigation_status": "open",
            "node_class": "navigation_structure",
        })
    for node in nav["nodes"]:
        node["node_class"] = "navigation_structure" if node["id"] in structure_ids else "safegate_location"

    new_coords = {}
    for node in nav["nodes"]:
        node_id = node["id"]
        x, y = xy_of(cells[node_id])
        previous = old_coords.get(node_id, {})
        yaw = structure_by_id[node_id][2] if node_id in structure_by_id else float(previous.get("yaw_deg", 0.0))
        new_coords[node_id] = {
            "usd_xyz_m": [round(x, 6), round(y, 6), 0.0],
            "yaw_deg": round(yaw, 3),
            "clearance_radius_m": 0.0,
            "coordinate_status": "openusd_static_collision_point_verified",
            "source_seed_usd_xy_m": [round(value, 6) for value in node_seed[node_id]],
            "usd_snap_displacement_m": snap_distance[node_id],
        }
    coords["node_coordinates"] = new_coords
    coords["usd_scene"] = "../scene/roboguard_hospital_sandbox.usda"
    coords["frame"]["navigation_model"] = "point_robot"
    coords["calibration"].update({
        "status": "openusd_static_collision_point_routes_verified",
        "routing_grid_resolution_m": CELL,
        "robot_radius_m": 0.0,
        "no_through_zones_file": "hospital_1f.no_through_zones.json",
        "no_through_zone_status": "floorplan_traced_pending_onsite_verification",
        "collision_height_band_m": [Z_MIN, Z_MAX],
    })

    edge_routes = {}
    edges = []
    route_diagnostics = []
    edge_number = 201
    for left, right in BACKBONE_LINKS:
        pixel_route, distance, raw_count = route_edge(left, right, cells, blocked, component)
        edge_id = f"E{edge_number:03d}"
        edge_number += 1
        edge_routes[edge_id] = pixel_route[1:-1]
        edges.append({
            "id": edge_id,
            "from": left,
            "to": right,
            "bidirectional": True,
            "distance_m": distance,
            "access_zone": "public",
            "distance_source": "openusd_static_collision_point_astar_simplified_polyline",
            "navigation_status": "open",
            "emergency_route": left in {"nav-f1-spine-07", "nav-f1-corridor-16", "nav-f1-corridor-19"} and right in {"nav-f1-spine-07", "nav-f1-corridor-16", "nav-f1-corridor-19"},
        })
        route_diagnostics.append((edge_id, left, right, distance, raw_count, len(pixel_route)))
    structure_cells = {node_id: cells[node_id] for node_id in structure_ids}
    resolved_attachments = {}
    for business_id in sorted(BUSINESS_ATTACHMENTS):
        forced_structure = FORCED_BUSINESS_ATTACHMENTS.get(business_id)
        if forced_structure:
            structure_id = forced_structure
            business_to_structure = astar(cells[business_id], cells[structure_id], blocked, component)
            if not business_to_structure:
                raise RuntimeError(f"forced business attachment is not routable: {business_id} -> {structure_id}")
        else:
            structure_id, business_to_structure = nearest_goal_path(cells[business_id], structure_cells, blocked, component)
        resolved_attachments[business_id] = structure_id
        pixel_route, distance, raw_count = route_from_cells(list(reversed(business_to_structure)), blocked)
        if {
            business_id,
            structure_id,
        } == {"sg-f1-outpatient-waiting", "nav-f1-corridor-23"}:
            # Preserve a positive graph cost for the two semantic nodes that
            # intentionally share one physical navigation pose.
            distance = 0.001
        edge_id = f"E{edge_number:03d}"
        edge_number += 1
        edge_routes[edge_id] = pixel_route[1:-1]
        business = next(node for node in nav["nodes"] if node["id"] == business_id)
        edges.append({
            "id": edge_id,
            "from": structure_id,
            "to": business_id,
            "bidirectional": True,
            "distance_m": distance,
            "access_zone": business["access_zone"],
            "distance_source": "openusd_static_collision_point_astar_simplified_polyline",
            "navigation_status": business["navigation_status"],
            "emergency_route": business_id in emergency_semantic_node_ids,
            "edge_class": "business_approach",
        })
        route_diagnostics.append((edge_id, structure_id, business_id, distance, raw_count, len(pixel_route)))
    for business_id, structure_id in EXTRA_DIRECT_LINKS:
        pixel_route, distance, raw_count = route_edge(business_id, structure_id, cells, blocked, component)
        # E285, E299 and E300 are retired operator links. Keep their IDs
        # reserved for audit history without renumbering later edges.
        while edge_number in {285, 299, 300}:
            edge_number += 1
        edge_id = f"E{edge_number:03d}"
        edge_number += 1
        edge_routes[edge_id] = pixel_route[1:-1]
        business = next(node for node in nav["nodes"] if node["id"] == business_id)
        edge = {
            "id": edge_id,
            "from": business_id,
            "to": structure_id,
            "bidirectional": True,
            "distance_m": distance,
            "access_zone": business["access_zone"],
            "distance_source": "openusd_static_collision_point_astar_simplified_polyline",
            "navigation_status": business["navigation_status"],
            "emergency_route": False,
            "edge_class": "operator_confirmed_direct_connection",
        }
        edges.append(edge)
        route_diagnostics.append((edge_id, business_id, structure_id, distance, raw_count, len(pixel_route)))
    nav["edges"] = edges
    nav["navigation_status"] = "openusd_static_collision_point_routes_verified"
    nav["node_count_policy"] = "real_map_unbounded_business_plus_navigation_structure"
    nav["emergency_corridor"] = {
        "node_ids": ["nav-f1-spine-07", "nav-f1-corridor-16", "nav-f1-corridor-11", "nav-f1-corridor-21", "sg-f1-emergency-room-entrance"],
        "edge_ids": [edge["id"] for edge in edges if edge.get("emergency_route")],
    }
    nav.setdefault("provenance", {}).update({
        "usd_scene": "scene/roboguard_hospital_sandbox.usda",
        "routing_method": "point_robot_astar_on_openusd_static_collision_meshes",
        "routing_grid_resolution_m": CELL,
        "robot_radius_m": 0.0,
        "no_through_zones_file": "maps/hospital_1f.no_through_zones.json",
        "no_through_zone_status": "floorplan_traced_pending_onsite_verification",
        "business_attachment_policy": "one nearby structure edge per business location; operator-confirmed direct links additionally connect information desk to 40/49 and medication consultation to south functional-exam corridor",
        "topology_optimization": "corridor-13 is routed through the blank public area west of the elevator shafts; pharmacy, emergency, infusion, nebulization, and elevator interiors are no-through zones",
        "resolved_business_attachments": resolved_attachments,
    })

    old_anchor_by_id = {item["node_id"]: item for item in anchors.get("nodes", [])}
    preferred_display_numbers = {
        "nav-f1-corridor-01": 48,
        "nav-f1-corridor-21": 68,
        "nav-f1-corridor-22": 69,
        "nav-f1-corridor-23": 70,
        "nav-f1-corridor-24": 71,
    }
    anchors["nodes"] = []
    for fallback_display_number, node in enumerate(nav["nodes"], 1):
        node_id = node["id"]
        pixel = list(usd_to_pixel(new_coords[node_id]["usd_xyz_m"]))
        old = old_anchor_by_id.get(node_id, {})
        display_number = 69 if node_id == "nav-f1-corridor-22" else old.get("display_number", preferred_display_numbers.get(node_id, fallback_display_number))
        semantic_pixel = old.get("semantic_floorplan_pixel_xy", old.get("floorplan_pixel_xy"))
        anchor_record = {
            "display_number": display_number,
            "node_id": node_id,
            "name_zh": node["name_zh"],
            "floorplan_pixel_xy": pixel,
            "floorplan_normalized_xy": [round(pixel[0] / IMAGE_WIDTH, 6), round(pixel[1] / IMAGE_HEIGHT, 6)],
            "semantic_floorplan_pixel_xy": semantic_pixel if node_id in business_ids else list(structure_by_id[node_id][1]),
            "source_label": old.get("source_label", "USD点机器人走廊结构节点"),
            "alignment_status": "openusd_point_collision_snapped_route_pose",
            "usd_snap_displacement_m": snap_distance[node_id],
        }
        # A display-only offset must survive rerouting. The route anchor above
        # remains the inverse projection of the USD pose.
        if "display_floorplan_pixel_xy" in old:
            anchor_record["display_floorplan_pixel_xy"] = old["display_floorplan_pixel_xy"]
        anchors["nodes"].append(anchor_record)
    anchors["usd_mapping_modified"] = True
    anchors["routing_pose_definition"] = "floorplan_pixel_xy_is_inverse_projection_of_usd_collision_free_point_pose"

    routes = {
        "schema_version": "2.0.0",
        "map_id": "hospital_1f",
        "coordinate_frame": "hospital_1f_floorplan_source_pixels",
        "source_image_size_px": [IMAGE_WIDTH, IMAGE_HEIGHT],
        "generation_method": "A* on OpenUSD static collision grid, then collision-checked line-of-sight simplification",
        "verification_status": "openusd_static_collision_point_verified",
        "robot_radius_m": 0.0,
        "grid_resolution_m": CELL,
        "no_through_zones_file": "../../maps/hospital_1f.no_through_zones.json",
        "collision_height_band_m": [Z_MIN, Z_MAX],
        "limitations": [
            "Door leaf meshes are treated as open; door state is not modeled.",
            "The navigation model is a mathematical point and does not provide robot-body clearance.",
            "Floor-plan pixels use the existing wall-bound affine registration to USD world coordinates.",
        ],
        "edge_routes": edge_routes,
    }

    # Location registry: all SafeGate/business names remain, structure names are
    # internal navigation aliases; 门诊大厅 stays bound to spine-04.
    registry_by_id = {item["node_id"]: item for item in registry.get("locations", []) if item.get("node_id") in business_ids}
    registry["locations"] = [registry_by_id[node["id"]] for node in nav["nodes"] if node["id"] in registry_by_id]
    for node_id, (name, _, _) in structure_by_id.items():
        registry["locations"].append({
            "canonical_name": name,
            "source_location_id": None,
            "node_id": node_id,
            "aliases": ["门诊大厅"] if node_id == "nav-f1-spine-04" else [],
            "location_class": "navigation_structure",
        })

    for zone in zones.get("zones", []):
        if zone.get("id") == "public":
            existing_public_business = [node["id"] for node in nav["nodes"] if node["id"] in business_ids and node.get("access_zone") == "public"]
            zone["node_ids"] = sorted(existing_public_business + list(structure_ids))
        else:
            zone["node_ids"] = sorted(node["id"] for node in nav["nodes"] if node["id"] in business_ids and node.get("access_zone") == zone.get("id"))

    stops["stops"] = [stop for stop in stops.get("stops", []) if stop.get("node_id") in business_ids]
    stops["robot_footprint"] = {"shape": "point", "radius_m": 0.0, "required_margin_m": 0.0}
    for stop in stops.get("stops", []):
        node_id = stop["node_id"]
        stop["usd_xyz_m"] = new_coords[node_id]["usd_xyz_m"]
        stop["yaw_deg"] = new_coords[node_id]["yaw_deg"]
        stop["clearance_radius_m"] = 0.0
        stop["obstacle_check"] = "openusd_static_collision_point_verified"

    # Preserve semantic door matches, but make approach_pose exactly equal to
    # the executable graph coordinate.  Recreate structure entries.
    entry_by_id = {entry["node_id"]: entry for entry in entrances.get("entries", []) if entry.get("node_id") in business_ids}
    entrances["entries"] = []
    for node in nav["nodes"]:
        node_id = node["id"]
        if node_id in business_ids:
            entry = entry_by_id[node_id]
            entry["approach_pose"]["usd_xyz_m"] = new_coords[node_id]["usd_xyz_m"]
            entry["approach_pose"]["yaw_deg"] = new_coords[node_id]["yaw_deg"]
            entry["approach_pose"]["yaw_source"] = "existing_door_orientation_with_openusd_point_pose_snap"
            entry.setdefault("clearance", {}).update({
                "robot_radius_m": 0.0,
                "required_margin_m": 0.0,
                "required_clearance_radius_m": 0.0,
                "static_collision_status": "point_pose_free_in_openusd_collision_grid",
                "navmesh_status": "not_required_for_point_topology",
            })
            entry["verification_status"] = "openusd_static_collision_point_verified"
        else:
            name, seed_pixel, yaw = structure_by_id[node_id]
            point = new_coords[node_id]["usd_xyz_m"]
            entry = {
                "node_id": node_id,
                "name_zh": name,
                "region_type": "corridor_structure",
                "access_zone": "public",
                "target_kind": "corridor_waypoint",
                "floorplan_semantic_anchor": {
                    "pixel_xy": list(seed_pixel),
                    "registered_usd_xyz_m": [round(value, 6) for value in pixel_to_usd(seed_pixel)],
                    "source_label": "operator_floorplan_corridor_seed",
                    "alignment_status": "openusd_point_collision_snapped",
                },
                "door_match": {"status": "not_applicable", "method": None},
                "door_cluster_id": None,
                "approach_pose": {
                    "usd_xyz_m": point,
                    "yaw_deg": yaw,
                    "door_offset_m": None,
                    "yaw_source": "corridor_structure_orientation",
                },
                "clearance": {
                    "robot_radius_m": 0.0,
                    "required_margin_m": 0.0,
                    "required_clearance_radius_m": 0.0,
                    "static_collision_status": "point_pose_free_in_openusd_collision_grid",
                    "navmesh_status": "not_required_for_point_topology",
                },
                "mapping_confidence": "usd_collision_checked",
                "verification_status": "openusd_static_collision_point_verified",
            }
        entrances["entries"].append(entry)
    entrances["robot_model"] = "point"
    entrances["verification_status"] = "openusd_static_collision_point_verified"

    candidates = {
        "schema_version": "2.0.0",
        "map_id": "hospital_1f",
        "source_usd": str(args.usd.resolve()),
        "source_navigation": str(nav_path.resolve()),
        "units": "meters",
        "robot_radius_m": 0.0,
        "grid_resolution_m": CELL,
        "collision_height_band_m": [Z_MIN, Z_MAX],
        "usd_collision_meshes_used": mesh_count,
        "usd_triangles_processed": triangle_count,
        "nodes": [
            {
                "id": node_id,
                "name_zh": structure_by_id[node_id][0],
                "usd_xyz_m": new_coords[node_id]["usd_xyz_m"],
                "seed_floorplan_pixel_xy": list(structure_by_id[node_id][1]),
                "seed_displacement_m": snap_distance[node_id],
                "derivation": "openusd_static_collision_connected_free_space_snap",
                "node_kind": "corridor_structure",
            }
            for node_id in structure_by_id
        ],
        "edges": [
            {
                "id": edge["id"], "from": edge["from"], "to": edge["to"],
                "distance_m": edge["distance_m"],
                "verification": "openusd_point_collision_free_polyline",
            }
            for edge in edges if edge["from"] in structure_ids and edge["to"] in structure_ids
        ],
        "verification_status": "openusd_static_collision_point_verified",
        "limitations": routes["limitations"],
    }

    densification = densify_artifacts(
        nav=nav,
        coords=coords,
        anchors=anchors,
        routes=routes,
        registry=registry,
        zones=zones,
        entrances=entrances,
        route_semantics=route_semantics,
        candidates=candidates,
        target_node_count=120,
        max_structure_edge_m=5.6,
    )

    print(f"Built {len(nav['nodes'])} nodes and {len(nav['edges'])} routed edges")
    print("  densification", densification)
    longest = sorted(route_diagnostics, key=lambda item: item[3], reverse=True)[:12]
    for item in longest:
        print("  route", item)

    if not args.write:
        print("Dry run complete; pass --write to update the sandbox")
        return

    for path, value in (
        (nav_path, nav), (coord_path, coords), (entrance_path, entrances),
        (candidates_path, candidates), (registry_path, registry),
        (zones_path, zones), (stops_path, stops), (anchors_path, anchors),
        (routes_path, routes), (route_semantics_path, route_semantics),
    ):
        save(path, value)

    scene_path = root / "scene/roboguard_hospital_sandbox.usda"
    # From navigation_sandbox/scene back to the project collection root.
    sublayer = "../../../医院一楼_AA机器人_20261004/医院一楼_AA机器人_20261004/打开这个场景.usda"
    write_scene(
        scene_path,
        sublayer,
        nav,
        coords,
        structure_ids,
        {item["node_id"] for item in stops.get("stops", [])},
    )
    print("Wrote synchronized navigation sandbox artifacts")


if __name__ == "__main__":
    main()
