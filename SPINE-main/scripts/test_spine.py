from spine.mapping.graph_util import GraphHandler
from spine.spine import SPINE
from spine.spine_util import get_add_connection_update_str
import numpy as np

import pprint
import argparse
import json


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=str)
    parser.add_argument("--graph", type=str)
    parser.add_argument("--init-node", type=str)

    args = parser.parse_args()

    graph_path = args.graph
    init_node = args.init_node

    graph_handle = GraphHandler(graph_path=graph_path, init_node=init_node)
    planner = SPINE(graph=graph_handle)

    def query_planner(llm_input: str) -> None:
        resp, success, logs = planner.request(llm_input)

        if success:
            print(f"success: {success}")

            print("--feedback--\n")
            for log in logs:
                print(log)
            print("\n--")

            pprint.PrettyPrinter().pprint(resp)

            plan = resp["plan"]
            reason = resp["reasoning"]

            print(f"plan:")
            for action, arg in plan:
                parsed_arg = arg
                print(f"\t{action}( {parsed_arg} )")

            print(f"reason: {reason}")
        else:
            print(f"success: {success}")

            print("--feedback--\n")
            for log in logs:
                print(log)
            print("\n--")

            print(type(resp["plan"]))
            print(resp)

    query_planner(f"task: {args.task}")

    while True:
        llm_input = input(f"provide input: ")

        query_planner(llm_input)
