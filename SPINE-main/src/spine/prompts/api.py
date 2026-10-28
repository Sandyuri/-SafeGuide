# lines 1-3 will be removed when parsing for prompt
from typing import List, Tuple, Dict

# you will receive updates to the scene graph via the following API


def remove_nodes(removed_nodes: List[str]) -> None:
    """Remove `nodes` and associated edges from graph."""


def add_nodes(new_nodes: Dict[str, str]) -> None:
    """Add nodes to graph. Each node is represented as a dictionary."""


def add_connections(new_connections: List[Tuple[str, str]]) -> None:
    """Add a list of connections. Each element is a tuple of the connecting nodes."""


def remove_connections(removed_connections: List[Tuple[str, str]]) -> None:
    """Removes a list of connections. Each element is a tuple of the endpoint nodes in the edge."""


def update_robot_location(region_node: str) -> None:
    """Update robot's location in the graph to `region_node`."""


def update_node_attributes(attribute: List[Dict[str, str]]) -> None:
    """Update node's attributes. Each entry of the input is a dictionary of new node values.
    Entries will include the referent node's name."""


def no_updates() -> None:
    """There have been no updates."""


# You can plan using the following API


def goto(region_node: str) -> None:
    """Navigate to `region_node`. This function uses a graph-search algorithm to find the most efficient path to that node."""


def verify_resource(resource_node: str) -> None:
    """Request trusted verification of an authorized task resource.

    This action requests verification; it does not assert that verification
    succeeded.
    """


def pickup(resource_node: str) -> None:
    """Pick up a verified task resource at the robot's current region."""


def verify_recipient(recipient_node: str) -> None:
    """Request trusted verification of the authorized task recipient.

    Do not use visual inspection to infer a person's identity.
    """


def handoff(resource_node: str, recipient_node: str) -> None:
    """Hand a held resource to a verified authorized recipient."""


def request_human_review(reason: str) -> None:
    """Pause the task and request human review without claiming completion."""


def abort_task(reason: str) -> None:
    """Terminate the current task without claiming completion."""


def map_region(region_node: str) -> List[str]:
    """Navigate to region in the graph and look for new objects.
    - region_node must be currently observed in graph and reachable from the robot's location.
    - This CANNOT be used to add connections in the graph.

    Will return updates to graph (if any).
    """


def extend_map(x_coordinate: int, y_coordinate: int) -> List[str]:
    """Try to add region node to graph at the coordinates (x_coordinate, y_coordinate).

    You should call this when your goal is far away (over 10 meters, for example).

    NOTE: if the proposed region is not physically feasible
    (because of an obstacle, for example), the closest feasible region will
    be added instead.

    Will return updates to graph (if any).
    """


def explore_region(region_node: str, exploration_radius_meters: float = 3) -> List[str]:
    """Explore within `exploration_radius_meters` around `region_node`
    If (x, y) are the coordinates of `region_node` and `r` is the exploration radius.
    You should only call this if you are close to your goal (within exploration radius).

    Will return updates to graph (if any).
    """


def replan() -> None:
    """You will update your plan with newly acquired information.
    This is a placeholder command, and cannot be directly executed.
    """


def inspect(object_node: str, vlm_query: str) -> List[str]:
    """Gather more information about `object_node` by
    querying a vision-language model with `vlm_query`. Be concise in
    your query. The robot will also navigate to the
    region connected to `object_node`.

    Will return updates to graph (if any).
    """


def answer(answer: str) -> None:
    """Provide an answer to the instruction"""


def clarify(question: str) -> None:
    """Ask for clarification. Only ask if the instruction is too vague to make a plan."""
