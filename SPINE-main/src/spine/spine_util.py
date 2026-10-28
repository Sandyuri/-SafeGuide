from typing import List, Sequence, Tuple, Dict


class UpdatePromptFormer:
    def __init__(self) -> None:
        self.new_nodes = []
        self.new_connections = []
        self.removed_connections = []
        self.location_updates = []
        self.attribute_updates = []
        self.freeform_updates = []

    def update(
        self,
        *,
        new_nodes: List[Dict[str, str]] = [],
        new_connections: List[Tuple[str, str]] = [],
        removed_connections: List[Tuple[str, str]] = [],
        location_updates: List[str] = [],
        attribute_updates: List[Dict[str, str]] = [],
        freeform_updates: List[str] = [],
    ):
        self.new_nodes.extend(new_nodes)
        self.new_connections.extend(new_connections)
        self.removed_connections.extend(removed_connections)
        self.location_updates.extend(location_updates)
        self.attribute_updates.extend(attribute_updates)
        self.freeform_updates.extend(freeform_updates)

    def form_updates(self):
        update_str = ""
        if len(self.new_nodes):
            new_node_str = "add_nodes("
            for new_node_info in self.new_nodes:
                new_node_str += f"{str(new_node_info)}, "
            new_node_str = new_node_str[:-2]  # remove trailing comma
            new_node_str += ")"

            update_str += new_node_str
            self.new_nodes.clear()

        if len(self.new_connections):
            new_connection_str = "add_connections("
            new_connection_str += ", ".join(
                f"[{nc[0]}, {nc[1]}]" for nc in self.new_connections
            )
            new_connection_str += ")"

            update_str += (
                f",\n{new_connection_str}" if len(update_str) else new_connection_str
            )
            self.new_connections.clear()

        if len(self.removed_connections):
            removed_connection_str = "remove_connections("
            removed_connection_str += ", ".join(
                f"[{nc[0]}, {nc[1]}]" for nc in self.removed_connections
            )
            removed_connection_str += ")"

            update_str += (
                f",\n{removed_connection_str}"
                if len(update_str)
                else removed_connection_str
            )
            self.removed_connections.clear()

        if len(self.location_updates):
            location_update = ", ".join(
                [f"update_robot_location({loc})" for loc in self.location_updates]
            )

            update_str += (
                f",\n{location_update}" if len(update_str) else location_update
            )
            self.location_updates.clear()

        if len(self.attribute_updates):
            attr_updates = "update_node_attributes("
            attr_updates += ", ".join(
                [str(update) for update in self.attribute_updates]
            )
            attr_updates += ")"
            update_str += f",\n{attr_updates}" if len(update_str) else attr_updates
            self.attribute_updates.clear()

        if len(self.freeform_updates):
            freeform_update_str = ", ".join(
                [
                    f"navigation_update({freeform_update})"
                    for freeform_update in self.freeform_updates
                ]
            )
            self.freeform_updates.clear()

            update_str += (
                f",\n{freeform_update_str}" if len(update_str) else freeform_update_str
            )

        update_str = update_str.replace("'", "").replace('"', "")
        if update_str == "":
            update_str = "no_updates()"

        return update_str


def get_add_connection_update_str(
    node_type: str, node_id: str, neighbor_id: str
) -> str:
    return f"add_connection({node_type}_connection, {node_id}, {neighbor_id})"


def get_add_node_update_str(
    node_id: str,
    node_type: str,
    coordinates: Sequence[float],
    neighbor_ids: List[str],
) -> List[str]:
    """get strings corresponding to adding a node to a graph.

    Parameters
    ----------
    node_id : str
    type : str
        `object` or `region`
    coordinates : Sequence[float]
    neighbor_ids : List[str]

    Returns
    -------
    str
        _description_
    """
    assert node_type in ("object", "region")
    updates = []
    attributes = f"{{coords: [{coordinates[0]:0.1f}, {coordinates[1]:0.1f}]}}"
    node_id

    updates.append(f"add_node({node_type}, {node_id}, attributes={attributes})")
    for neighbor_id in neighbor_ids:
        updates.append(
            get_add_connection_update_str(
                node_type=node_type, node_id=node_id, neighbor_id=neighbor_id
            )
        )
    return updates


if __name__ == "__main__":
    updater = UpdatePromptFormer()

    connection_updates = [["node_1", "node_2"], ["node_2", "node_3"]]
    node_updates = [{"name": "node_3", "type": "region", "coords": [10, 10]}]
    node_updates = []

    location_updates = ["loc_1", "loc_2"]

    node_attributes = [{"name": "n1", "desc": "vlm_info"}, {"test": "test"}]

    updater.update(
        new_connections=connection_updates,
        new_nodes=node_updates,
        location_updates=location_updates,
        attribute_updates=node_attributes,
    )

    update_str = updater.form_updates()

    print(update_str)
