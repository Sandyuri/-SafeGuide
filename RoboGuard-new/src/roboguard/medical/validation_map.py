"""Raw-map reader used only by the independent validator.

This module deliberately does not import planner views or search algorithms.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class ValidationMap:
    project_root: Path
    map_version: str
    map_id: str
    nodes: Mapping[str, Mapping[str, object]]
    edges: Mapping[str, Mapping[str, object]]
    directed_edges: Mapping[tuple[str, str], Mapping[str, object]]
    one_way_reverse: Mapping[tuple[str, str], Mapping[str, object]]
    zone_permissions: Mapping[str, tuple[str, ...]]
    zone_roles: Mapping[str, tuple[str, ...]]
    route_semantics: Mapping[str, Mapping[str, object]]
    risk_zones: tuple[Mapping[str, object], ...]
    rules: Mapping[str, object]

    def edge(self, left: str, right: str) -> Mapping[str, object] | None:
        return self.directed_edges.get((left, right))


def _read_json(path: Path) -> dict[str, object]:
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load validator input file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Validator input file must contain an object: {path}")
    return value


def load_validation_map(
    project_root: str | Path,
    *,
    navigation_path: str | Path | None = None,
    access_path: str | Path | None = None,
    bindings_path: str | Path | None = None,
    semantics_path: str | Path | None = None,
    risk_zones_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
    rules_path: str | Path | None = None,
) -> ValidationMap:
    """Load an immutable validation view from authoritative JSON files."""

    root = Path(project_root).resolve()
    resolve = lambda value, default: Path(value) if value else root / default
    nav = _read_json(resolve(navigation_path, "navigation_sandbox/maps/hospital_1f.navigation.json"))
    access = _read_json(resolve(access_path, "navigation_sandbox/semantics/access_zones.json"))
    bindings = _read_json(resolve(bindings_path, "data/navigation/hospital_1f.bindings.json"))
    semantics = _read_json(resolve(semantics_path, "navigation_sandbox/semantics/route_semantics.json"))
    risk = _read_json(resolve(risk_zones_path, "navigation_sandbox/semantics/hospital_1f.risk_zones.json"))
    manifest_file = resolve(
        manifest_path, "navigation_sandbox/manifests/hospital_1f.v1.0.json"
    )
    manifest = _read_json(manifest_file)
    rules = _read_json(resolve(rules_path, "config/route_validation_rules.json"))

    authoritative = manifest.get("authoritative_files", {})
    if authoritative:
        if not isinstance(authoritative, Mapping):
            raise ValueError("manifest authoritative_files must be an object")
        authoritative_root = manifest_file.parent.parent
        for relative, metadata in authoritative.items():
            if not isinstance(relative, str) or not isinstance(metadata, Mapping):
                raise ValueError("manifest authoritative file entry is invalid")
            expected = str(metadata.get("sha256", "")).lower()
            source = authoritative_root / relative
            if len(expected) != 64 or not source.is_file():
                raise ValueError(f"manifest authoritative file is missing or invalid: {relative}")
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(
                    f"manifest hash mismatch for {relative}: expected {expected}, got {actual}"
                )

    nodes = {str(item["id"]): item for item in nav.get("nodes", ())}
    edges = {str(item["id"]): item for item in nav.get("edges", ())}
    directed: dict[tuple[str, str], Mapping[str, object]] = {}
    reverse: dict[tuple[str, str], Mapping[str, object]] = {}
    for edge in edges.values():
        left, right = str(edge["from"]), str(edge["to"])
        directed[(left, right)] = edge
        if bool(edge.get("bidirectional")):
            directed[(right, left)] = edge
        else:
            reverse[(right, left)] = edge

    zone_roles = {
        str(zone["id"]): tuple(str(role) for role in zone.get("required_roles", ()))
        for zone in access.get("zones", ())
    }
    zone_permissions = {
        str(key): tuple(str(item) for item in value)
        for key, value in dict(bindings.get("zone_permissions", {})).items()
    }
    semantic_nodes = {
        str(key): value
        for key, value in dict(semantics.get("nodes", {})).items()
    }
    defaults = dict(semantics.get("defaults", {}))
    for node_id in nodes:
        semantic_nodes.setdefault(node_id, defaults)

    return ValidationMap(
        project_root=root,
        map_version=str(manifest.get("map_release_id", manifest.get("map_version", ""))),
        map_id=str(nav.get("map_id", "")),
        nodes=nodes,
        edges=edges,
        directed_edges=directed,
        one_way_reverse=reverse,
        zone_permissions=zone_permissions,
        zone_roles=zone_roles,
        route_semantics=semantic_nodes,
        risk_zones=tuple(risk.get("risk_zones", ())),
        rules=rules,
    )
