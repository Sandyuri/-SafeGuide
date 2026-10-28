from typing import Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.axes import Axes
from matplotlib.figure import Figure

from spine.mapping.graph_util import GraphHandler


def get_node_color(node_type: str) -> str:
    if node_type == "region":
        return "orange"
    elif node_type == "object":
        return "blue"
    else:
        raise ValueError()


def coord_to_xy(*coord: Tuple[float, float]) -> Tuple[float, float]:
    """coordinates are interpreted as north/south, east/west"""
    x = [c[0] for c in coord]
    y = [c[1] for c in coord]

    return np.array([x, y])


def plot_graph(
    graph_handle: GraphHandler,
    plot_edge_names: Optional[bool] = False,
    plot_node_names: Optional[bool] = True,
    figsize: Optional[Tuple[int, int]] = None,
) -> Tuple[Figure, Axes]:
    args = {}
    if figsize:
        args["figsize"] = figsize
    fig, ax = plt.subplots(**args)

    for n1, n2 in graph_handle.graph.edges:
        start = graph_handle.lookup_node(n1)[0]["coords"]
        end = graph_handle.lookup_node(n2)[0]["coords"]
        loc = coord_to_xy(start, end)

        ax.plot(loc[0, :], loc[1, :], color="black", linewidth=1)

    for node in graph_handle.graph.nodes:
        attr = graph_handle.graph.nodes[node]

        loc = coord_to_xy(attr["coords"])
        ax.scatter(loc[0], loc[1], color=get_node_color(attr["type"]))

        if attr["type"] == "object" and plot_node_names:
            text_buffer = 3
            ax.text(loc[0] + text_buffer, loc[1], node, fontsize=8, alpha=0.9)

        if attr["type"] == "region" and plot_edge_names:
            text_buffer = 3
            ax.text(loc[0] + text_buffer, loc[1], node, fontsize=8, alpha=0.9)

    return fig, ax
