"""OpenAI-compatible natural-language medical rule grounding."""

from __future__ import annotations

import json
import os
from typing import Any

from roboguard.prompts.base import SYSTEM_PROMPT


class ContextualGrounding:
    def __init__(self, *, rules: list[str], model: str | None = None, client=None):
        self.rules = tuple(rule.strip() for rule in rules if rule.strip())
        self.model = model or os.getenv("ROBOGUARD_MODEL")
        self.client = client

    def get_specifications(self, scene_graph: str) -> dict[str, list[dict[str, str]]]:
        if not self.rules:
            raise ValueError("no natural-language medical navigation rules configured")
        if not self.model:
            raise RuntimeError("ROBOGUARD_MODEL is not configured")
        client = self.client
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as error:
                raise RuntimeError("openai dependency is not installed") from error
            client = OpenAI(
                api_key=os.getenv("OPENAI_API_KEY"),
                base_url=os.getenv("OPENAI_BASE_URL") or None,
            )
        response = client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({
                    "rules": self.rules,
                    "scene_graph": json.loads(scene_graph),
                }, ensure_ascii=False)},
            ],
        )
        content = response.choices[0].message.content
        try:
            value = json.loads(content)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("rule generator returned invalid JSON") from error
        return self.normalize(value)

    @staticmethod
    def normalize(value: Any) -> dict[str, list[dict[str, str]]]:
        if not isinstance(value, dict):
            raise ValueError("generated specifications must be a JSON object")
        result: dict[str, list[dict[str, str]]] = {}
        for rule, items in value.items():
            if not isinstance(items, list):
                raise ValueError(f"constraints for {rule!r} must be a list")
            normalized = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError(f"constraint for {rule!r} must be an object")
                formula = item.get("constraint", item.get("ltl_constraint"))
                reasoning = item.get("reasoning", item.get("justification", ""))
                if not isinstance(formula, str) or not formula.strip():
                    raise ValueError(f"constraint for {rule!r} is empty")
                normalized.append({"constraint": formula.strip(), "reasoning": str(reasoning)})
            result[str(rule)] = normalized
        return result
