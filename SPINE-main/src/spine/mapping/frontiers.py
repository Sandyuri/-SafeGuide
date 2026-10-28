import logging
from collections import namedtuple
from typing import List, Optional, Tuple

import cv2
import numpy as np
from scipy.spatial import ConvexHull
from scipy.spatial.transform import Rotation

from spine.llm_logging import get_logger
from spine.mapping.graph_util import GraphHandler

CostMapWithInfo = namedtuple("CostMap", ["map", "resolution_m_p_cell", "pos", "yaw"])
Node = namedtuple("Node", ["id", "location", "neighbors"])


class FrontierExtractor:
    def __init__(
        self,
        init_graph: GraphHandler,
        min_new_frontier_thresh: Optional[float] = 1,
        costmap_filter_threshold: Optional[int] = 70,
        costmap_filter_n_cells: Optional[int] = 3,
    ) -> None:
        self.costmap_with_info = None
        self.graph_handler = init_graph
        self.compute_region_nodes()
        self.min_new_frontier_thresh = min_new_frontier_thresh

        self.erode = False

        self.n_frontiers = 0
        self.n_samples = 5

        # create sample grid
        self.costmap_resolution = 0.2
        self.default_costmap_shape = (10, 10)
        self.costmap_step_size = self.default_costmap_shape[0] * 0.2 / 4
        self.sample_grid = self.create_sampling_grid(
            self.default_costmap_shape, self.costmap_step_size
        )

        self.pct_sample = 15

        self.costmap_filter_threshold = costmap_filter_threshold
        self.costmap_filter_n_cells = costmap_filter_n_cells

        # TODO need to parameterize
        self.costmap_dilate_n_cells = costmap_filter_n_cells

        self.logger = get_logger(
            name="frontiers", level=logging.INFO, stdout=True, fpath=""
        )

    def create_sampling_grid(
        self, costmap_shape: Tuple[int, int], step_size: Optional[float] = 0.5
    ) -> np.array:
        """Create a sample space frontier checking."""

        x_bound = (costmap_shape[0] - step_size) - costmap_shape[0] // 2
        y_bound = (costmap_shape[1] - step_size) - costmap_shape[1] // 2

        x = np.linspace(-x_bound, x_bound, np.uint8(costmap_shape[0] / step_size - 1))
        y = np.linspace(-y_bound, y_bound, np.uint8(costmap_shape[1] / step_size - 1))
        grid_x, grid_y = np.meshgrid(x, y)
        sample_grid = np.stack([grid_x.reshape(-1), grid_y.reshape(-1)], axis=-1)

        return sample_grid

    # TODO move into graph
    def compute_region_nodes(self):
        """Update set of region nodes. Assumes graph will be updated
        during operation.

        Updates
        - region_nodes: array of region names (str)
        - region_node_locs: array of region locations
        """
        region_nodes_locs = []
        region_nodes = []

        for node in self.graph_handler.graph.nodes:
            if self.graph_handler.graph.nodes[node]["type"] == "region":
                node_loc = self.graph_handler.graph.nodes[node]["coords"]
                region_nodes_locs.append(node_loc)
                region_nodes.append(node)

        self.region_nodes = np.array(region_nodes)
        self.region_node_locs = np.array(region_nodes_locs)

    def update_graph(self, graph: GraphHandler) -> None:
        """Update the current graph

        Parameters
        ----------
        graph : GraphHandler
            New graph.
        """
        self.graph_handler = graph

    def line_is_free(
        self,
        start: np.ndarray,
        end: np.ndarray,
        costmap_info: CostMapWithInfo,
        samples_p_cell: int,
    ) -> bool:
        """Check if line between start and end points is free
        is costmap.

        Parameters
        ----------
        start : np.ndarray
            (x, y) start location
        end : np.ndarray
            (x, y) end location
        costmap_info : CostMapWithInfo
            Object holding costmap and matadata
        samples_p_cell : int
            Number of samples to take per cell (can be less than 1)

        Returns
        -------
        bool
            Line between start and end points is free
        """
        # TODO make parallel
        samples = np.linspace(
            start.astype(np.int32),
            end.astype(np.int32),
            num=(np.linalg.norm(end - start) * samples_p_cell).astype(np.int32),
        )
        return np.all(
            [self.pt_is_free(sample, costmap_info=costmap_info) for sample in samples]
        )

    def world_to_costmap(
        self, pt: np.ndarray, pos: np.ndarray, yaw: float, resolution: float
    ) -> np.ndarray:
        """Transform point from world to costmap coordinates

        Parameters
        ----------
        pt : np.ndarray
            Input point (x, y) coordinates.
        pos : np.ndarray
            Origin of costmap in world coordinates.
        yaw : float
            Rotation of costmap in world coordinates.
        resolution : float
            Costmap resolution (meters per cell)

        Returns
        -------
        np.ndarray
            point in costmap coordinates as int array
        """
        # in costmap coord
        rot = Rotation.from_euler("xyz", [0, 0, yaw]).as_matrix()[1:, 1:]
        if pt.ndim == 1:
            pt = (rot.T @ (pt - pos) / resolution).astype(np.int32)
        else:
            pt = ((rot.T @ (pt - pos)[..., np.newaxis])[..., 0] / resolution).astype(
                np.int32
            )
        return pt.squeeze()[::-1]  # flip (x,y) to put in image coords for costmap

    def is_pt_in_costmap(self, pt: np.ndarray, costmap_info: CostMapWithInfo) -> bool:
        """Does the point lie in the costmap?

        Parameters
        ----------
        pt : np.ndarray
        costmap_info : CostMapWithInfo

        Returns
        -------
        bool
            True if point is in costmap.
        """
        pt_in_costmap = self.world_to_costmap(
            pt=pt,
            pos=costmap_info.pos,
            yaw=costmap_info.yaw,
            resolution=costmap_info.resolution_m_p_cell,
        ).squeeze()

        return not (
            (pt_in_costmap[0] < 0)
            or (pt_in_costmap[0] >= costmap_info.map.shape[0])
            or (pt_in_costmap[1] < 0)
            or (pt_in_costmap[1] >= costmap_info.map.shape[1])
        )

    def pt_in_costmap(self, pt: np.ndarray, costmap_info: CostMapWithInfo) -> bool:
        """True if point lies in costmap bounds"""
        pt_in_costmap = self.world_to_costmap(
            pt=pt,
            pos=costmap_info.pos,
            yaw=costmap_info.yaw,
            resolution=costmap_info.resolution_m_p_cell,
        ).squeeze()

        if (
            (pt_in_costmap[0] < 0)
            or (pt_in_costmap[0] >= costmap_info.map.shape[0])
            or (pt_in_costmap[1] < 0)
            or (pt_in_costmap[1] >= costmap_info.map.shape[1])
        ):
            return False

        return True

    def pt_is_free(self, pt: np.ndarray, costmap_info: CostMapWithInfo) -> bool:
        """True is the point is in the costmap and location is not
        occupied.

        Parameters
        ----------
        pt : np.ndarray
        costmap_info : CostMapWithInfo

        Returns
        -------
        bool
            True if point is in costmap and not occupied
        """
        pt_in_costmap = self.world_to_costmap(
            pt=pt,
            pos=costmap_info.pos,
            yaw=costmap_info.yaw,
            resolution=costmap_info.resolution_m_p_cell,
        ).squeeze()

        if (
            (pt_in_costmap[0] < 0)
            or (pt_in_costmap[0] >= costmap_info.map.shape[0])
            or (pt_in_costmap[1] < 0)
            or (pt_in_costmap[1] >= costmap_info.map.shape[1])
        ):
            return False

        self.logger.debug(
            f"\tpt: {pt_in_costmap}, value: {~costmap_info.map[pt_in_costmap[0], pt_in_costmap[1]].astype(bool)}"
        )

        return ~costmap_info.map[pt_in_costmap[0], pt_in_costmap[1]].astype(bool)

    def update_costmap(
        self,
        costmap: np.ndarray,
        resolution_m_p_cell: float,
        pos: np.ndarray,
        yaw: float,
    ) -> CostMapWithInfo:
        if costmap.shape != self.default_costmap_shape:
            self.default_costmap_shape = costmap.shape
            self.costmap_step_size = self.default_costmap_shape[0] * 0.2 / 4
            self.sample_grid = self.create_sampling_grid(
                self.default_costmap_shape, step_size=self.costmap_step_size
            )
        self.costmap_with_info = CostMapWithInfo(
            map=costmap, resolution_m_p_cell=resolution_m_p_cell, pos=pos, yaw=yaw
        )

        if self.costmap_filter_n_cells > 0 and self.costmap_filter_threshold > 0:
            self.filtered_costmap_with_info = self.filter_costmap(
                self.costmap_with_info,
                cost_threshold=self.costmap_filter_threshold,
                n_cells=self.costmap_filter_n_cells,
            )
        else:
            self.filtered_costmap_with_info = self.costmap_with_info

        return self.filtered_costmap_with_info

    def get_resolution_m_p_cell(self) -> float:
        if self.filtered_costmap_with_info is not None:
            return self.filtered_costmap_with_info.resolution_m_p_cell
        else:
            return 0.2

    def dilate_costmap(self, costmap: np.ndarray, n_cells: Optional[int] = 3):
        dilated_map = cv2.dilate(costmap, np.ones((n_cells, n_cells)))
        return dilated_map

    def erode_costmap(self, costmap: np.ndarray, n_cells: Optional[int] = 3):
        eroded_map = cv2.erode(costmap, np.ones((n_cells, n_cells)))
        return eroded_map

    def filter_costmap(
        self,
        costmap_with_info: CostMapWithInfo,
        cost_threshold: Optional[float] = 0,
        n_cells: Optional[int] = 3,
    ) -> CostMapWithInfo:
        # first remove all low-cost entries. Then make binary
        costmap = costmap_with_info.map.copy()
        costmap[costmap < cost_threshold] = 0
        costmap[costmap > 0] = 1
        costmap = costmap.astype(np.uint8)

        if self.erode:
            costmap = self.erode_costmap(costmap=costmap, n_cells=n_cells)

        costmap = self.dilate_costmap(costmap=costmap, n_cells=n_cells)

        # TODO hack need to parameterize
        costmap = self.dilate_costmap(
            costmap=costmap, n_cells=self.costmap_dilate_n_cells
        )

        free_dist = 1
        costmap[-free_dist:free_dist, -free_dist:free_dist] = 0

        filtered_costmap_with_info = CostMapWithInfo(
            map=costmap,
            pos=self.costmap_with_info.pos,
            yaw=self.costmap_with_info.yaw,
            resolution_m_p_cell=self.costmap_with_info.resolution_m_p_cell,
        )

        return filtered_costmap_with_info

    def get_dist_from_regions(self, point: np.ndarray) -> float:
        min_dist = np.linalg.norm(point - self.region_node_locs, axis=1).min()

        return min_dist

    def evaluate_frontier(
        self,
        exploration_target: np.ndarray,
        current_location: np.ndarray,
        desired_scale: float,
    ) -> Tuple[bool, np.ndarray, bool]:
        """Check if frontier is free. Line between current location
        and target must be free.

        If the frontier is not free, this function will iteratively
        reduce the length of the frontier (as defined by the line between
        the frontier and current location), until a valid point is found.

        Parameters
        ----------
        exploration_target : np.ndarray
            Frontier
        current_location : np.ndarray
            Location of robot.

        Returns
        -------
        Tuple[bool, np.ndarray, float]
        - returned value is valid frontier
        - closest fit frontier
        - is at obstacle boundary
        """
        current_loc = self.region_node_locs[
            np.where(current_location == self.region_nodes)
        ]
        pointing_vector = exploration_target - current_loc

        # want to sample at costmap resolution to avoid redundant calls
        n_samples = (np.linalg.norm(pointing_vector) / self.costmap_resolution).astype(
            np.int32
        )

        scale_factor = 1
        success = False
        best_fit_point = None
        at_obstacle = False

        # line search for farthest free point
        for scale_factor in range(1, n_samples):
            exploration_target = (
                pointing_vector * (scale_factor / n_samples) + current_loc
            )
            current_scale = np.linalg.norm(exploration_target - current_loc)

            # assume robot is at (0, 0), perform line search for farthest free point
            if not self.pt_is_free(exploration_target, self.filtered_costmap_with_info):
                # once we hit a non-free point, line search terminates
                self.logger.debug(f"\tchecking point: {exploration_target} occuplied")

                # if the point is in the costmap, we stopped b/c an obstacle
                at_obstacle = self.pt_in_costmap(
                    exploration_target, self.filtered_costmap_with_info
                )

                break
            # we've hit desired magnitude
            elif current_scale > desired_scale:
                break

            # don't consider point if it is too close to existing region
            elif (
                self.get_dist_from_regions(exploration_target)
                < self.min_new_frontier_thresh
            ):
                self.logger.debug(
                    f"proposed frontier {exploration_target} too close to existing region"
                )
                continue
            # if point is passes distance and obstacle checks, consider it
            else:
                best_fit_point = exploration_target
                success = True

            self.logger.debug(f"\tchecking point: {exploration_target} free")

        if scale_factor <= 1:  # this means no points were free
            return (
                False,
                best_fit_point,
                at_obstacle,
            )
        else:
            return (
                success,
                best_fit_point,
                at_obstacle,
            )

    def get_feasible_frontier(
        self,
        exploration_target: np.ndarray,
        current_location: str,
        absolute_coordinates: bool,
    ) -> Tuple[bool, np.ndarray, bool]:
        """Get free frontier closest to
        exploration_target

        Parameters
        ----------
        exploration_target : np.ndarray
        current_location : str
        absolute_coordinates : bool

        Returns
        -------
        Tuple[bool, np.ndarray]
        - is valid frontier
        - valid closest point to target
        - point is at obstacle boundary
        """
        # if coordinates are given relative to agent, transform to global

        current_coords = self.region_node_locs[
            np.where(current_location == self.region_nodes)
        ].squeeze()
        if absolute_coordinates:
            self.logger.debug(f"transform {current_coords} to make global")
            sampled_points = self.sample_grid + current_coords
        else:
            sampled_points = self.sample_grid.copy()

        sampled_points = sampled_points.squeeze()
        self.logger.debug(f"total number of sampled points is {sampled_points.shape}")

        dist_to_target = np.linalg.norm(sampled_points - exploration_target, axis=-1)
        self.logger.debug(f"dists target {exploration_target}: {dist_to_target.shape}")

        # TODO may want to tune percentile
        top_point_vals = np.percentile(
            dist_to_target, self.pct_sample
        )  # will be about 50 points
        self.logger.debug(f"top points within {top_point_vals} target")

        # evaluate points closest to target
        points_to_eval = sampled_points[dist_to_target < top_point_vals]
        self.logger.debug(f"have {points_to_eval.shape} points to eval")

        # want points
        desired_scale = np.linalg.norm(exploration_target - current_coords)
        desired_scale += 2  # give buffer

        # want points
        desired_scale = np.linalg.norm(exploration_target - current_coords)
        desired_scale += 2  # give buffer

        best_target = np.array([0, 0])
        best_score = np.inf
        search_success = False
        best_target_at_obstacle = False
        for frontier in points_to_eval:
            self.logger.debug(f"eval point: {frontier}")
            point_success, scaled_target, frontier_at_obstacle = self.evaluate_frontier(
                exploration_target=frontier,
                current_location=current_location,
                desired_scale=desired_scale,
            )
            self.logger.debug(f"eval point: {frontier} {point_success}")

            if not point_success:
                continue

            score = np.linalg.norm(scaled_target - exploration_target)
            if score < best_score and point_success:
                best_score = score
                best_target = scaled_target.reshape(2)
                search_success = True
                best_target_at_obstacle = frontier_at_obstacle
                self.logger.debug(
                    f"setting target: {scaled_target} with score: {best_score}"
                )

        if search_success:
            self.logger.info(f"best fit frontier: {best_target}")
        else:
            self.logger.info(f"no best fit frontier found")

        return search_success, best_target, best_target_at_obstacle

    def get_regions_in_costmap_idx(self, costmap_info: CostMapWithInfo):
        # TODO make parallel
        inds = []
        for i, region in enumerate(self.region_node_locs):
            if self.is_pt_in_costmap(region.squeeze(), costmap_info=costmap_info):
                inds.append(i)

        return np.array(inds)

    def find_neighbors_via_linesearch(
        self, source_node_coord, potential_neighbors_inds, resolution_m_p_cell
    ) -> List[str]:
        neighbor_ids = []
        for potential_neighbor_ind in potential_neighbors_inds:
            if self.line_is_free(
                start=source_node_coord,
                end=self.region_node_locs[potential_neighbor_ind],
                costmap_info=self.filtered_costmap_with_info,
                samples_p_cell=resolution_m_p_cell * 4,
            ):
                neighbor_ids.append(self.region_nodes[potential_neighbor_ind])

                self.logger.debug(
                    f"node: {self.region_nodes[potential_neighbor_ind]} is neighbor"
                )

        return neighbor_ids

    def get_frontiers(
        self,
        proposed_frontier: np.ndarray,
        current_location: str,
    ) -> Tuple[List[Node], str]:
        """Get frontier to explore. Straight line path between robot and
        frontier must be free. This function:

        1. Get's exploration target (via LLM)
        2. Finds the free point closest to that exploration target.
           Function samples N points in the costmap closest to exploration
           target. Free point (via line search) closest to target is considered
           best.
        3. Computes edges to the new target (line search in costmap)

        Parameters
        ----------
        current_location : str
        task : str
            User given task
            TODO likely deprecated in favor of direct exploration action
        history : List[str]
            History of actions
            TODO likely deprecated in favor of direct exploration action
        graph_as_json : str
            Current graph
            TODO likely deprecated in favor of direct exploration action.

        Returns
        -------
        Tuple[List[Node], str]
        - Added nodes (location and edges)
        - exploration llm response (TODO likely deprecated in favor of direct exploration)
        """
        frontiers = []

        if self.filtered_costmap_with_info == None:
            return frontiers, ""

        self.logger.debug(f"about to get frontiers")
        self.compute_region_nodes()

        resolution_m_p_cell = self.filtered_costmap_with_info.resolution_m_p_cell

        success, best_fit_frontier, is_at_obstacle = self.get_feasible_frontier(
            exploration_target=proposed_frontier,
            current_location=current_location,
            absolute_coordinates=True,
        )

        if not success:
            return frontiers, ""

        region_in_costmap_inds = self.get_regions_in_costmap_idx(
            costmap_info=self.filtered_costmap_with_info
        )

        neighbors = [current_location]

        sorted_inds = np.linalg.norm(
            best_fit_frontier - self.region_node_locs
        ).argsort()

        self.logger.debug(f"checking frontiers: {best_fit_frontier}")

        neighbors.extend(
            self.find_neighbors_via_linesearch(
                source_node_coord=best_fit_frontier,
                potential_neighbors_inds=region_in_costmap_inds[sorted_inds],
                resolution_m_p_cell=resolution_m_p_cell,
            )
        )

        # TODO remove after testing

        # for region_in_costmap_idx in region_in_costmap_inds[sorted_inds]:
        #     if self.line_is_free(
        #         start=best_fit_frontier,
        #         end=self.region_node_locs[region_in_costmap_idx],
        #         costmap_info=self.filtered_costmap_with_info,
        #         samples_p_cell=resolution_m_p_cell * 4,
        #     ):
        #         neighbors.append(self.region_nodes[region_in_costmap_idx])

        #         self.logger.debug(
        #             f"node: {self.region_nodes[region_in_costmap_idx]} is neighbor"
        #         )

        frontiers.append(
            Node(
                id=f"discovered_region_{self.n_frontiers}",
                location=[best_fit_frontier[0], best_fit_frontier[1]],
                neighbors=neighbors,
            )
        )
        self.n_frontiers += 1
        self.logger.debug("--")

        assert len(frontiers) == 1, "more than 1 frontier not supported"

        return frontiers, is_at_obstacle

    def get_missing_neighbors(self, node_id: str, node_coord: float) -> List[str]:
        region_in_costmap_inds = self.get_regions_in_costmap_idx(
            costmap_info=self.filtered_costmap_with_info
        )

        all_neighbors = self.find_neighbors_via_linesearch(
            source_node_coord=node_coord,
            potential_neighbors_inds=region_in_costmap_inds,
            resolution_m_p_cell=self.get_resolution_m_p_cell(),
        )

        current_neighbors = self.graph_handler.get_neighbors(node_id)
        new_neighbors = set(all_neighbors) - set(current_neighbors)
        new_neighbors = set(new_neighbors) - set(node_id)  # no self neighbors
        return list(new_neighbors)

    def in_hull(self, points: np.ndarray, hull: ConvexHull) -> np.ndarray:
        """Check if points if are convex hull.

        Is deprecated. Will remove.

        Parameters
        ----------
        points : np.ndarray
        hull : ConvexHull

        Returns
        -------
        np.ndarray
            indices of points in hull.
        """
        A, b = hull.equations[:, :-1], hull.equations[:, -1:]

        tol = np.finfo(np.float32).eps
        in_hull = np.all(points @ A.T + b.T < np.finfo(np.float32).eps, axis=-1)
        return in_hull
