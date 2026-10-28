EXAMPLE_GRAPH_1 = {
    "objects": [
        {"name": "house_1", "coords": [-1, -1]},
        {"name": "house_2", "coords": [-3, -1]},
        {"name": "grocery_store_1", "coords": [-5, -1]},
        {"name": "shed_1", "coords": [1, 3]},
        {"name": "shed_1", "coords": [1, 5]},
    ],
    "regions": [
        {"name": "example_road_1", "coords": [-1, 0]},
        {"name": "example_road_2", "coords": [-2, 0]},
        {"name": "field_11", "coords": [0, 1]},
        {"name": "field_13", "coords": [2, 3]},
    ],
    "object_connections": [
        ["house_1", "example_road_1"],
        ["house_2", "example_road_2"],
        ["shed_1", "field_11"],
        ["shed_2", "field_13"],
    ],
    "region_connections": [
        ["example_road_1", "example_road_2"],
        ["example_road_1", "field_11"],
        ["field_11", "field_13"],
    ],
    "robot_location": "example_road_1",
}


EXAMPLE_1 = [
    {
        "role": "user",
        "content": "task: I need a shovel. Is there one in the scene?"
        + f"Scene graph:{str(EXAMPLE_GRAPH_1)}",
    },
    {
        "role": "assistant",
        "content": '{\n\
        "primary_goal": "find a shovel for the user."\
        "relevant_graph": "field_11, field_13, unobserved_node(shovel)", \
        "reasoning": "The graph does not contain any shovels. However, I know that the graph may be incomplete, so I will explore before providing a definitive answer. I will first map, then if needed I will add regions. There are two sheds in the scene, and shovels are often found near sheds. Therefore, for each of the sheds, I will navigate to the nearby region and map.",\
        "plan": "[goto(field_11), map_region(field_11), goto(field_13), map_region(field_13)]"\n}',
    },
    {
        "role": "user",
        "content": "updates:[no_updates()]",
    },
    {
        "role": "assistant",
        "content": '{\n\
            "primary_goal": "find a shovel for the user."\
            "relevant_graph": "field_13, unobserved_node(shovel)",\
            "reasoning": "After mapping field_11, which is next to shed_1, I did not find a shovel. I also planned on going to example_node_14 to map near shed_2, so I will continue my plan.",\
            "plan": "[goto(field_13), map_region(field_13)]"\n}',
    },
    {
        "role": "user",
        "content": "updates:[add_nodes({ name: shovel_1, type: object}), add_connections([shovel_1, field_13])]",
    },
    {
        "role": "assistant",
        "content": '{\n\
        "primary_goal": "find a shovel for the user."\
        "relevant_graph": "shovel_1", \
        "reasoning": "After exploring field_13, which is next to shed_2, I found a nearby shovel, shovel_1, that is connected to field_13. This fulfills the users request.",\
        "plan": "[answer(There is a shovel, shovel_1, that is near shed_2 and connected to region_13.)]"\n}',
    },
    {
        "role": "user",
        "content": "task: How many grocery stores are currently in the scene?",
    },
    {
        "role": "assistant",
        "content": '{ \
        "primary_goal": "find a shovel for the user."\
        "relevant_graph": "grocery_store_1",\
        "reasoning": "There is one grocery store in the scene. Although the scene may be incomplete, I am only asked about currently observed grocery stores. Thus the answer is one",\
        "plan": "[answer(There is one grocery store in the scene, grocery_store_1.)]"}',
    },
]


EXAMPLE_GRAPH_2 = {
    "objects": [
        {"name": "office_building_1", "coords": [200, 51]},
        {"name": "office_building_2", "coords": [190, 50]},
        {"name": "robot_1", "coords": [210, 65]},
        {"name": "robot_2", "coords": [199, 65]},
        {"name": "robot_3", "coords": [190, 70]},
        {"name": "example_truck_1", "coords": [180, 70]},
    ],
    "regions": [
        {"name": "example_sidewalk_1", "coords": [180, 51]},
        {"name": "example_road_1", "coords": [190, 50]},
        {"name": "charging_station_1", "coords": [205, 60]},
        {"name": "charging_station_2", "coords": [190, 68]},
        {"name": "example_node_2", "coords": [150, 38]},
        {"name": "example_node_1", "coords": [130, 38]},
    ],
    "object_connections": [
        ["office_building_1", "example_sidewalk_1"],
        ["office_building_2", "example_sidewalk_1"],
        ["robot_1", "charging_station_1"],
        ["robot_2", "charging_station_1"],
        ["robot_3", "charging_station_2"],
        ["example_truck_1", "charging_station_2"],
    ],
    "region_connections": [
        ["example_sidewalk_1", "example_road_1"],
        ["example_sidewalk_1", "charging_station_1"],
        ["example_sidewalk_1", "charging_station_2"],
    ],
    "robot_location": "example_road_1",
}

EXAMPLE_2 = [
    {
        "role": "user",
        "content": "task: How many robots are currently observed, where are they located, and which is the southmost robot?"
        + f"scene graph: {str(EXAMPLE_GRAPH_2)}",
    },
    {
        "role": "assistant",
        "content": '{\n\
        "primary_goal": "Answer the users questions about robots in the scene. The user is likely referring to robot_1, robot_2, and robot_3"\
        "relevant_graph": "robot_1, robot_2, robot_3", \
        "reasoning": "There are currently three robots in the scene graph, robot_1, robot_2, and robot_2 robot_1 is at [210, 65], robot_2 is at [199, 65], and robot_3 is at [190, 70]. The y coordinate, which corresponds to south / north. robot_1 and robot_2 have the same y coordinate, which is lower than robot_2. Thus, robot_1 and robot_2 are equally southmost.",\
        "plan": "[answer(There are three robots in the scene. robot_1 is at [210, 65], robot_2 is at [199, 65], and robot_2 is at [190, 70]. Because robot_1 and robot_2 have the same y coordinate, which is lower than robot_2, they are equally southmost.)]"\n}',
    },
    {"role": "user", "content": "Feedback: That is correct."},
    {
        "role": "user",
        "content": "task: I cannot reach example_node_2 from my current location, as there is no path. I want to try to find a connection. What node should I go to in order to look for a connection.",
    },
    {
        "role": "assistant",
        "content": '{\n\
        "primary_goal": "Answer the users questions about robots in the scene. The user is likely referring to robot_1, robot_2, and robot_3"\
        "relevant_graph": "example_road_1, example_sidewalk_1, example_node_2", \
        "reasoning": "Based on my current location, example_road_1, I can reach example_sidewalk_1, charging_station_1, and charging_station_2. Out of this list, example_sidewalk_1 at [180, 51] is the closest to example_node_2 at [150, 38].",\
        "plan": "[answer(example_sidewalk_1 at [180, 51] is the closest to example_node_2 at [150, 38]. You should go there to look for a connection.)]"\n}',
    },
    {"role": "user", "content": "task: What is around the sidewalk?"},
    {
        "role": "assistant",
        "content": '{\n\
        "primary_goal": "Find what is around the sidewalk. From scene context, the user is referring to example_sidewalk_1."\
        "relevant_graph": "example_sidewalk_1",\n"reasoning": "The user wants to know what is around the sidewalk. There is only one sidewalk in the scene, example_sidewalk_1, so the user must be referring to that region. Because my intended goal is within a exploration radius of 3 meters, the most efficient way for me to complete that task is to go there and explore the region with a radius of 3 meters.",\
        "plan": "[goto(example_sidewalk_1), map_region(example_sidewalk_1), explore_region(example_sidewalk_1, 3)]"\n}',
    },
    {
        "role": "user",
        "content": "updates: [add_nodes({ name: discovered_region_1, type: region coords: [180, 54]}, {name: discovered_region_2, type: region, coords: [180, 48]}, {name: discovered_region_3, type: region, coords: [183, 51]}, {name: discovered_region_4, type: region, coords: [177, 61]}), add_connections([example_sidewalk_1, discovered_region_1], [example_sidewalk_1, discovered_region_2], [example_sidewalk_1, discovered_region_3], [example_sidewalk_1, discovered_region_4]), update_node_attributes({name: discovered_region_4, description=sidewalk}), update_robot_location(discovered_region_4)]",
    },
    {
        "role": "assistant",
        "content": '{\n\
        "primary_goal": "Find what is around the sidewalk. From scene context, the user is referring to example_sidewalk_1."\
        "goal": "example_sidewalk_1, discovered_region_1, discovered_region_2, discovered_region_3, discovered_region_4", \
        "reasoning": "I explore example_sidewalk_1, which means I tried to discover new regions. I then mapped those regions in search of new objects. I did find four new regions. but no new objects. By the description of discovered_region_4, it must be part of a sidewalk.",\
        "plan": "[answer(example_sidewalk_1 at [180, 51] has several traversable regions connected to it. discovered_region_4, is also part of a sidewalk. However, my exploration did not yield any new objects.)]"\n}',
    },
]

EXAMPLE_GRAPH_3 = {
    "objects": [],
    "regions": [
        {"name": "example_node_1", "coords": [10, 10]},
        {"name": "example_node_2", "coords": [12, 20]},
        {"name": "example_node_3", "coords": [15, 30]},
        {"name": "example_node_4", "coords": [12, 15]},
        {"name": "example_node_5", "coords": [10, 5]},
        {"name": "example_ground_6", "coords": [-2, -2]},
        {"name": "charging_station_1", "coords": [30, 40]},
    ],
    "object_connections": [],
    "region_connections": [
        ["example_node_2", "example_node_3"],
        ["example_node_1", "example_node_4"],
        ["example_node_4", "example_node_5"],
        ["example_node_5", "example_ground_6"],
    ],
    "robot_location": "example_node_1",
}

EXAMPLE_3 = [
    {
        "role": "user",
        "content": "task: I lost my robot when it was filling its battery. It is red. Can you find it and tell me its status?\n"
        + f"scene graph: {str(EXAMPLE_GRAPH_3)}",
    },
    {
        "role": "assistant",
        "content": '{\n\
        "primary_goal": "Find the users lost robot. The user provided contextual details that will help me find the robot."\
        "relevant_graph": "charging_station_1", \
        "reasoning": "The graph does not contain any robots, but the graph does contain a charging station, charging_station_1. The robot may be charging its battery there. I will determine if the nearby regions are connected to the booth.",\
        "plan": "[goto(example_node_2), goto(example_node_3), goto(example_node_4), goto(example_node_5)]"}',
    },
    {
        "role": "user",
        "content": "Feedback: this is a bad plan. example_node_4 and example_node_5 take you farther away from the goal, charging_station_1. Instead, try to get as close as possible to your goal, then expand the map via extend_map()",
    },
    {
        "role": "assistant",
        "content": '{\n\
        "primary_goal": "Find the users lost robot. The user provided contextual details that will help me find the robot."\
        "relevant_graph": "charging_station_1", \
        "reasoning": "My plan took me farther away from my goal, which is incorrect. Instead, I will navigate to the closest reachable region, example_node_3. I will then try to add a new region in the direction of charging_station_1 and replan depending on what I find.",\
        "plan": "[goto(example_node_3), extend_map(20, 35), replan()]"}',
    },
    {
        "role": "user",
        "content": "updates: [add_nodes({ name: discovered_region_1, type: region, coords: [15, 30]}), add_connections( [example_node_3, discovered_region_1], [discovered_region_1, charging_station_1]), update_robot_location(example_node_3)]",
    },
    {
        "role": "assistant",
        "content": '{\n\
        "primary_goal": "Find the users lost robot. The user provided contextual details that will help me find the robot."\
        "relevant_graph": "charging_station_1", \
        "reasoning": Upon exploring, I was able to add a new region that allows me to navigate to my goal, charging_station_1. I will now map charging_station_1 to see if I can find the users robot",\
        "plan": "[goto(charging_station_1), map_region(charging_station_1)]"}',
    },
    {
        "role": "user",
        "content": "updates: [add_nodes({ name: robot_1, type: object, coords: [31, 43]}), add_connections([robot_1, charging_station_1]), update_robot_location(charging_station_1)]",
    },
    {
        "role": "assistant",
        "content": '{\n\
        "primary_goal": "Find the users lost robot. The user provided contextual details that will help me find the robot."\
        "relevant_graph": "robot_1", \
        "reasoning": "Upon mapping the charging_station, I found a robot. I will now inspect it.",\
        "plan": "[inspect(robot_1, what is the color of this robot, and is it damaged?)]"\n}',
    },
    {
        "role": "user",
        "content": "updates: [update_node_attributes({ name: robot_1, description=this is a red robot that is not damaged})]",
    },
    {
        "role": "assistant",
        "content": '{\n\
        "primary_goal": "Find the users lost robot. The user provided contextual details that will help me find the robot."\
        "relevant_graph": "robot_1",\
        "reasoning": "I found a robot that is red. The robot is not damaged. This may be the users robot, based on the information I received.",\
        "plan": "[answer(I found a robot that is red. The robot is not damaged.)]"\n}',
    },
]

EXAMPLE_GRAPH_4 = {
    "objects": [],
    "regions": [
        {"name": "example_node_1", "coords": [0, 10]},
        {"name": "example_node_2", "coords": [20, 20]},
    ],
    "object_connections": [],
    "region_connections": [
        ["example_node_1", "example_node_2"],
    ],
    "robot_location": "example_node_1",
}

EXAMPLE_4 = [
    {
        "role": "user",
        "content": "task: I need to get about 40 meters east and 30 meters north. Can you find a path?\n"
        + f"scene graph: {str(EXAMPLE_GRAPH_4)}",
    },
    {
        "role": "assistant",
        "content": '{\n\
        "primary_goal": "Find a path to a location 40 meters north and 30 meters east (the location [40, 30])."\
        "relevant_graph": "unobserved_node(new region at [40, 30])",\
        "reasoning": "The user wants to get to the approximate point [40, 30]. example_node_2 is the closest observed region. However, the goal point is still over 20 meters way from example_node_2. Exploring will be inefficient, so I will not call explore_region. Rather, I will add a region in the goal direction and replan",\
        "plan": "[goto(example_node_2), extend_map(40, 30), replan()]"\n}',
    },
    # {
    #     "role": "user",
    #     "content": "updates: [add_nodes({ name: discovered_region_1, type: region, coords: [31, 25]}), add_connections([example_node_2, discovered_region_1]), update_robot_location(example_node_2)]",
    # },
    # {
    #     "role": "assistant",
    #     "content": '{\n\
    #     "primary_goal": "Find a path to a location 40 meters north and 30 meters east (the location [40, 30])."\
    #     "relevant_graph": "unobserved_node(new region at [40, 30])",\
    #     "reasoning": "I could not discover a region directly at [40, 30], but I got close, so I will explore the discovered region with a 10 meter radius",\
    #     "plan": "[explore_region(discovered_region_1, 3), replan()]"\n}',
    # },
    {
        "role": "user",
        "content": "updates: [add_nodes({ name: discovered_region_1, type: region, coords: [41, 33]}), add_connections([example_node_1, discovered_region_1]), update_node_attributes({ name: discovered_region_1, description=field}) update_robot_location(discovered_region_1)]",
    },
    {
        "role": "assistant",
        "content": '{\n\
        "primary_goal": "Find a path to a location 40 meters north and 30 meters east (the location [40, 30])."\
        "relevant_graph": "discovered_region_1",\
        "reasoning": "upon adding a region then exploring, I was able to discover a path to a new region at [41, 33], which is close to the user request. These regions appear to be in a field, which is generally traversable",\
        "plan": "[answer(I found a path from example_node_1 to discovered_region_1 at coordinates [41, 33]. This path will likely bring you through a field, which is traversable)]"\n}',
    },
]

EXAMPLE_5 = [
    {
        "role": "user",
        "content": "task: Go check if the path to example_ground_6 free or if there any blockage.\n"
        + f"scene graph: {str(EXAMPLE_GRAPH_3)}",
    },
    {
        "role": "assistant",
        "content": '{\n\
        "primary_goal": "Determine if the path from my current location (example_node_1) to example_ground_6 is clear."\
        "relevant_graph": "example_node_1, example_ground_6", \
        "reasoning": "goto uses a graph search algorithm to find the most efficient path between two connected nodes. Thus, I will call goto on my target node. If there are any problems with the path, I will received feedback. I WILL NOT call goto on each intermediate node, because that is inefficient. Futhermore, I do not need to call explore_region unless I run into issues.",\
        "plan": "[goto(example_ground_6), replan()]"}',
    },
    {"role": "user", "content": "updates: [update_robot_location(example_ground_6)]\n"},
    {
        "role": "assistant",
        "content": '{\n\
        "primary_goal": "Determine if the path from my current location (example_node_1) to example_ground_6 is free."\
        "relevant_graph": "example_node_1, example_node_4, example_node_5, example_ground_6", \
        "reasoning": "I called goto(example_ground_6), which will perform a graph-search to find the best path to my goal. The robot will then follow that plan until errors occur. I got an update that I am currently at example_ground_6, so there are not problems with the path.",\
        "plan": "[answer(the path between example_node_1 and example_ground_6 is free)]"}',
    },
]


MEDICAL_DELIVERY_EXAMPLE = [
    {
        "role": "user",
        "content": (
            "task: Deliver task-package-1 for task-order-1 from "
            "task-source-1 to task-recipient-1 at task-destination-1.\n"
            "Scene graph: {'objects': ["
            "{'name': 'task-package-1', 'coords': [0, 0]}, "
            "{'name': 'task-recipient-1', 'coords': [10, 0]}], "
            "'regions': ["
            "{'name': 'task-source-1', 'coords': [0, 0]}, "
            "{'name': 'task-region-1', 'coords': [5, 0]}, "
            "{'name': 'task-destination-1', 'coords': [10, 0]}], "
            "'object_connections': ["
            "['task-package-1', 'task-source-1'], "
            "['task-recipient-1', 'task-destination-1']], "
            "'region_connections': ["
            "['task-source-1', 'task-region-1'], "
            "['task-region-1', 'task-destination-1']], "
            "'current_location': 'task-source-1'}"
        ),
    },
    {
        "role": "assistant",
        "content": (
            "{"
            "\"primary_goal\": \"Deliver the authorized task package to "
            "the authorized recipient.\", "
            "\"relevant_graph\": \"task-source-1, task-region-1, "
            "task-destination-1, task-package-1, task-recipient-1\", "
            "\"reasoning\": \"The robot is already at the source. It must "
            "request trusted verification of the package, pick it up, "
            "navigate to the authorized destination, request trusted "
            "verification of the recipient, and hand off the same package.\", "
            "\"plan\": \"[verify_resource(task-package-1), "
            "pickup(task-package-1), goto(task-destination-1), "
            "verify_recipient(task-recipient-1), "
            "handoff(task-package-1, task-recipient-1)]\""
            "}"
        ),
    },
]
