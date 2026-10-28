from geometry_msgs.msg import Point
from rospy import Publisher
import rospy
from std_msgs.msg import ColorRGBA
from visualization_msgs.msg import Marker

from spine.mapping.graph_util import GraphHandler


class NodeMarker:
    def __init__(self, node_marker: Marker, text_marker: Marker):
        """Hold marker for node and corresponding text label.
        TODO text should be optional

        Parameters
        ----------
        node_marker : Marker
        text_marker : Marker
        """
        self.node_marker = node_marker
        self.text_marker = text_marker


class GraphViz:
    RED_COLOR = ColorRGBA(r=0.75, a=1)
    GREEN_COLOR = ColorRGBA(g=0.75, a=1)
    BLUE_COLOR = ColorRGBA(b=0.75, a=1)
    WHITE_COLOR = ColorRGBA(r=0.75, g=0.75, b=0.75, a=1)

    def __init__(self, graph: GraphHandler, scale: float, target_frame: str) -> None:
        """Class for visualizing graph.

        Parameters
        ----------
        graph : GraphHandler
            Graph to visualize.
        scale : float
            Marker scale.
        target_frame : str
            Frame to plot in.
        """
        self.scale = scale
        self.target_frame = target_frame

        self.graph = graph
        self.node_markers = {}
        self.edge_markers = {}

        self.region_nodes = set()
        self.object_nodes = set()

        self.build_markers()

        self.prev_num_connections = 0
        self.changed = True

    def update_graph(self, graph: GraphHandler) -> None:
        self.graph = graph
        self.build_markers()
        self.changed = True

    def publish(self, pub: Publisher) -> None:
        """Publish node and text markers.

        Parameters
        ----------
        pub : Publisher
            Send messages on this publisher.
        """
        if self.changed or self.prev_num_connections != pub.get_num_connections():
            self.prev_num_connections = pub.get_num_connections()

            for node_marker in self.node_markers.values():
                pub.publish(node_marker.node_marker)
                pub.publish(node_marker.text_marker)

                rospy.sleep(0.001)

            for edge_marker in self.edge_markers.values():
                pub.publish(edge_marker)
                rospy.sleep(0.001)

            self.changed = False
        self.prev_num_connections = pub.get_num_connections()

    def get_marker_msg(self, x: int, y: int, id: int, z=1) -> Marker:
        marker_msg = Marker()
        marker_msg.id = id
        marker_msg.header.frame_id = self.target_frame

        marker_msg.pose.position.x = x
        marker_msg.pose.position.y = y
        marker_msg.pose.position.z = z
        marker_msg.pose.orientation.x = 0
        marker_msg.pose.orientation.y = 0
        marker_msg.pose.orientation.z = 0
        marker_msg.pose.orientation.w = 1

        marker_msg.scale.x = self.scale
        marker_msg.scale.y = self.scale
        marker_msg.scale.z = self.scale

        marker_msg.color = self.WHITE_COLOR

        return marker_msg

    def build_markers(self) -> None:
        """Constructs marker messages."""
        self.node_markers = {}
        self.edge_markers = {}

        for id, node in enumerate(self.graph.graph.nodes):
            attr = self.graph.graph.nodes[node]

            loc_x = attr["coords"][0]
            loc_y = attr["coords"][1]
            type = attr["type"]  # either object or region

            marker_msg = self.get_marker_msg(loc_x, loc_y, 2 * id)
            marker_msg.type = marker_msg.SPHERE

            if type == "object":
                marker_msg.color = self.BLUE_COLOR
                self.object_nodes.add(node)
            elif type == "region":
                marker_msg.color = self.RED_COLOR
                self.region_nodes.add(node)

            marker_msg.action = marker_msg.ADD

            marker_msg_text = self.get_marker_msg(
                loc_x, loc_y, 2 * id + 1, z=self.scale * 3
            )
            marker_msg_text.color = ColorRGBA(r=1, g=1, b=1, a=1)
            marker_msg_text.type = marker_msg.TEXT_VIEW_FACING

            text_msg = f"{node} ({loc_x:0.1f}, {loc_y:0.1f})"
            for k, v in attr.items():
                if k == "type" or k == "coords":
                    continue
                text_msg += f"\n{k}: {v}"

            marker_msg_text.text = text_msg

            self.node_markers[node] = NodeMarker(marker_msg, marker_msg_text)

        for id, (n1, n2) in enumerate(self.graph.graph.edges):
            start = self.graph.lookup_node(n1)[0]["coords"]
            end = self.graph.lookup_node(n2)[0]["coords"]

            base_id = 2 * len(self.graph.graph.nodes) + 2 * id
            marker_msg_start = self.get_marker_msg(0, 0, base_id)

            marker_msg_start.type = marker_msg.LINE_STRIP
            marker_msg_start.action = marker_msg.ADD
            marker_msg_start.color = self.GREEN_COLOR
            marker_msg_start.scale.x = self.scale / 5
            marker_msg_start.points.append(Point(x=start[0], y=start[1], z=0))
            marker_msg_start.points.append(Point(x=end[0], y=end[1], z=0))

            self.edge_markers[(n1, n2)] = marker_msg_start
