"""Safe serialization helpers for the active, de-identified scene graph."""

from copy import deepcopy
import json
from typing import Any, Mapping


def active_scene_graph(graph: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(graph, Mapping):
        raise TypeError("scene graph must be a mapping")
    return {
        str(key): deepcopy(value)
        for key, value in graph.items()
        if not str(key).startswith("extra_")
    }


def active_scene_json(graph: Mapping[str, Any]) -> str:
    return json.dumps(active_scene_graph(graph), ensure_ascii=False, indent=2)
