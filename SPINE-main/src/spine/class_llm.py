import json
import os
from typing import Dict, List, Optional, Tuple

from openai import OpenAI

BASE_SYSTEM_INSTRUCTIONS = """"Agent Role: You must configure the vision system for excellent graph planner. The plan must fulfill a given task provided by the user given an incomplete graph representation of an environment. You will configure the computer vision system used by this planner.

The vision system has an open vocabulary object detection. You must decide what classes it will detect for a given task.\n\nGiven a task, provide a list of classes and  provide your reason for choosing these classes. Your output will be given to an object detector, so only provide objects classes. Do not provide classes of rooms or spatial concepts. For example, instead of office, list chair, table, etc. Instead of seating area, list chair, bench, etc.. Your output must be a valid JSON string (it will be parsed by the python `json.loads` function) in the form:
{
    classes: [<list of classes>],
    task_summary: <is your task primarily mapping and exploring space, finding a path, etc.>
    reason: <reasoning>
    }

Problem setup:
- You will be operating on a small mobile robot with a camera, so provide classes that would be visible from that platform.

- You may also be given a short description of the environment.

DO NOT INCLUDE spatial concepts such as 
- road
- paths
- field


Types of problems:
- if the problem only call for finding a path, you do not need to generate many classes. 5 will suffice.
- if the problem calls for inspecting space or mapping new areas, you should generate up to 10 classes, or more if needed. However, too many classes will result in missed detections of false positives 

Requirements:
- At the very least, include ALL OBJECTS referenced in a given task
- for example, if a user asks `I left my keys on the couch`, you should at least provide couch and keys as classes because the user directly references those objects.
"""


BASE_PROMPT = [
    {
        "role": "system",
        "content": [
            {
                "text": BASE_SYSTEM_INSTRUCTIONS,
                "type": "text",
            }
        ],
    }
]

EXAMPLES = [
    {
        "role": "user",
        "content": [
            {
                "text": "What is in the outdoor scene?",
                "type": "text",
            }
        ],
    },
    {
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": '{"task_summary": "the problem calls for a general description of an environment, so I will list many classes.",\
                "reason": "the vision system must detect various common outdoor objects to infer and fill in the incomplete areas. There are many types of areas that could be in a scene, including a park, a construction site, a forest, etc. I will include a wide array of objects that could be found in these areas."\n    \
                "classes": ["bike", "chair", "table", "tree", "building", "car", "road", "person", "park", "playground", "bench", "streetlight", "bus", "truck", "motorcycle", "crosswalk", "sidewalk", "river", "bridge", "food", "stop sign"],\n}',
            }
        ],
    },
    {
        "role": "user",
        "content": [
            {
                "text": "I was doing some groceries and I lost my bike. What happened?",
                "type": "text",
            }
        ],
    },
    {
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": '{\n \
                "task_summary": "The problem implies that we need to find the users bike. Most importantly, I will list that as a class. I will also list objects where a bike might be found.", \
                "reason": "I need to find a bike and objects related to a bike", \
                "classes": ["bike", "bike rack", "stop sign", "pedestrian", "wheel"]',
            }
        ],
    },
    {
        "role": "user",
        "content": [
            {
                "text": "What happened to my phone?",
                "type": "text",
            }
        ],
    },
    {
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": '{\n \
                "task_summary": "The problem implies that we need to find the users phone. Most importantly, I will list that as a class. I will also list objects where a phone might be found.", \
                "reason": "I need to find a bike and objects related to a bike", \
                "classes": ["phone", "desk", "charger", "chair", "table]',
            }
        ],
    },
]


def create_prompt(
    task: str, location_description: str = ""
) -> Dict[str, Dict[str, str]]:
    prompt = []
    prompt.extend(BASE_PROMPT)
    prompt.extend(EXAMPLES)

    task_prompt = task

    if location_description != "":
        task_prompt += f" {location_description}"

    user_input = [
        {
            "role": "user",
            "content": [
                {
                    "text": task_prompt,
                    "type": "text",
                }
            ],
        }
    ]

    prompt.extend(user_input)
    return prompt


class ClassLLM:
    DEFAULT_RESPONSE = {
        "classes": ["car", "truck", "robot"],
        "reason": "Network unavailable for GPT call. Returning default classes",
        "task_summary": "Unknown. Returning default classes",
    }
    EXCLUDE_CLASSES = [
        "ground",
        "water",
        "road",
        "grass",
        "water body",
        "sand",
        "dock",
        "building",
        "park",
    ]

    def filter_classes(self, class_list: List[str]) -> List[str]:
        filtered_class_list = []
        for class_label in class_list:
            if class_label not in self.EXCLUDE_CLASSES:
                filtered_class_list.append(class_label)

        return filtered_class_list

    def __init__(self, n_attempts: Optional[int] = 3) -> None:
        self.client = OpenAI()
        self.n_attempts = n_attempts

    def scene_description_from_nodes(self, nodes: List[str]) -> str:
        prompt = f"The robot is outside, and it has a scene graph representing its environment. These are the nodes in the graph: {nodes}"
        return prompt

    def try_query(self, task: str, location_description: str):
        try:
            response = self.client.chat.completions.create(
                model=os.getenv(
                    "SPINE_MODEL",
                    os.getenv("ROBOGUARD_MODEL", "gpt-4o"),
                ),
                messages=create_prompt(
                    task=task, location_description=location_description
                ),
                temperature=1,
                max_tokens=1024,
                top_p=1,
                frequency_penalty=0,
                presence_penalty=0,
            )
            return response, True
        except Exception as ex:
            return "", False

    def request(
        self, task: str, location_description: str = ""
    ) -> tuple[bool, dict[str, str]]:
        formatted_answer = {"classes": [], "reason": [], "task_summary": []}
        success = False

        for _ in range(self.n_attempts):
            response, success = self.try_query(
                task=task, location_description=location_description
            )

            # unclear if we should return defaults here
            if not success:
                return True, self.DEFAULT_RESPONSE

            answer = response.choices[0].message.content

            answer = answer.strip("```").strip("json")

            try:
                llm_answer = json.loads(answer, strict=False)
                success = True
            except Exception as ex:
                return False, ""

            if "classes" in llm_answer:
                llm_classes = self.filter_classes(llm_answer["classes"])
                formatted_answer["classes"].extend(llm_classes)
            if "reason" in llm_answer:
                formatted_answer["reason"].append(llm_answer["reason"])
            if "task_summary" in llm_answer:
                formatted_answer["task_summary"].append(llm_answer["task_summary"])

            return success, formatted_answer

        return False, answer
