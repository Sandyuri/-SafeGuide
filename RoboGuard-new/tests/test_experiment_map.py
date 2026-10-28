from pathlib import Path
import unittest

from scripts.densify_navigation_topology import densify_artifacts, load
from scripts.validate_experiment_map import validate


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SANDBOX_ROOT = PROJECT_ROOT / "navigation_sandbox"


class ExperimentMapTests(unittest.TestCase):
    def test_review_map_is_structurally_valid(self):
        report = validate(SANDBOX_ROOT, allow_provisional=True)
        self.assertEqual(report["status"], "VALID_FOR_REVIEW")
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["node_count"], 135)
        self.assertEqual(report["edge_count"], 176)

    def test_map_cannot_freeze_before_manual_review(self):
        report = validate(SANDBOX_ROOT, allow_provisional=False)
        self.assertEqual(report["status"], "INVALID")
        self.assertTrue(any("not manually verified" in item for item in report["errors"]))

    def test_densification_is_idempotent_at_target_count(self):
        paths = {
            "nav": SANDBOX_ROOT / "maps/hospital_1f.navigation.json",
            "coords": SANDBOX_ROOT / "maps/hospital_1f.coordinate_mapping.json",
            "anchors": SANDBOX_ROOT / "artifacts/floorplan/hospital_1f.floorplan_anchor_mapping.json",
            "routes": SANDBOX_ROOT / "artifacts/floorplan/hospital_1f.edge_routes.json",
            "registry": SANDBOX_ROOT / "semantics/location_registry.json",
            "zones": SANDBOX_ROOT / "semantics/access_zones.json",
            "entrances": SANDBOX_ROOT / "maps/hospital_1f.entrance_mapping.json",
            "route_semantics": SANDBOX_ROOT / "semantics/route_semantics.json",
        }
        artifacts = {name: load(path) for name, path in paths.items()}
        summary = densify_artifacts(**artifacts, target_node_count=120)
        self.assertEqual(summary["status"], "already_dense")


if __name__ == "__main__":
    unittest.main()
