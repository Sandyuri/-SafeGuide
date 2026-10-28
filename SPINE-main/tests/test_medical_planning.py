from spine.prompts.prompts import get_base_prompt_update_graph
from spine.spine import SPINE


class FakeMedicalGraph:
    current_location = "task-source-1"

    def __init__(self):
        self.nodes = {
            "task-package-1": "object",
            "task-recipient-1": "object",
            "task-source-1": "region",
            "task-destination-1": "region",
        }

    def contains_node(self, node):
        return node in self.nodes

    def get_node_type(self, node):
        return self.nodes[node]

    def path_exists_from_current_loc(self, node):
        return node in self.nodes


def planner_for_test():
    planner = SPINE.__new__(SPINE)
    planner.graph = FakeMedicalGraph()
    return planner


def test_medical_delivery_actions_parse_with_typed_arguments():
    plan, feedback = planner_for_test().extract_plan(
        [
            "verify_resource(task-package-1)",
            "pickup(task-package-1)",
            "goto(task-destination-1)",
            "verify_recipient(task-recipient-1)",
            "handoff(task-package-1, task-recipient-1)",
        ]
    )

    assert feedback.success
    assert plan == [
        ("verify_resource", "task-package-1"),
        ("pickup", "task-package-1"),
        ("goto", "task-destination-1"),
        ("verify_recipient", "task-recipient-1"),
        ("handoff", ("task-package-1", "task-recipient-1")),
    ]


def test_medical_delivery_actions_reject_wrong_arity_and_node_type():
    _, handoff_feedback = planner_for_test().extract_plan(
        ["handoff(task-package-1)"]
    )
    _, pickup_feedback = planner_for_test().extract_plan(
        ["pickup(task-source-1)"]
    )

    assert not handoff_feedback.success
    assert "exactly two object nodes" in handoff_feedback.message
    assert not pickup_feedback.success
    assert "requires an object node" in pickup_feedback.message


def test_medical_scene_uses_only_the_medical_few_shot_example():
    scene = (
        "{'medical_task': {'type': 'DELIVER_MEDICATION'}, "
        "'objects': ['task-package-1', 'task-recipient-1']}"
    )
    prompt = get_base_prompt_update_graph(
        "Deliver task-package-1 to task-recipient-1.", scene
    )
    contents = "\n".join(message["content"] for message in prompt)

    assert "verify_resource(task-package-1)" in contents
    assert "handoff(task-package-1, task-recipient-1)" in contents
    assert "shovel" not in contents
