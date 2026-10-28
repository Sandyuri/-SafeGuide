"""Generate an editable draw.io overlay for the hospital navigation graph."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from xml.etree import ElementTree as ET


MAP_X, MAP_Y, MAP_SIZE = 20.0, 100.0, 1600.0
SOURCE_W, SOURCE_H = 6709.0, 6702.0


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def scaled(point):
    return (
        round(MAP_X + point[0] / SOURCE_W * MAP_SIZE, 2),
        round(MAP_Y + point[1] / SOURCE_H * MAP_SIZE, 2),
    )


def cell(element, **attributes):
    return ET.SubElement(element, "mxCell", {key: str(value) for key, value in attributes.items()})


def geometry(element, **attributes):
    return ET.SubElement(element, "mxGeometry", {key: str(value) for key, value in attributes.items()})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True, help="navigation_sandbox directory")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output or root / "artifacts/floorplan/hospital_1f.navigation_overlay_latest.drawio"
    nav = load(root / "maps/hospital_1f.navigation.json")
    anchors = load(root / "artifacts/floorplan/hospital_1f.floorplan_anchor_mapping.json")
    routes = load(root / "artifacts/floorplan/hospital_1f.edge_routes.json")
    safe_stops = load(root / "semantics/safe_stops.json")
    safe_stop_ids = {item["node_id"] for item in safe_stops.get("stops", [])}
    source = (root / "artifacts/floorplan/source/hospital_1f_floorplan.jpg").resolve()
    anchor_by_id = {item["node_id"]: item for item in anchors["nodes"]}
    display_by_id = {item["node_id"]: item["display_number"] for item in anchors["nodes"]}

    mxfile = ET.Element("mxfile", {"host": "drawio", "version": "26.0.0"})
    diagram = ET.SubElement(mxfile, "diagram", {"name": "医院一层点机器人导航图"})
    model = ET.SubElement(diagram, "mxGraphModel", {
        "dx": "1600", "dy": "900", "grid": "1", "gridSize": "10", "guides": "1",
        "tooltips": "1", "connect": "1", "arrows": "1", "fold": "1", "page": "1",
        "pageScale": "1", "pageWidth": "3000", "pageHeight": "1800", "math": "0", "shadow": "0",
    })
    root_xml = ET.SubElement(model, "root")
    cell(root_xml, id="0")
    cell(root_xml, id="1", parent="0")

    title = cell(root_xml, id="title", value="RoboGuard 医院一层｜OpenUSD 点机器人全量导航图",
                 style="text;html=1;align=left;verticalAlign=middle;fontSize=24;fontStyle=1;fontColor=#172554;",
                 vertex="1", parent="1")
    geometry(title, x="20", y="20", width="1550", height="50", **{"as": "geometry"})

    image_uri = source.as_uri()
    background = cell(root_xml, id="floorplan", value="",
                      style=f"shape=image;html=1;imageAspect=0;image={image_uri};container=1;pointerEvents=0;",
                      vertex="1", parent="1")
    geometry(background, x=MAP_X, y=MAP_Y, width=MAP_SIZE, height=MAP_SIZE, **{"as": "geometry"})

    node_cell = {}
    for index, node in enumerate(nav["nodes"], 1):
        node_id = node["id"]
        anchor = anchor_by_id[node_id]
        node_cell[node_id] = (
            f"route-node-{index}"
            if anchor.get("display_floorplan_pixel_xy", anchor["floorplan_pixel_xy"])
            != anchor["floorplan_pixel_xy"]
            else f"node-{index}"
        )

    # Edges are children of the floor-plan container and are emitted before
    # nodes so bubbles remain readable.
    for index, edge in enumerate(nav["edges"], 1):
        edge_id = edge["id"]
        if edge.get("routing_scope") == "medication_delivery_only":
            edge_style = "edgeStyle=none;rounded=1;orthogonalLoop=1;jettySize=auto;html=1;endArrow=none;dashed=1;dashPattern=6 4;strokeColor=#9333ea;strokeWidth=3;fontSize=8;fontStyle=1;fontColor=#6b21a8;labelBackgroundColor=#ffffff;"
        else:
            edge_style = "edgeStyle=none;rounded=1;orthogonalLoop=1;jettySize=auto;html=1;endArrow=none;strokeColor=#0891b2;strokeWidth=2;fontSize=8;fontColor=#075985;labelBackgroundColor=#ffffff;"
        edge_cell = cell(
            root_xml,
            id=f"edge-{index}",
            value=f"{edge_id}  {edge['distance_m']:.2f} m",
            style=edge_style,
            edge="1",
            parent="floorplan",
            source=node_cell[edge["from"]],
            target=node_cell[edge["to"]],
        )
        geo = geometry(edge_cell, relative="1", **{"as": "geometry"})
        waypoints = routes["edge_routes"].get(edge_id, [])
        if waypoints:
            array = ET.SubElement(geo, "Array", {"as": "points"})
            for point in waypoints:
                x, y = scaled(point)
                ET.SubElement(array, "mxPoint", {"x": str(round(x - MAP_X, 2)), "y": str(round(y - MAP_Y, 2))})

    for index, node in enumerate(nav["nodes"], 1):
        node_id = node["id"]
        anchor = anchor_by_id[node_id]
        route_x, route_y = scaled(anchor["floorplan_pixel_xy"])
        x, y = scaled(anchor.get("display_floorplan_pixel_xy", anchor["floorplan_pixel_xy"]))
        x -= MAP_X
        y -= MAP_Y
        if node_cell[node_id] != f"node-{index}":
            route_anchor = cell(root_xml, id=node_cell[node_id], value="",
                                style="ellipse;opacity=0;fillColor=none;strokeColor=none;",
                                vertex="1", parent="floorplan")
            geometry(route_anchor, x=round(route_x - MAP_X - 1, 2),
                     y=round(route_y - MAP_Y - 1, 2), width="2", height="2",
                     **{"as": "geometry"})
            leader = cell(root_xml, id=f"display-leader-{index}", value="",
                          style="edgeStyle=none;dashed=1;dashPattern=3 2;endArrow=none;strokeColor=#64748b;strokeWidth=1;",
                          edge="1", parent="floorplan", source=node_cell[node_id],
                          target=f"node-{index}")
            geometry(leader, relative="1", **{"as": "geometry"})
        if node.get("node_class") == "navigation_structure":
            fill, stroke = "#f8cecc", "#b85450"
        elif node.get("node_class") in {"task_location", "task_route_structure"}:
            fill, stroke = "#e1d5e7", "#9673a6"
        elif node_id in safe_stop_ids:
            fill, stroke = "#d5e8d4", "#82b366"
        elif node.get("access_zone") == "clinical_controlled":
            fill, stroke = "#fff2cc", "#d6b656"
        else:
            fill, stroke = "#dae8fc", "#6c8ebf"
        label = f"{display_by_id[node_id]:02d}"
        item = cell(root_xml, id=node_cell[node_id], value=label,
                    style=f"ellipse;whiteSpace=wrap;html=1;align=center;verticalAlign=middle;fontSize=8;fontStyle=1;fillColor={fill};strokeColor={stroke};strokeWidth=2;",
                    vertex="1", parent="floorplan")
        geometry(item, x=round(x - 9, 2), y=round(y - 9, 2), width="18", height="18", **{"as": "geometry"})

    legend = cell(root_xml, id="legend", value="节点索引",
                  style="rounded=0;whiteSpace=wrap;html=1;fillColor=#ffffff;strokeColor=#94a3b8;verticalAlign=top;fontStyle=1;fontSize=18;container=1;pointerEvents=0;",
                  vertex="1", parent="1")
    geometry(legend, x="1660", y="100", width="1300", height="1600", **{"as": "geometry"})
    for index, node in enumerate(nav["nodes"], 1):
        column = (index - 1) // 23
        row = (index - 1) % 23
        x = 20 + column * 420
        y = 45 + row * 65
        if node.get("node_class") == "navigation_structure":
            fill, stroke = "#f8cecc", "#b85450"
        elif node.get("node_class") in {"task_location", "task_route_structure"}:
            fill, stroke = "#e1d5e7", "#9673a6"
        elif node["id"] in safe_stop_ids:
            fill, stroke = "#d5e8d4", "#82b366"
        elif node.get("access_zone") == "clinical_controlled":
            fill, stroke = "#fff2cc", "#d6b656"
        else:
            fill, stroke = "#dae8fc", "#6c8ebf"
        bubble = cell(root_xml, id=f"legend-bubble-{index}", value=f"{display_by_id[node['id']]:02d}",
                      style=f"ellipse;html=1;align=center;verticalAlign=middle;fontSize=8;fontStyle=1;fillColor={fill};strokeColor={stroke};",
                      vertex="1", parent="legend")
        geometry(bubble, x=x, y=y, width="24", height="24", **{"as": "geometry"})
        text = cell(root_xml, id=f"legend-text-{index}", value=f"{node['name_zh']}&#xa;{node['id']}",
                    style="text;html=1;align=left;verticalAlign=top;fontSize=10;fontColor=#172554;whiteSpace=wrap;",
                    vertex="1", parent="legend")
        geometry(text, x=x + 32, y=y - 3, width="370", height="48", **{"as": "geometry"})

    output.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(mxfile)
    ET.indent(tree, space="  ")
    tree.write(output, encoding="utf-8", xml_declaration=True)
    print(output)


if __name__ == "__main__":
    main()
