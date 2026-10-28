import logging
from typing import Any, Dict, List, Tuple

from spine.mapping.graph_util import GraphHandler


class GraphSim:
    """Simulate dynamically updated graph."""

    def __init__(self, full_graph: str, init_graph: str, init_node: str) -> None:
        self.full_graph = GraphHandler(full_graph, init_node=init_node)
        # init_graph = GraphHandler(init_graph, init_node=init_node)

        # graph to be updated during exploration
        self.working_graph = GraphHandler(init_graph, init_node=init_node)

        self.seen_nodes = set(self.working_graph.graph.nodes)
        self.seen_edges = set(tuple(sorted(e)) for e in self.working_graph.graph.edges)

        self.seen_nodes.add("misc_1")
        extra_nodes = set(self.seen_nodes - set(self.full_graph.graph.nodes))

        if len(extra_nodes):
            logging.warn(
                f"Following nodes in init_graph but not in full_graph: {extra_nodes}"
            )

    def explore(
        self, node: str
    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        """Explore `node` in graph

        Parameters
        ----------
        node : str
            Node name, which is expected to be a region node.

        Returns
        -------
        Dict[str, Dict[str, Any]]
            Dictionary of (node, attribute) key-value pairs.
            Only newly discovered nodes are returned.
        """
        assert self.full_graph.contains_node(node), f"{node} not in graph"

        neighbors = self.full_graph.get_neighbors_by_type(node)

        new_neighbors_names = set(neighbors.keys()) - self.seen_nodes
        self.seen_nodes.update(new_neighbors_names)

        # add new nodes
        new_neighbors = {
            new_neighbor: neighbors[new_neighbor]
            for new_neighbor in new_neighbors_names
        }

        for node_id, attrs in new_neighbors.items():
            self.working_graph.update_with_node(node=node_id, edges=[node], attrs=attrs)

        # add new edges
        edges_to_be_added = set(tuple(sorted([node, n])) for n in new_neighbors_names)
        border_edges = set(tuple(sorted(e)) for e in self.full_graph.get_edges(node))
        new_edges = border_edges - edges_to_be_added
        new_edges -= self.seen_edges
        self.seen_edges.update(new_edges)
        new_edges = {e: self.full_graph.graph.edges[e] for e in new_edges}

        for edge, attrs in new_edges.items():
            self.working_graph.update_with_edge(edge, attrs)

        return new_neighbors, new_edges

    def inspect(self):
        pass

    def remove_quotes(self, in_str: str) -> str:
        return in_str.replace("'", "").replace('"', "")

    def dict_to_str(self, in_dict: Dict[Any, Any]) -> str:
        other_attrs = in_dict.copy()
        other_attrs.pop("type")
        attr_str = str(other_attrs)
        return self.remove_quotes(attr_str)

    def get_update_api(
        self,
        root_node: str,
        new_nodes: Dict[str, List[str]],
        new_edges: Dict[Tuple[str, str], List[str]],
    ) -> List[str]:
        updates = []
        for node_id, attrs in new_nodes.items():
            node_type = attrs["type"]
            attr_str = self.dict_to_str(attrs)
            updates.append(f"add_node({node_type}, {node_id}, attributes={attr_str})")
            updates.append(
                f"add_connection({node_type}_connection, {node_id}, {root_node})"
            )
        for edge, attrs in new_edges.items():
            node_type = attrs["type"]
            updates.append(
                f"add_connection({node_type}_connection, {edge[0]}, {edge[1]})"
            )
        return updates
