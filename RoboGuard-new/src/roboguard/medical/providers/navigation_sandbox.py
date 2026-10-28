"""Compose medical facts with the hospital navigation sandbox."""

from copy import deepcopy
import json
import math
from pathlib import Path

from roboguard.medical.providers.base import MedicalSceneProvider
from roboguard.medical.scene import MedicalScene, MedicalSceneError

OPEN = {"open", "open_with_authorization", "restricted", "emergency_only"}
STATUS_MAP = {
    "blocked": "BLOCKED",
    "task_only": "BLOCKED",
    "unknown": "UNKNOWN",
    "conflicted": "CONFLICTED",
}


def _load(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MedicalSceneError(f"cannot load {label}: {error}") from error
    if not isinstance(value, dict):
        raise MedicalSceneError(f"{label} must be a JSON object")
    return value


class NavigationSandboxSceneProvider(MedicalSceneProvider):
    """Make sandbox topology authoritative while retaining medical entities."""

    def __init__(
        self,
        base_scene_path,
        sandbox_root,
        bindings_path,
        *,
        risk_rules_path=None,
    ):
        self.base_scene_path = Path(base_scene_path)
        self.sandbox_root = Path(sandbox_root)
        self.bindings_path = Path(bindings_path)
        self.risk_rules_path = (
            Path(risk_rules_path)
            if risk_rules_path is not None
            else Path(__file__).resolve().parents[4]
            / "config"
            / "route_validation_rules.json"
        )
        self.location_aliases = {}
        self.task_only_location_names = frozenset()
        self.safe_stop_node_ids = frozenset()

    def load_scene(self):
        base = _load(self.base_scene_path, "base medical scene")
        nav = _load(self.sandbox_root / "maps/hospital_1f.navigation.json", "navigation map")
        coords = _load(self.sandbox_root / "maps/hospital_1f.coordinate_mapping.json", "coordinate mapping")
        registry = _load(self.sandbox_root / "semantics/location_registry.json", "location registry")
        zones = _load(self.sandbox_root / "semantics/access_zones.json", "access zones")
        stops = _load(self.sandbox_root / "semantics/safe_stops.json", "safe stops")
        route_semantics = _load(
            self.sandbox_root / "semantics/route_semantics.json",
            "route semantics",
        )
        risk_zones = _load(
            self.sandbox_root / "semantics/hospital_1f.risk_zones.json",
            "risk zones",
        )
        risk_rules = _load(
            self.risk_rules_path,
            "route risk rules",
        )
        task_destinations = _load(
            self.sandbox_root / "semantics/task_destinations.json",
            "task destinations",
        )
        bindings = _load(self.bindings_path, "sandbox bindings")
        if {nav.get("map_id"), coords.get("map_id"), registry.get("map_id"), stops.get("map_id"), route_semantics.get("map_id"), task_destinations.get("map_id")} != {"hospital_1f"}:
            raise MedicalSceneError("sandbox artifacts do not share map_id=hospital_1f")

        nodes = {item["id"]: item for item in nav.get("nodes", [])}
        if len(nodes) != len(nav.get("nodes", [])) or not nodes:
            raise MedicalSceneError("sandbox requires unique, non-empty nodes")
        xyz = coords.get("node_coordinates", {})
        links = {node_id: set() for node_id in nodes}
        connection_costs = {node_id: {} for node_id in nodes}
        connection_risk_rates = {node_id: {} for node_id in nodes}
        configured_edge_rates = risk_rules.get("risk", {}).get(
            "edge_risk_per_meter", {}
        )
        edge_risk_rates = {}
        for zone in risk_zones.get("risk_zones", []):
            zone_type = str(zone.get("type", "")).upper()
            if zone_type not in configured_edge_rates:
                raise MedicalSceneError(
                    f"risk zone type {zone_type!r} has no configured unit risk"
                )
            component = zone_type.lower()
            rate = float(configured_edge_rates[zone_type])
            for edge_id in zone.get("affected_edges", []):
                edge_risk_rates.setdefault(edge_id, {})[component] = rate
        for edge in nav.get("edges", []):
            source, target = edge.get("from"), edge.get("to")
            if source not in nodes or target not in nodes or source == target:
                raise MedicalSceneError(f"invalid sandbox edge {edge.get('id')}")
            left = xyz.get(source, {}).get("usd_xyz_m")
            right = xyz.get(target, {}).get("usd_xyz_m")
            if not self._xyz(left) or not self._xyz(right):
                raise MedicalSceneError(f"edge {edge.get('id')} lacks USD coordinates")
            distance_m = edge.get("distance_m")
            if (
                isinstance(distance_m, bool)
                or not isinstance(distance_m, (int, float))
                or not math.isfinite(distance_m)
                or distance_m <= 0
            ):
                raise MedicalSceneError(
                    f"edge {edge.get('id')} has invalid distance_m"
                )
            # A routed corridor may be longer than the straight line between
            # its endpoint nodes, but it cannot be materially shorter.
            if distance_m + 0.02 < math.dist(left, right):
                raise MedicalSceneError(
                    f"edge {edge.get('id')} distance is shorter than its endpoints"
                )

            # Task-route edges remain authoritative map evidence, but they are
            # never exposed to the general navigation graph.  A dedicated task
            # executor must opt into them through task_destinations.json.
            if (
                edge.get("general_navigation_allowed") is False
                or edge.get("routing_scope") == "medication_delivery_only"
            ):
                continue

            self._add_connection(
                links,
                connection_costs,
                source,
                target,
                float(distance_m),
                edge.get("id"),
            )
            connection_risk_rates[source][target] = dict(
                edge_risk_rates.get(edge.get("id"), {})
            )
            if edge.get("bidirectional") is True:
                self._add_connection(
                    links,
                    connection_costs,
                    target,
                    source,
                    float(distance_m),
                    edge.get("id"),
                )
                connection_risk_rates[target][source] = dict(
                    edge_risk_rates.get(edge.get("id"), {})
                )

        permission_map = bindings.get("zone_permissions", {})
        medical_zones = {}
        for zone in zones.get("zones", []):
            zone_id = zone.get("id")
            permissions = permission_map.get(zone_id)
            if not isinstance(permissions, list):
                raise MedicalSceneError(f"missing permission binding for {zone_id}")
            medical_zones[zone_id] = {"id": zone_id,
                "area_type": "NORMAL" if not permissions else "RESTRICTED",
                "required_permissions": permissions}

        semantic_defaults = route_semantics.get("defaults", {})
        semantic_nodes = route_semantics.get("nodes", {})
        unknown_semantic_nodes = sorted(set(semantic_nodes) - set(nodes))
        if unknown_semantic_nodes:
            raise MedicalSceneError(
                f"route semantics reference unknown nodes: {unknown_semantic_nodes}"
            )
        safe_stop_ids = {item.get("node_id") for item in stops.get("stops", [])}
        task_node_ids = {
            item.get("node_id") for item in task_destinations.get("destinations", [])
        }
        if None in task_node_ids or not task_node_ids <= set(nodes):
            raise MedicalSceneError("task destinations reference unknown nodes")
        if any(nodes[node_id].get("node_class") != "task_location" for node_id in task_node_ids):
            raise MedicalSceneError("task destinations must use task_location nodes")
        regions = {}
        for node_id, node in nodes.items():
            point = xyz.get(node_id, {}).get("usd_xyz_m")
            zone_id = node.get("access_zone")
            if not self._xyz(point) or zone_id not in medical_zones:
                raise MedicalSceneError(f"invalid coordinate or zone for {node_id}")
            semantic = dict(semantic_defaults)
            semantic.update(semantic_nodes.get(node_id, {}))
            raw_status = str(node.get("navigation_status", "unknown")).lower()
            status = "OPEN" if raw_status in OPEN else STATUS_MAP.get(raw_status, "UNKNOWN")
            regions[node_id] = {"id": node_id, "display_name": node["name_zh"],
                "type": node["region_type"].upper(), "coords": point[:2],
                "connections": sorted(links[node_id]), "access_zone_id": zone_id,
                "connection_costs": {
                    neighbor: connection_costs[node_id][neighbor]
                    for neighbor in sorted(connection_costs[node_id])
                },
                "connection_risk_rates": {
                    neighbor: connection_risk_rates[node_id][neighbor]
                    for neighbor in sorted(connection_risk_rates[node_id])
                },
                "navigation_status": status,
                "navigation_status_valid_until": semantic.get("navigation_status_valid_until"),
                "prohibited_cargo_kinds": list(semantic.get("prohibited_cargo_kinds", [])),
                "semantic_tags": list(semantic.get("semantic_tags", [])),
                "route_policy": semantic.get("route_policy", "ALLOW_TRANSIT"),
                "safe_stop": node_id in safe_stop_ids}

        self.location_aliases = self._aliases(registry, nodes)
        self.task_only_location_names = frozenset(
            variant
            for item in registry.get("locations", [])
            if item.get("resolution_scope") == "task_only"
            for name in [item.get("canonical_name"), *item.get("aliases", [])]
            if isinstance(name, str) and name.strip()
            for variant in {name, name.split("（", 1)[0].strip()}
            if variant
        )
        self.safe_stop_node_ids = self._stops(stops, nav, xyz, nodes)
        result = deepcopy(base)
        result.update({"regions": regions, "access_zones": medical_zones,
            "scene_id": base["scene_id"] + "-navigation-sandbox"})
        result["snapshot"]["id"] += "-navigation-sandbox"
        result["snapshot"]["version"] += 1
        result["source"]["system"] = "hospital_1f-navigation-sandbox"
        result["provenance"].append({"source_system": "navigation_sandbox/hospital_1f",
            "evidence_id": "navigation-coordinate-semantics-composition-v1",
            "observed_at": result["snapshot"]["captured_at"]})
        self._bind(result, bindings)
        return MedicalScene.from_mapping(result)

    @staticmethod
    def _xyz(value):
        return isinstance(value, list) and len(value) == 3 and all(isinstance(x, (int, float)) for x in value)

    @staticmethod
    def _add_connection(
        links,
        connection_costs,
        source,
        target,
        distance_m,
        edge_id,
    ):
        if target in connection_costs[source]:
            raise MedicalSceneError(
                f"duplicate sandbox connection {source} -> {target} at edge {edge_id}"
            )
        links[source].add(target)
        connection_costs[source][target] = distance_m

    @staticmethod
    def _aliases(registry, nodes):
        result = {}
        for item in registry.get("locations", []):
            node_id = item.get("node_id")
            if node_id not in nodes:
                raise MedicalSceneError("location registry references an unknown node")
            if item.get("resolution_scope") == "task_only":
                continue
            for name in [item.get("canonical_name"), *item.get("aliases", [])]:
                if not isinstance(name, str) or not name.strip():
                    raise MedicalSceneError("location names must be non-empty")
                if name in result and result[name] != node_id:
                    raise MedicalSceneError(f"ambiguous location alias: {name}")
                result[name] = node_id
        return result

    @staticmethod
    def _stops(stops, nav, xyz, nodes):
        actual = set()
        for stop in stops.get("stops", []):
            node_id = stop.get("node_id")
            if node_id not in nodes or stop.get("usd_xyz_m") != xyz[node_id].get("usd_xyz_m"):
                raise MedicalSceneError(f"invalid safe stop {node_id}")
            actual.add(node_id)
        return frozenset(actual)

    @staticmethod
    def _bind(data, bindings):
        for key, value in bindings.get("patient_regions", {}).items():
            data["patients"][key]["region_id"] = value
        for key, value in bindings.get("medication_storage_regions", {}).items():
            data["medications"][key]["storage_region_id"] = value
        for key, values in bindings.get("delivery_item_locations", {}).items():
            for field in ("current_region_id", "destination_region_id"):
                if field in values:
                    data["delivery_items"][key][field] = values[field]
        data["robot_state"]["current_region_id"] = bindings["robot_current_region_id"]

    def resolve_location(self, text):
        if any(name in text for name in self.task_only_location_names):
            return None
        matches = [(name, node) for name, node in self.location_aliases.items() if name in text]
        if not matches:
            return None
        longest = max(len(name) for name, _ in matches)
        winners = {node for name, node in matches if len(name) == longest}
        if len(winners) != 1:
            raise MedicalSceneError("navigation request contains an ambiguous location")
        return next(iter(winners))

    def bind_navigation_context(
        self,
        context,
        text,
        scene,
        *,
        destination_id=None,
    ):
        """Bind one registry-verified destination into SafeGate context.

        ``destination_id`` supports aliases such as "nearest restroom", where
        route selection resolves a concrete candidate instead of the registry's
        legacy default node.
        """

        destination = destination_id or self.resolve_location(text)
        result = deepcopy(context)
        if destination is None:
            return result
        if destination not in scene.data["regions"]:
            raise MedicalSceneError(
                f"navigation context references unknown destination {destination}"
            )
        region = scene.data["regions"][destination]
        zone = scene.data["access_zones"][region["access_zone_id"]]
        required = set(zone["required_permissions"])
        granted = set(scene.data["robot_state"]["access_permissions"])
        matched_aliases = {
            name: destination
            for name in self.location_aliases
            if name in text
        }
        if not matched_aliases and isinstance(text, str) and text.strip():
            matched_aliases[text.strip()] = destination
        result["reference_resolution"]["location_aliases"] = matched_aliases
        result["environment"].update({"target_location_id": destination,
            "target_location_resolution": "RESOLVED", "target_area_type": zone["area_type"],
            "target_area_access": "GRANTED" if required <= granted else "DENIED"})
        return result
