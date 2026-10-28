import json
import logging
import os
from collections import namedtuple
from datetime import datetime
from typing import Any, Dict, List, Tuple

import numpy as np
from openai import OpenAI

from spine.llm_logging import get_logger
from spine.mapping.graph_util import GraphHandler
from spine.prompts.prompts import INVALID_JSON, get_base_prompt_update_graph

ValidPlanFeedback = namedtuple("ValidPlanFeedback", ["success", "message"])
VALID_ACTIONS = set(
    [
        "explore_region",
        "map_region",
        "inspect",
        "clarify",
        "goto",
        "answer",
        "extend_map",
        "replan",
        "verify_resource",
        "pickup",
        "verify_recipient",
        "handoff",
        "request_human_review",
        "abort_task",
    ]
)
REGION_ACTIONS = set(["explore_region", "map_region", "goto"])
NAVIGATION_ACTIONS = set(["explore_region", "map_region", "goto", "inspect"])
INTERACTION_ACTIONS = set(["explore_region", "map_region", "goto", "inspect"])
OBJECT_ACTIONS = set(["inspect"])
SINGLE_OBJECT_ACTIONS = set(["verify_resource", "pickup", "verify_recipient"])
TWO_OBJECT_ACTIONS = set(["handoff"])
SPATIAL_ACTION = set(["extend_map"])
EXPLORE_ACTIONS = set(["explore_region"])


class SPINE:
    def __init__(self, graph: GraphHandler) -> None:
        self.graph = graph
        self.client = OpenAI()
        self.model = os.getenv(
            "SPINE_MODEL",
            os.getenv("ROBOGUARD_MODEL", "gpt-4o"),
        )
        self.n_attempts = 3
        self.base_request = ""

        self.msg_history = []

        now = datetime.now()

        # dt_string = now.strftime("%d_%m_%Y_%H_%M_%S")
        self.logger = get_logger(
            name="LLMPlanner",
            level=logging.INFO,
            stdout=False,
            # fpath=f"llm_logs_{dt_string}.txt",
        )
        self.logger.disabled = True

        self.most_recent_query = []

    def clean_llm_output(self, s: str) -> str:
        return s.strip("```").strip("json")

    def clean_plan_argument(self, s: str) -> str:
        """remove extra quotes, etc."""
        return s.strip().strip("'").strip('"')

    def preprocess_cmd_str(self, cmd_list: str) -> List[str]:
        if isinstance(cmd_list, str):
            cmd_list = cmd_list.strip("[").strip("]")
            cmd_list = cmd_list.split(",")

        if len(cmd_list) == 1:
            smoothed_cmds = cmd_list
        else:
            # some arguments may have commas, thus be over split
            # go through list and add back full function-argument pairs
            smoothed_cmds = []
            current_cmd = ""
            for cmd in cmd_list:
                if cmd.strip().startswith(tuple(VALID_ACTIONS)):
                    if len(current_cmd):
                        smoothed_cmds.append(current_cmd)
                        current_cmd = ""

                    current_cmd = cmd
                else:
                    current_cmd += f",{cmd}"

            if len(current_cmd):
                smoothed_cmds.append(current_cmd.strip())

        return smoothed_cmds

    def try_parse(self, response: str) -> Tuple[Dict[str, Any], ValidPlanFeedback]:
        try:
            if not isinstance(response, str):
                self.logger.info(f"response is of type: {type(response)}")
                response = str(response)
                # TODO log this
            response = self.clean_llm_output(response)
            as_json = json.loads(response, strict=False)
            plan = self.preprocess_cmd_str(as_json["plan"])
            # plan = [p.strip() for p in as_json["plan"].split(",")]
            as_json["plan"] = plan
            return as_json, ValidPlanFeedback(True, "")
        except Exception as ex:
            return {}, ValidPlanFeedback(False, str(ex))

    def try_parse_region_arg(self, arg: str) -> Tuple[bool, np.ndarray]:
        try:
            parsed_arg = np.array([float(x) for x in arg.split(",")])
            return True, parsed_arg
        except:
            return False, arg

    def try_parse_exploration_arg(self, arg: str) -> Tuple[bool, Tuple[str, float]]:
        try:
            arg = arg.split(",")
            region = arg[0].strip()
            radius = float(arg[1].strip())
            return True, (region, radius)
        except:
            return False, (arg, arg)

    def try_parse_inspection_arg(self, arg: str) -> Tuple[bool, Tuple[str, str]]:
        try:
            arg = arg.split(",")
            object = arg[0].strip()
            query = "".join(arg[1:]).strip()
            return True, (object, query)
        except:
            return False, (arg, arg)

    def try_parse_single_object_arg(self, arg: str) -> Tuple[bool, str]:
        parts = [item.strip() for item in arg.split(",") if item.strip()]
        if len(parts) != 1:
            return False, arg
        return True, parts[0]

    def try_parse_two_object_args(
        self, arg: str
    ) -> Tuple[bool, Tuple[str, str]]:
        parts = [item.strip() for item in arg.split(",") if item.strip()]
        if len(parts) != 2:
            return False, (arg, arg)
        return True, (parts[0], parts[1])

    def normalize_messages(self, messages):
        normalized = []
        for message in messages:
            item = dict(message)
            content = item.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, str):
                        text_parts.append(block)
                    elif isinstance(block, dict) and block.get("text") is not None:
                        text_parts.append(str(block["text"]))
                content = "\n".join(text_parts)
            item["content"] = content
            normalized.append(item)
        return normalized

    def query_llm(self, msg: str) -> Tuple[str, bool]:
        self.most_recent_query = msg
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.normalize_messages(msg),
                temperature=0.05,  # was 1
                max_tokens=int(os.getenv("SPINE_MAX_TOKENS", "8192")),
                top_p=1,
                frequency_penalty=0,
                presence_penalty=0,
                # Some OpenAI-compatible providers do not support
                # response_format; JSON is enforced by parser feedback.
            )
            top_msg = response.choices[0].message
            return top_msg, True
        except Exception as ex:
            print(f"got execption: {ex}")
            return "Error: network dropout", False

    def _try_parse_command(self, cmd: str) -> Tuple[str, bool]:
        try:
            # TODO bit of a hack but freeform answers may
            # break parsing. should fix later
            if cmd.startswith("answer"):
                function = "answer"
                arg = cmd[7:]
                arg = arg[:-1]
                arg = self.clean_plan_argument(arg)
                return (function, arg), True
            else:
                function, arg = cmd.split("(")
                function = function.strip()
                arg = arg.split(")")[0]
                arg = self.clean_plan_argument(arg)
                return (function, arg), True
        except ValueError as ex:
            return (), False

    def first_element_in_arg(self, arg: str) -> str:
        if "," in arg:
            return arg.split(",")[0].strip()
        else:
            return arg

    def extract_plan(self, plan_as_str: str) -> Tuple[List[str], ValidPlanFeedback]:
        parsed_plan = []

        for step in plan_as_str:
            # command = re.findall("[a-zA-Z0-9_]+", step)
            command, success = self._try_parse_command(step)
            # command must be 'function(arg)
            if not success:  # len(command) != 2:
                return [], ValidPlanFeedback(
                    False, f"Could not parse step: {step} in plan."
                )
            function, arg = command

            # navigation function require first argument to be a region. pull that out
            # here to simplify checking.
            first_arg = self.first_element_in_arg(arg)

            # is valid function
            if function not in VALID_ACTIONS:
                feedback = (
                    f"Feedback: {function} is not a valid command. You must use one of the "
                    f"following commands {list(VALID_ACTIONS)}. Update your plan accordingly."
                )
                return [], ValidPlanFeedback(False, feedback)

            # check that argument is in the graph, if required
            elif function in INTERACTION_ACTIONS and not self.graph.contains_node(
                first_arg
            ):
                self.logger.info(
                    f"couldn't find node {first_arg} in graph: {self.graph.graph.nodes}"
                )
                feedback = (
                    f"Feedback: scene does not contain {first_arg}. "
                    f"All plans must reference nodes in the current scene. "
                    f"If your plan depends on potentially discovered regions or objects, consider using `replan()` as a placeholder. "
                    f"Update your plan accordingly."
                )
                return [], ValidPlanFeedback(False, feedback)

            # if navigation command, check that it's reachable
            elif (
                function in NAVIGATION_ACTIONS
                and not self.graph.path_exists_from_current_loc(first_arg)
            ):
                feedback = (
                    f"Feedback: No path from current location, {self.graph.current_location}, "
                    f"to goal {first_arg}. "
                )
                (
                    closest_reachable_node,
                    target_node,
                ) = self.graph.get_closest_reachable_node(goal_node=first_arg)
                feedback += (
                    f"The closest pair of nodes in the connected components of {self.graph.current_location} and {first_arg}"
                    f" are {closest_reachable_node} and {target_node}. "
                )

                feedback += (
                    f"If you find a connection between that pair via `extend_map()`, you can reach {first_arg}. If there is no connection, you will need to find another path. "
                    "Update your plan accordingly. Note that your relevant_map and long-term goals may stay the same."
                )

                return [], ValidPlanFeedback(False, feedback)

            # if navigation command, check that argument is region
            elif (
                function in REGION_ACTIONS
                and not self.graph.get_node_type(first_arg) == "region"
            ):
                node_type = self.graph.get_node_type(first_arg)
                assert node_type == "object", f"Must implement logic for {node_type}"
                feedback = (
                    "Feedback: Only region nodes can be given as arguments for navigation actions: "
                    f"{list(REGION_ACTIONS)}. Got {first_arg} of type {self.graph.get_node_type(first_arg)} for command {function}."
                    f" Consider using one of the following functions: {list(OBJECT_ACTIONS)}. The preceding part of your plan is valid. "
                    f"Update accordingly."
                )
                return [], ValidPlanFeedback(False, feedback)

            elif function in SPATIAL_ACTION and not self.try_parse_region_arg(arg)[0]:
                feedback = (
                    f"Feedback: the extend_map function takes in a coordinate in (x, y) numeric. "
                    f"Got exception when trying to parse {arg}. Update your plan accordingly."
                )
                return [], ValidPlanFeedback(False, feedback)
            elif (
                function in EXPLORE_ACTIONS
                and not self.try_parse_exploration_arg(arg)[0]
            ):
                feedback = (
                    f"Feedback: the explore_region function takes in a region name (str) and radius (float). "
                    f"Got exception when trying to parse {arg}. Update your plan accordingly."
                )
            elif (
                function in OBJECT_ACTIONS
                and not self.graph.get_node_type(first_arg) == "object"
            ):
                feedback = f"Feedback: inspect requires an object, but {first_arg} is a region. Try calling map_region({first_arg}) or explore_region({first_arg}, 3) to get information about the area, depending on the task"

                return [], ValidPlanFeedback(False, feedback)
            elif function in SINGLE_OBJECT_ACTIONS:
                success, parsed_arg = self.try_parse_single_object_arg(arg)
                if not success:
                    return [], ValidPlanFeedback(
                        False,
                        f"Feedback: {function} takes exactly one object node. "
                        f"Got {arg}. Update your plan accordingly.",
                    )
                if not self.graph.contains_node(parsed_arg):
                    return [], ValidPlanFeedback(
                        False,
                        f"Feedback: scene does not contain {parsed_arg}. "
                        "Use only objects present in the current graph.",
                    )
                if self.graph.get_node_type(parsed_arg) != "object":
                    return [], ValidPlanFeedback(
                        False,
                        f"Feedback: {function} requires an object node, "
                        f"but {parsed_arg} is a region.",
                    )
                arg = parsed_arg
                parsed_plan.append((function, arg))
                continue
            elif function in TWO_OBJECT_ACTIONS:
                success, parsed_args = self.try_parse_two_object_args(arg)
                if not success:
                    return [], ValidPlanFeedback(
                        False,
                        f"Feedback: {function} takes exactly two object nodes. "
                        f"Got {arg}. Update your plan accordingly.",
                    )
                for parsed_arg in parsed_args:
                    if not self.graph.contains_node(parsed_arg):
                        return [], ValidPlanFeedback(
                            False,
                            f"Feedback: scene does not contain {parsed_arg}. "
                            "Use only objects present in the current graph.",
                        )
                    if self.graph.get_node_type(parsed_arg) != "object":
                        return [], ValidPlanFeedback(
                            False,
                            f"Feedback: {function} requires object nodes, "
                            f"but {parsed_arg} is a region.",
                        )
                arg = parsed_args
                parsed_plan.append((function, arg))
                continue
            else:
                if function in SPATIAL_ACTION:
                    success, arg = self.try_parse_region_arg(arg)

                elif function in EXPLORE_ACTIONS:
                    success, arg = self.try_parse_exploration_arg(arg)

                elif function in OBJECT_ACTIONS:
                    success, arg = self.try_parse_inspection_arg(arg)

                parsed_plan.append((function, arg))

        return parsed_plan, ValidPlanFeedback(True, "Is valid plan")

    def _generate_plan(
        self, msg: List[Dict[str, str]]
    ) -> Tuple[Dict[str, Any], bool, List[str]]:
        response = {}
        success = False
        logs = []

        for _ in range(self.n_attempts):
            top_msg, could_query_llm = self.query_llm(msg)

            if not could_query_llm:
                return {"msg": top_msg}, False, logs

            response, is_valid_json = self.try_parse(top_msg.content)

            if not is_valid_json.success:
                self.logger.info(
                    f"Not valid json. Got \n\t==\n\t{top_msg.content}\n"
                    f"=\n\twhich could not be parsed.\n\terror:{is_valid_json.message}.\n\t=="
                )
                msg.append({"role": "assistant", "content": top_msg.content})
                msg.append(
                    {
                        "role": "user",
                        "content": INVALID_JSON.format(is_valid_json.message),
                    }
                )
                logs.append(is_valid_json.message)
                logs.append(top_msg.content)
                continue

            plan, is_valid_plan = self.extract_plan(response["plan"])

            if not is_valid_plan.success:
                self.logger.info(
                    f"got: {response}\n\tnot valid plan: {is_valid_plan.message}"
                )
                msg.append({"role": "assistant", "content": top_msg.content})
                msg.append({"role": "user", "content": is_valid_plan.message})

                logs.append(top_msg.content)
                logs.append(is_valid_plan.message)

                print(logs[-1], logs[-2])

                continue

            if is_valid_json.success and is_valid_plan.success:
                success = True
                response["plan"] = plan
                break

        self.msg_history.append({"role": "assistant", "content": top_msg.content})

        return response, success, logs

    def request(self, request: str) -> Tuple[Dict[str, Any], bool, List[str]]:
        self.logger.info(f"Got Request: {request}")

        # on first request
        # TODO assumes first request is instruction. should update
        if self.base_request == "":
            self.base_request = request
        # adding to existing prompt
        else:
            current_request = {"role": "user", "content": request}
            self.msg_history.append(current_request)

        msg = (
            get_base_prompt_update_graph(
                request=self.base_request, scene_graph=self.graph.as_json_str
            )
            + self.msg_history
        )

        return self._generate_plan(msg)

    def resume_request(self) -> Tuple[Dict[str, Any], bool, List[str]]:
        assert len(self.most_recent_query), f"must have history to regenerate plan"
        return self._generate_plan(self.most_recent_query)

    def clear_history(self) -> None:
        self.msg_history = []

    def update(self, content: str) -> None:
        msg = {"role": "user", "content": content}

        self.msg_history.append(msg)


if __name__ == "__main__":
    import argparse

    # default not used
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=str, help="Instructions", required=True)
    parser.add_argument("--graph", type=str, help="input graph", required=True)
    args = parser.parse_args()

    msg = args.task

    graph_handler = GraphHandler(graph_path=args.graph)
    planner = SPINE(graph=graph_handler)
    response, success, logs = planner.request(msg)

    print(f"success: {success}")

    for log in logs:
        print(log)

    for k, v in response.items():
        print(f"{k}: {v}")
