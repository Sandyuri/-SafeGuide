#!/usr/bin/env python
import enum
from typing import List, Optional, Tuple, Union

import numpy as np
import rospy
import tf
from actionlib_msgs.msg import GoalID, GoalStatusArray
from geometry_msgs.msg import Pose, PoseStamped
from spine.mapping.graph_util import GraphHandler
from spine_ros.srv import (
    AddNode,
    AddNodeRequest,
    AddNodeResponse,
    Task,
    TaskRequest,
    TaskResponse,
)
from move_base_msgs.msg import MoveBaseActionGoal
from scipy.spatial.transform import Rotation
from std_msgs.msg import Header


def unit_vector(vector):
    return vector / np.linalg.norm(vector)


class NAV_STATUS(enum.Enum):
    NONE = 0
    GOAL_IN_PROGRESS = 1
    GOAL_CANCELED = 2
    GOAL_COMPLETE = 3
    FAILED_TO_FIND_PLAN = 4
    REJECTED = 5
    PREMPTING = 6
    RECALLING = 7
    RECALLED = 8
    LOST = 9


class GraphNavNode:
    ZERO_VEC_2D = np.zeros(
        2,
    )
    DEFAULT_GRAPH = "/home/zac/projects/dcist/catkin_ws/src/llm-planning/data/flooded_grounds_coords.json"

    def __init__(self) -> None:
        self.ns = rospy.get_param("~ns", default="/")
        self.robot_frame = rospy.get_param("~robot_frame", "/husky/base_link")
        self.world_frame = rospy.get_param("~world_frame", "/world")
        self.max_goal_dist_m = rospy.get_param("~max_goal_dist_m", 9.0)

        self.goal_reached_lin_tol = rospy.get_param("~goal_reached_lin_tol", 0.5)

        pub_topic = rospy.get_param("~pub", f"/{self.ns}/move_base/goal")
        cancel_topic = rospy.get_param("~cancel", f"/{self.ns}/move_base/cancel")
        graph = rospy.get_param("~graph")  # , self.DEFAULT_GRAPH)

        # for navigation
        object_goal_angle_tol = rospy.get_param("~object_goal_angle_deg", 30)
        self.object_goal_angle_tol = np.deg2rad(object_goal_angle_tol)

        # for timeouts
        self.timeout_s = rospy.get_param("~timeout_s", 20)
        self.timeout_dist_m = rospy.get_param("~timeout_dist_m", 0.25)

        self.current_goal_id = 0

        self.tf_listener = tf.TransformListener()

        self.graph = GraphHandler(graph)
        self.current_nav_status = NAV_STATUS.NONE

        self.pub = rospy.Publisher(pub_topic, MoveBaseActionGoal)
        self.cancel_goal_pub = rospy.Publisher(cancel_topic, GoalID)
        self.region_sub = rospy.Service("~region_goal", Task, self.region_goal_cbk)
        self.object_sub = rospy.Service("~object_goal", Task, self.object_goal_cbk)
        self.add_node_sub = rospy.Service("~add_node", AddNode, self.add_node_cbk)
        self.nav_status_sub = rospy.Subscriber(
            f"/{self.ns}/move_base/status", GoalStatusArray, self.nav_status_cbk
        )

    def add_node_cbk(self, req: AddNodeRequest) -> AddNodeResponse:
        # TODO should this be flipped
        attrs = {"coords": [req.x, req.y], "type": req.type}
        self.graph.update_with_node(node=req.node_id, attrs=attrs, edges=req.neighbors)
        rospy.logdebug(f"updating graph with coords")
        return AddNodeResponse(success=True)

    def nav_status_cbk(self, status: GoalStatusArray) -> None:
        if len(status.status_list):
            self.current_nav_status = NAV_STATUS(status.status_list[0].status)

    def lookup_robot_pose(self) -> Tuple[Tuple[List[int], List[int]], bool]:
        try:
            (pos, quat) = self.tf_listener.lookupTransform(
                self.world_frame, self.robot_frame, rospy.Time(0)
            )
            return (pos, quat), True
        except (
            tf.LookupException,
            tf.ConnectivityException,
            tf.ExtrapolationException,
        ):
            return (None, None), False

    def get_goal_angle_yaw(
        self, goal_point: np.ndarray, obj_point: Union[np.ndarray, None] = None
    ) -> float:
        goal_angle = np.arctan2(goal_point[1], goal_point[0])
        if obj_point is not None:
            goal_to_obj = obj_point - goal_point
            obj_angle = np.arctan2(goal_to_obj[1], goal_to_obj[0])
            goal_angle = obj_angle

        return goal_angle

    def dist_from_goal(self) -> bool:
        pass

    def wait_for_nav_success(self) -> bool:
        # TODO bypass for real experiments
        while False and self.current_nav_status != NAV_STATUS.GOAL_COMPLETE:
            rospy.loginfo(
                f"waiting for nav success. status: {self.current_nav_status}"
            )  # TODO debugging
            rospy.sleep(5)
        return True

    def _normalized_angle_diff(self, angle_1: float, angle_2: float) -> float:
        diff = angle_1 - angle_2
        diff = (diff + np.pi) % (2 * np.pi) - np.pi

        return np.abs(diff)

    def get_min_dist_to_buffer(self, pos: np.ndarray, buffer: List[np.array]) -> float:
        # nothing to compare
        if len(buffer) == 0:
            return np.inf

        return np.linalg.norm(pos - np.array(buffer).reshape(-1, 2), axis=-1).min()

    def wait_for_goal_reached(
        self, goal_point, goal_angle=None, tol=5, angle_tol=0.5
    ) -> bool:
        in_lin_tol = lambda pos: np.linalg.norm(pos - goal_point, ord=2) < tol
        in_angle_tol = (
            lambda angle: self._normalized_angle_diff(angle_1=angle, angle_2=goal_angle)
            < angle_tol
        )

        if goal_angle != None:
            stop_condition = lambda pos, yaw: in_lin_tol(pos) and in_angle_tol(yaw)
        else:
            stop_condition = lambda pos, yaw: in_lin_tol(pos)

        start_time = rospy.Time.now()
        history_buffer = []
        pos, _ = self.get_robot_position()
        history_buffer.append(pos)

        while True:
            pos, yaw = self.get_robot_position()
            # TODO debugging
            if True or goal_angle != None:
                dist = np.linalg.norm(pos - goal_point, ord=2)
                # rospy.loginfo(f"dist: {dist}, tol: {tol}")
                # rospy.loginfo(
                #     f"current pose ({pos}, {yaw}), desired: ({goal_point} ,{goal_angle})"
                #     f", tols: ({tol}, {angle_tol})"
                # )
            if stop_condition(pos, yaw):
                break

            # break if can't reach goal
            if (
                self.current_nav_status == NAV_STATUS.FAILED_TO_FIND_PLAN
                or self.current_nav_status == NAV_STATUS.REJECTED
            ):
                self.cancel_goal()
                return False

            # if robot is moving, reset timer and update history
            if self.get_min_dist_to_buffer(pos, history_buffer) > self.timeout_dist_m:
                start_time = rospy.Time.now()
                history_buffer.append(pos)

            # if robot has been stationary for a while, consider goal failed.
            if (rospy.Time.now() - start_time).to_sec() > self.timeout_s:
                rospy.loginfo(
                    f"Goal taking over timeout ({self.timeout_s}). Cancelling"
                )
                self.cancel_goal()
                return False

            rospy.sleep(0.5)

        # wait for goal to be cancelled
        self.cancel_goal()
        return True

    def command_subgoals(
        self, robot_position: np.ndarray, goal_point: np.ndarray
    ) -> bool:
        diff = goal_point - robot_position
        dist = np.linalg.norm(diff, ord=2)

        unit_vec = diff / dist
        unit_angle = self.get_goal_angle_yaw(goal_point=unit_vec)

        n_segments = int(dist // self.max_goal_dist_m)
        segments = [
            robot_position + unit_vec * self.max_goal_dist_m * (i + 1)
            for i in range(n_segments)
        ]

        for segment in segments:
            self.pub_msg(goal_point=segment, orientation_yaw=unit_angle)
            rospy.sleep(5)  # debounce

            # don't check angle TODO check this
            success = self.wait_for_goal_reached(
                goal_point=segment,
                goal_angle=None,
                tol=self.goal_reached_lin_tol,
                angle_tol=4,
            )
            if not success:
                return False
            # self.wait_for_nav_success()

        return True

    def get_robot_position(self) -> Tuple[np.ndarray, float]:
        robot_pose, transform_found = self.lookup_robot_pose()

        if not transform_found:
            raise ValueError("No transform found. Couldn't plan")

        robot_position = np.array(robot_pose[0])[:2]  # only care about xy
        yaw = Rotation.from_quat(robot_pose[1]).as_euler("xyz")[2]
        return robot_position, yaw

    def intermediate_nav(self, goal_point: np.ndarray) -> Union[bool, str]:
        """Move base cannot command goals far (as defined by a threshold) away
        from the robot. This breaks down long goals into intermediate subgoals
        which are realized, until `goal_point` is reachable by one command.

        Parameters
        ----------
        goal_point : np.ndarray

        Returns
        -------
        Union[bool, str]
        """
        robot_position, _ = self.get_robot_position()
        dist = np.linalg.norm(goal_point - robot_position, ord=2)

        # goal is to far away. navigate by subgoals
        if dist > self.max_goal_dist_m:
            success = self.command_subgoals(
                robot_position=robot_position, goal_point=goal_point
            )
            if not success:
                return False, "Failed to reach goal"

        return True, ""

    def object_goal_cbk(self, goal_task: TaskRequest) -> None:
        goal_node = goal_task.task
        (obj, obj_attr), (region, region_attr), found = self.graph.lookup_object(
            goal_node
        )

        if not found:
            return TaskResponse(
                success=False, message=f"Could not find node: {goal_node}"
            )

        goal_point = np.array(region_attr["coords"])
        goal_angle = self.get_goal_angle_yaw(
            goal_point=goal_point, obj_point=np.array(obj_attr["coords"])
        )

        # if goal is too far to directly navigate to
        success, msg = self.intermediate_nav(goal_point=goal_point)
        if not success:
            return TaskResponse(success=success, message=msg)

        self.pub_msg(goal_point=goal_point, orientation_yaw=goal_angle)
        success = self.wait_for_nav_success()
        success = self.wait_for_goal_reached(
            goal_point=goal_point,
            goal_angle=goal_angle,
            tol=self.goal_reached_lin_tol,
            angle_tol=self.object_goal_angle_tol,
        )

        return TaskResponse(success=success, message="reached goal")

    def region_goal_cbk(self, goal: TaskRequest) -> None:
        goal_node = goal.task
        attr, found = self.graph.lookup_node(goal_node)

        if not found:
            return TaskResponse(
                success=False, message=f"could not find goal: {goal_node}"
            )

        goal_point = np.array(attr["coords"])

        success, msg = self.intermediate_nav(goal_point=goal_point)
        if not success:
            return TaskResponse(success=success, message=msg)

        self.pub_msg(goal_point=goal_point)
        success = self.wait_for_nav_success()
        success = self.wait_for_goal_reached(
            goal_point=goal_point, tol=self.goal_reached_lin_tol
        )

        return TaskResponse(success=success, message="reached goal")

    def form_msg(self, goal: np.ndarray, orientation_yaw: float = 0) -> PoseStamped:
        assert goal.ndim == 1
        if len(goal) == 2:
            goal = np.pad(goal, (0, 1))

        self.current_goal_id += 1

        quat = Rotation.from_euler("xyz", (0, 0, orientation_yaw)).as_quat()

        header = Header()
        header.stamp = rospy.Time.now()
        header.frame_id = "world"

        pose = Pose()
        pose.orientation.x = quat[0]
        pose.orientation.y = quat[1]
        pose.orientation.z = quat[2]
        pose.orientation.w = quat[3]

        pose.position.x = goal[0]
        pose.position.y = goal[1]
        pose.position.z = goal[2]

        msg = PoseStamped(header=header, pose=pose)

        goal_msg = MoveBaseActionGoal()
        goal_msg.header = msg.header
        goal_msg.goal_id = GoalID(stamp=msg.header.stamp, id=str(self.current_goal_id))
        goal_msg.goal.target_pose = msg
        return goal_msg

    def pub_msg(
        self, goal_point: np.ndarray, orientation_yaw: Optional[float] = 0
    ) -> None:
        # self.update_controller()

        msg = self.form_msg(goal_point, orientation_yaw=orientation_yaw)
        self.pub.publish(msg)

    def cancel_goal(self) -> None:
        # while self.current_nav_status == NAV_STATUS.GOAL_IN_PROGRESS:
        for _ in range(5):  # there is a delay in reading status, so use time for now
            self.cancel_goal_pub.publish(
                GoalID(stamp=rospy.Time.now(), id=str(self.current_goal_id))
            )
            rospy.sleep(0.1)


if __name__ == "__main__":
    rospy.init_node("graph_nav_node")
    nav = GraphNavNode()
    rospy.spin()
