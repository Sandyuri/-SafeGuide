#!/usr/bin/env python
import rospy
from std_msgs.msg import String
from spine.mapping.graph_util import GraphHandler
import json

from spine_ros.srv import Graph, GraphRequest, GraphResponse


class GraphService:
    def __init__(self) -> None:
        self.publish_graph_srv = rospy.Service("~graph_srv", Graph, self.graph_srv)

        self.spine_graph_srv = rospy.Publisher("/titan/overhead_graph", String)

    def graph_srv(self, graph_path: GraphRequest) -> GraphResponse:
        """Takes a path to JSON containing scene graph."""
        path = graph_path.graph
        current_location = graph_path.current_location

        origin_data = {}
        # with open(path) as f:
        #     data = json.load(f)

        #     if "origin" in data:
        #         origin_data["origin"] = data["origin"]

        try:
            rospy.loginfo(f"trying to load: {path}")
            graph_handler = GraphHandler(path, current_location)
            graph_json_str = graph_handler.to_json_str(extra_data=origin_data)

            rospy.loginfo(f"graph as str: {graph_json_str}")
            self.spine_graph_srv.publish(String(graph_json_str))
            return GraphResponse(success=True)
        except Exception as ex:
            rospy.loginfo(ex)
            return GraphResponse(success=False)


if __name__ == "__main__":
    rospy.init_node("graph_service")
    node = GraphService()
    rospy.spin()
