"""Move only a node's visible map marker; never change navigation geometry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ANCHORS = (
    PROJECT_ROOT
    / "navigation_sandbox/artifacts/floorplan/hospital_1f.floorplan_anchor_mapping.json"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--node", help="Unique node ID or exact Chinese name")
    selector.add_argument("--number", type=int, help="Number shown in the map legend")
    parser.add_argument("--x", type=int, help="Display X in source floorplan pixels")
    parser.add_argument("--y", type=int, help="Display Y in source floorplan pixels")
    parser.add_argument("--reset", action="store_true", help="Remove the display-only override")
    parser.add_argument("--dry-run", action="store_true", help="Show the change without writing")
    parser.add_argument("--anchors", type=Path, default=DEFAULT_ANCHORS)
    args = parser.parse_args()

    if args.reset == (args.x is not None and args.y is not None):
        parser.error("Provide either --reset or both --x and --y")
    anchors = json.loads(args.anchors.read_text(encoding="utf-8"))
    width, height = anchors["source_image_size_px"]
    if not args.reset and not (0 <= args.x < width and 0 <= args.y < height):
        parser.error(f"Display pixel must be inside 0..{width - 1}, 0..{height - 1}")

    matches = [
        item for item in anchors["nodes"]
        if (args.number is not None and item["display_number"] == args.number)
        or (args.node is not None and args.node in (item["node_id"], item["name_zh"]))
    ]
    if len(matches) != 1:
        requested = args.number if args.number is not None else args.node
        parser.error(f"Expected exactly one node matching {requested!r}, found {len(matches)}")
    item = matches[0]
    route_pixel = item["floorplan_pixel_xy"]
    before = item.get("display_floorplan_pixel_xy", route_pixel)
    if args.reset:
        item.pop("display_floorplan_pixel_xy", None)
        after = route_pixel
    else:
        item["display_floorplan_pixel_xy"] = [args.x, args.y]
        after = item["display_floorplan_pixel_xy"]

    print(f"{item['name_zh']} ({item['node_id']})")
    print(f"Route/ USD-linked pixel: {route_pixel} (unchanged)")
    print(f"Map marker: {before} -> {after}")
    if args.dry_run:
        print("Dry run: no file changed")
    else:
        args.anchors.write_text(
            json.dumps(anchors, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Updated: {args.anchors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
