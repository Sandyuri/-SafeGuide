from typing import Optional, Tuple
import utm

import rospy
from scipy.spatial.transform import Rotation
import numpy as np

from std_msgs.msg import Header
from sensor_msgs.msg import Imu, NavSatFix
from geometry_msgs.msg import PoseStamped


class ImuReader:
    def __init__(
        self,
        imu_topic: Optional[str] = "/imu/imu",
        navsat_topic: Optional[str] = "/ublox/fix",
        map_origin: Optional[Tuple[float, float]] = (
            482939.85851084325,
            4421267.982947684,
        ),
    ) -> None:
        """Get initial rotation according to IMU, then map it to east aligned.

        Parameters
        ----------
        imu_topic : Optional[str]
            by default "/imu/imu"
        """
        self.utm_pos_pub = rospy.Publisher("/odometry_filter", PoseStamped)
        self.imu_sub = rospy.Subscriber(imu_topic, Imu, self.imu_cbk)
        self.navsat_sub = rospy.Subscriber(navsat_topic, NavSatFix, self.navsat_cbk)
        self.current_rotation = Rotation.from_euler("xyz", (0, 0, 0))
        self.north_to_east_aligned = Rotation.from_euler(
            "xyz", (0, 0, -90), degrees=True
        )
        self.imu_initialized = False
        self.gps_initialized = False
        self.lla = None
        self.map_origin = np.array(map_origin)

    def imu_cbk(self, imu_msg: Imu) -> None:
        if not self.imu_initialized:
            orientation = imu_msg.orientation
            orientation = np.array(
                [orientation.x, orientation.y, orientation.z, orientation.w]
            )
            from_imu = Rotation.from_quat(orientation)  # .as_euler("xyz")
            yaw = from_imu.as_euler("xyz", degrees=True)[-1]
            static_bias = 26

            # imu is north aligned. Make this east aligned
            self.current_rotation = Rotation.from_euler(
                "xyz", (0, 0, -90 + static_bias - 7.5), degrees=True
            )
            # self.north_to_east_aligned * from_imu()
            # print(from_imu.as_euler("xyz", degrees=True))
            # self.current_rotation = from_imu.inv()

            self.imu_initialized = True

    def navsat_cbk(self, navsat_msg: NavSatFix) -> None:
        utm_pos = np.array(
            utm.from_latlon(navsat_msg.latitude, navsat_msg.longitude)[:2]
        )
        if not self.gps_initialized:
            self.utm_origin = utm_pos
            self.gps_initialized = True

        utm_msg = PoseStamped()
        utm_msg.header.stamp = rospy.Time.now()
        rel_utm_pos = utm_pos - self.map_origin
        utm_msg.pose.position.x = float(rel_utm_pos[0])
        utm_msg.pose.position.y = float(rel_utm_pos[1])
        self.utm_pos_pub.publish(utm_msg)

    def get_current_rot(self) -> Rotation:
        return self.current_rotation

    def get_utm_origin(self) -> np.ndarray:
        return self.utm_origin

    def is_initialized(self) -> bool:
        return self.imu_initialized and self.gps_initialized
