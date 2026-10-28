import json
from typing import Any, Dict, List, Optional, Tuple, Union

import networkx as nx
import numpy as np
from scipy.spatial.transform import Rotation

EMPTY_GRAPH = {
    "objects": [],
    "regions": [{"name": "ground_1", "coords": [0, 0]}],
    "region_connections": [],
    "object_connections": [],
}


def to_list(x):
    if isinstance(x, list):
        return x
    else:
        return x.replace("[", "").replace("]", "").split(",")


def to_float_list(x: str):
    return [float(y) for y in to_list(x)]


def parse_graph_coord(
    coord_as_str: str, origin: np.ndarray, rotation: Optional[Rotation] = None
) -> List[float]:
    """Parsing coordinate from incoming graph.
    Coordinates are assumed to be ENU

    Parameters
    ----------
    coord_as_str : str
        Coordinate as a string `[x, y]`
    origin : np.ndarray
        Origin of coordinate system
    rotation : Optional[Rotation], optional
        Rotation of coordinate system

    Returns
    -------
    List[float]
        coordinates `[x, y]` after SE2 transform defined
        by origin and rotation
    """
    # apply SE2 transform
    coords = np.array(to_float_list(coord_as_str))
    # print(f"--")
    # print(coords)
    coords = apply_transform(coords, origin=origin, rotation=rotation)

    # back to list
    coords = [round(float(coords[0]), 1), round(float(coords[1]), 1)]
    # print(coords)
    # print("--")

    return coords


def apply_transform(
    x: np.ndarray, origin: np.ndarray, rotation: Rotation
) -> np.ndarray:
    """Apply SE2 transform to x

    Note rot is an SO3 transform, so we lift x to R-3
    then map back to R-2, ignoring the third dim.

    Parameters
    ----------
    x : np.ndarray
        A vector in R-2
    origin : np.ndarray
        An origin in R-2
    rot : Rotation
        A rotation in SO3

    Returns
    -------
    np.ndarray
        Transformed vector
    """
    x -= origin
    if rotation != None:
        # print(f"rotation xyz: {rotation.as_euler('xyz')}")
        x = np.concatenate([x, np.zeros(1)])
        x = rotation.apply(x)
        # print(f"transformed x {x}")
        x = x[:2]

    return x


def parse_graph(
    data: Dict[str, Dict[str, str]],
    custom_data: Optional[Dict[str, Dict[str, str]]] = {},
    rotation: Optional[Rotation] = None,
    utm_origin: Optional[np.ndarray] = None,
    flip_coords=False,
) -> Tuple[nx.Graph, str]:
    """Parse scene graph in `data` into a networkx object.

    Parameters
    ----------
    data : Dict[str, Dict[str, str]]
        graph where keys-values are nodes-attributes
    rotation : Optional[Rotation]
        current rotation of robot

    Returns
    -------
    Tuple[nx.Graph, str]
        Networkx and string of json
    """
    origin = np.array([0, 0])
    """
    if "origin" in data:
        origin = utm_origin
        origin = np.array(to_float_list(data["origin"]))
        # print(f"original origin is: {origin}")
        origin = utm_origin - origin
    """
    if utm_origin is not None:
        origin = utm_origin

    if len(custom_data):
        add_keys = ["regions", "region_connections", "objects", "object_connections"]
        for key in add_keys:
            if key in data and key in custom_data:
                data[key].extend(custom_data[key])

    # print(f"origin: {origin}, rot: {rotation}")

    G = nx.Graph()
    for node in data["objects"]:
        c = node["coords"]
        # print(f"node: {node}, coords: {c}")
        coords = parse_graph_coord(node["coords"], origin=origin, rotation=rotation)
        if flip_coords:
            raise ValueError()
            # print("flipping coords")
            coords = [coords[0], -coords[1]]
        G.add_node(node["name"], coords=coords, type="object")

    for node in data["regions"]:
        assert "coords" in node, node
        c = node["coords"]
        # print(f"node: {node}, coords: {c}")
        coords = parse_graph_coord(node["coords"], origin=origin, rotation=rotation)

        if flip_coords:
            raise ValueError
            # print("flipping coords")
            coords = [coords[0], -coords[1]]

        G.add_node(node["name"], coords=coords, type="region")

    for edge in data["object_connections"]:
        c1 = G.nodes[edge[0]]["coords"]
        c2 = G.nodes[edge[1]]["coords"]
        # print(f"edge: {edge}, c1, c2: {c1}, {c2}")
        dist = np.linalg.norm(np.array(c1) - np.array(c2))
        G.add_edge(edge[0], edge[1], type="object", weight=dist)

    for edge in data["region_connections"]:
        c1 = G.nodes[edge[0]]["coords"]
        c2 = G.nodes[edge[1]]["coords"]
        # print(f"edge: {edge}, c1, c2: {c1}, {c2}")
        dist = np.linalg.norm(np.array(c1) - np.array(c2))
        G.add_edge(edge[0], edge[1], type="region", weight=dist)

    return G, str(data)


class GraphHandler:
    def __init__(self, graph_path: str, init_node: Union[None, str] = None) -> None:
        if graph_path == "":
            self.graph = nx.Graph()
            self.as_json_str = "{}"
            self.current_location = ""
        else:
            with open(graph_path) as f:
                data = json.load(f)
            self.graph, self.as_json_str = parse_graph(data)
            self.current_location = init_node

    def reset(
        self,
        graph_as_json: str,
        current_location: Optional[str] = "",
        rotation: Optional[Rotation] = None,
        utm_origin: Optional[np.ndarray] = None,
        custom_data: Optional[Dict[str, Dict[str, str]]] = {},
        flip_coords=False,
    ) -> bool:
        try:
            data = json.loads(graph_as_json)

            # TODO, logic is obtuse
            # priority is current location -> incoming argument -> value in data

            if self.current_location == None or self.current_location == "":
                self.current_location = current_location

            self.graph, self.as_json_str = parse_graph(
                data,
                rotation=rotation,
                utm_origin=utm_origin,
                custom_data=custom_data,
                flip_coords=flip_coords,
            )
            self.as_json_str = self.to_json_str()
        except Exception as ex:
            print(f"\nexception: {ex}")
            return False
        return True

    def to_json_str(self, extra_data={}) -> str:
        added_edges = set()
        graph_dict = {
            "objects": [],
            "regions": [],
            "object_connections": [],
            "region_connections": [],
        }
        for node in self.graph.nodes:
            node_type = self.graph.nodes[node]["type"]
            coords = self.graph.nodes[node]["coords"]
            coords = f"[{coords[0]:0.1f}, {coords[1]:0.1f}]"  # TODO best way?
            graph_dict[f"{node_type}s"].append({"name": node, "coords": coords})

            for neighbor in self.get_neighbors(node):
                if tuple(sorted((node, neighbor))) not in added_edges:
                    graph_dict[f"{node_type}_connections"].append(
                        sorted([node, neighbor])
                    )
                    added_edges.add(tuple(sorted((node, neighbor))))

        if self.current_location != None:
            graph_dict["current_location"] = self.current_location

        graph_dict.update(extra_data)

        return json.dumps(graph_dict, indent=2)

    def update_location(self, new_location: str) -> bool:
        if not self.contains_node(new_location):
            return False
        self.current_location = new_location
        return True

    def get_neighbors_by_type(
        self, node: str, node_type: Optional[str] = ""
    ) -> Dict[str, List[str]]:
        """Get neighbors of `node`

        Parameters
        ----------
        node : str
        type : Optional[str]
            If given, only return neighbors of this type

        Returns
        -------
        Dict[str, List[str]]
            node: attributes
        """
        ret_val = {}
        if node in self.graph.nodes:
            neighbors = list(self.graph.neighbors(node))

            for neighbor in neighbors:
                if node_type == "":
                    ret_val[neighbor] = self.lookup_node(neighbor)[0]
                elif (
                    node_type != "" and self.lookup_node(neighbor)[0]["type"] == "type"
                ):
                    ret_val[neighbor] = self.lookup_node(neighbor)[0]

            # return {node: self.lookup_node(node)[0] for node in neighbors}
        return ret_val

    def get_edges(self, node: str) -> Dict[str, List[str]]:
        out = {}
        for edge in self.graph.edges(node):
            out[edge] = self.graph.edges[edge]
        return out

    def lookup_object(
        self, node: str
    ) -> Tuple[Tuple[str, Dict], Tuple[str, Dict], bool]:
        """Check if an object is in the graph. If so, return the object
        attributes and the connecting region, if any

        Parameters
        ----------
        node : str
            _description_

        Returns
        -------
        Tuple[Tuple[str, Dict], Tuple[str, Dict], bool]:
        - object name, attributes
        - region name, attributes
        - true if all information found

        """
        if node in self.graph.nodes and self.graph.nodes[node]["type"] == "object":
            node_attr = self.graph.nodes[node]

            neighbors = list(self.graph.neighbors(node))

            # if found, just region first for now
            if len(neighbors) >= 1:
                # object-region connections are always (object, region) order
                region_attr = self.graph.nodes[neighbors[0]]
                return (
                    (node, node_attr),
                    (neighbors[0], region_attr),
                    True,
                )
            else:
                return (node, node_attr), (None, None), False

        return (None, None), (None, None), False

    def get_neighbors(self, node_name: str) -> List[str]:
        if not self.contains_node(node_name):
            return []
        return list(nx.neighbors(self.graph, node_name))

    def get_path(
        self, start_node, end_node, only_regions: Optional[bool] = False
    ) -> List[str]:
        return nx.shortest_path(self.graph, start_node, end_node)

    def contains_node(self, node: str) -> bool:
        return node in self.graph.nodes

    def path_exists_from_current_loc(self, target: str) -> bool:
        assert self.current_location != None, "current location is unknown"
        return nx.has_path(self.graph, self.current_location, target)

    def lookup_node(self, node: str) -> Tuple[Dict, bool]:
        if self.contains_node(node):
            return self.graph.nodes[node], True
        else:
            return {}, False

    def get_node_coords(self, node: str) -> Tuple[np.ndarray, bool]:
        if self.contains_node(node):
            return self.graph.nodes[node]["coords"], True
        else:
            return (
                np.zeros(
                    2,
                ),
                False,
            )

    def update_node_description(self, node, **attrs) -> None:
        self.graph.nodes[node].update(attrs)

    def get_node_type(self, node: str) -> str:
        node_info, success = self.lookup_node(node)
        if success and "type" in node_info:
            return node_info["type"]
        else:
            return ""

    def update_with_node(
        self,
        node: str,
        edges: List[str],
        attrs: Dict[str, Any] = {},
    ) -> None:
        assert "type" in attrs and "coords" in attrs
        self.graph.add_node(node, **attrs)
        for edge in edges:
            c1 = self.graph.nodes[node]["coords"]
            c2 = self.graph.nodes[edge]["coords"]
            dist = np.linalg.norm(np.array(c1) - np.array(c2))
            self.graph.add_edge(
                node, edge, type=self.graph.nodes[node]["type"], weight=dist
            )

    def update_with_edge(self, edge: Tuple[str, str], attrs: Dict[str, Any] = {}):
        self.graph.add_edge(edge[0], edge[1], **attrs)

    def remove_edge(self, start: str, end: str) -> None:
        try:  # TODO hacky should check if edge exists first
            # note edges are bidirectional
            self.graph.remove_edge(start, end)
        except Exception as ex:
            return

    # TODO figure out what we wanna do with this
    def get_region_nodes_and_locs(self) -> Tuple[np.ndarray, np.ndarray]:
        """Update set of region nodes. Assumes graph will be updated
        during operation.

        Updates
        - region_nodes: array of region names (str)
        - region_node_locs: array of region locations
        """

        region_nodes_locs = []
        region_nodes = []

        for node in self.graph.nodes:
            if self.graph.nodes[node]["type"] == "region":
                node_loc = self.graph.nodes[node]["coords"]
                region_nodes_locs.append(node_loc)
                region_nodes.append(node)

        # TODO finish
        self.region_nodes = np.array(region_nodes)
        self.region_node_locs = np.array(region_nodes_locs)

        return self.region_nodes, self.region_node_locs

    def get_closest_reachable_node(
        self, goal_node: str, current_node: Optional[str] = None
    ) -> str:
        """Get closest reachable node from current_node to goal_node

        Parameters
        ----------
        goal_node : str
        current_node : str

        Returns
        -------
        str
            closest reachable node from current_node to goal_node
        """
        if current_node == None:
            assert self.current_location != None, "current_location must be set"
            current_node = self.current_location

        nodes_reachable_from_curr_loc = list(
            nx.node_connected_component(self.graph, current_node)
        )
        nodes_reachable_from_goal = list(
            nx.node_connected_component(self.graph, goal_node)
        )

        # only consider region nodes
        nodes_reachable_from_curr_loc = [
            n
            for n in nodes_reachable_from_curr_loc
            if self.get_node_type(n) == "region"
        ]
        nodes_reachable_from_goal = [
            n for n in nodes_reachable_from_goal if self.get_node_type(n) == "region"
        ]

        coords_of_nodes_reachable_curr_loc = np.array(
            [self.get_node_coords(n)[0] for n in nodes_reachable_from_curr_loc]
        )

        closest_node_dist = np.inf
        closest_node_id = current_node
        target_node_id = goal_node
        for node_reachable_from_goal in nodes_reachable_from_goal:
            node_dists = np.linalg.norm(
                coords_of_nodes_reachable_curr_loc
                - np.array(self.get_node_coords(node_reachable_from_goal)[0]),
                axis=-1,
            )

            if node_dists.min() < closest_node_dist:
                closest_node_dist = node_dists.min()
                closest_node_id = nodes_reachable_from_curr_loc[node_dists.argmin()]
                target_node_id = node_reachable_from_goal

        # return nodes_reachable_from_curr_loc[closest_node]
        return closest_node_id, target_node_id

    def __str__(self) -> str:
        out = f"Nodes\n---\n"
        for node in self.graph.nodes:
            attrs, _ = self.lookup_node(node)
            out += f"\t{node}: {attrs}"

        object_edges = ""
        region_edges = ""
        for edge in self.graph.edges:
            if "object" in [self.get_node_type(e) for e in edge]:
                object_edges += f"\t[{edge[0]}, {edge[1]}]\n"
            else:
                region_edges += f"\t[{edge[0]}, {edge[1]}]\n"

        out += f"\nObject edges\n---\n"
        out += object_edges + "\n"

        out += f"Region edges:\n---\n"
        out += region_edges
        return out
