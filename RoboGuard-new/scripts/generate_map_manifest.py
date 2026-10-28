"""Generate a review or frozen manifest for the authoritative experiment map."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path

from validate_experiment_map import validate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--release-id", default="hospital_1f.v1.0")
    parser.add_argument("--status", choices=("under_review", "frozen_for_evaluation"), default="under_review")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    allow_provisional = args.status == "under_review"
    report = validate(root, allow_provisional=allow_provisional)
    if report["status"] == "INVALID":
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    if args.status == "frozen_for_evaluation" and report["status"] != "READY_TO_FREEZE":
        raise SystemExit("refusing to freeze a map with provisional review items")
    manifest = {
        "schema_version": "1.0.0",
        "map_release_id": args.release_id,
        "map_id": report["map_id"],
        "status": args.status,
        "generated_on": date.today().isoformat(),
        "node_count": report["node_count"],
        "edge_count": report["edge_count"],
        "safe_stop_count": report["safe_stop_count"],
        "authoritative_files": {
            relative: {"sha256": digest}
            for relative, digest in report["hashes"].items()
        },
        "review": {
            "status": "pending" if allow_provisional else "approved",
            "reviewers": [],
            "warning_count": len(report["warnings"]),
            "note": (
                "Static topology is valid; manual node, edge, accessibility, width, and risk-zone review remains."
                if allow_provisional
                else "All freeze gates passed."
            ),
        },
    }
    output = args.output or root / f"manifests/{args.release_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
