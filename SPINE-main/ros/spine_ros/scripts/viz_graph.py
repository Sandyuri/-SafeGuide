#!/usr/bin/env python
import rospy
from geometry_msgs.msg import Point
from spine.mapping.graph_util import GraphHandler
from std_msgs.msg import ColorRGBA
from visualization_msgs.msg import Marker


# TODO deprecated
class NodeMarker:
    def __init__(self, node_marker, text_marker):
        self.node_marker = node_marker
        self.text_marker = text_marker


class GraphVizNode:
    RED_COLOR = ColorRGBA(r=0.75, a=1)
    GREEN_COLOR = ColorRGBA(g=0.75, a=1)
    BLUE_COLOR = ColorRGBA(b=0.75, a=1)
    WHITE_COLOR = ColorRGBA(r=0.75, g=0.75, b=0.75, a=1)

    def __init__(self) -> None:
        graph = rospy.get_param("~graph")
        self.scale = rospy.get_param("~scale", 0.5)
        self.target_frame = rospy.get_param("~world_frame", "map")

        self.pub = rospy.Publisher("~graph_viz", Marker, queue_size=100)
        self.graph = GraphHandler(graph)
        self.node_markers = {}
        self.edge_markers = {}

        self.build_markers()

        self.prev_num_connections = 0
        while not rospy.is_shutdown():
            if self.prev_num_connections != self.pub.get_num_connections():
                self.prev_num_connections = self.pub.get_num_connections()
                for node_marker in self.node_markers.values():
                    self.pub.publish(node_marker.node_marker)
                    self.pub.publish(node_marker.text_marker)
                for edge_marker in self.edge_markers.values():
                    self.pub.publish(edge_marker)

    def get_marker_msg(self, x, y, id, z=1):
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

    def build_markers(self):
        for id, node in enumerate(self.graph.graph.nodes):
            attr = self.graph.graph.nodes[node]

            loc_x = attr["coords"][0]
            loc_y = attr["coords"][1]
            type = attr["type"]  # either object or region

            marker_msg = self.get_marker_msg(loc_x, loc_y, 2 * id)
            marker_msg.type = marker_msg.SPHERE

            if type == "object":
                marker_msg.color = self.BLUE_COLOR
            elif type == "region":
                marker_msg.color = self.RED_COLOR

            marker_msg.action = marker_msg.ADD

            marker_msg_text = self.get_marker_msg(
                loc_x, loc_y, 2 * id + 1, z=self.scale * 3
            )
            marker_msg_text.color = ColorRGBA(r=1, g=1, b=1, a=1)
            marker_msg_text.type = marker_msg.TEXT_VIEW_FACING
            marker_msg_text.text = f"{node} ({loc_x:0.1f}, {loc_y:0.1f})"

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


if __name__ == "__main__":
    rospy.init_node("graph_viz_node")
    viz = GraphVizNode()
    rospy.spin()
