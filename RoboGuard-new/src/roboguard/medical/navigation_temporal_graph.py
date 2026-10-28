"""Build the de-identified semantic graph supplied to RoboGuard."""

from roboguard.medical.planner_view import PlannerSceneView


def build_navigation_temporal_graph(view: PlannerSceneView) -> dict:
    regions = view.payload["regions"]
    edges = set()
    for name, region in regions.items():
        for neighbor in region["connections"]:
            edges.add(tuple(sorted((name, neighbor))))
    return {
        "schema_version": "1.0.0",
        "regions": [
            {
                "name": name,
                "type": region["type"],
                "area_type": region["area_type"],
                "semantic_tags": list(region.get("semantic_tags", ())),
                "route_policy": region.get("route_policy", "ALLOW_TRANSIT"),
                "navigation_status": region.get("navigation_status", "UNKNOWN"),
                "required_permissions": list(region.get("required_permissions", ())),
                "prohibited_cargo_kinds": list(region.get("prohibited_cargo_kinds", ())),
                "safe_stop": bool(region.get("safe_stop", False)),
            }
            for name, region in regions.items()
        ],
        "region_connections": [list(edge) for edge in sorted(edges)],
        "current_location": view.payload["robot"]["current_region_ref"],
        "task": {
            "type": view.payload["task"]["type"],
            "destination": view.payload["task"]["destination_region_ref"],
        },
    }
