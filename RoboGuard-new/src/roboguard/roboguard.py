"""Optional LLM grounding plus Spot-backed plan validation."""

from roboguard.generator import ContextualGrounding
from roboguard.synthesis import ControlSynthesis


class RoboGuard:
    def __init__(self, *, rules, model=None, client=None, synthesizer_factory=ControlSynthesis):
        self.grounding = ContextualGrounding(rules=list(rules), model=model, client=client)
        self.synthesizer_factory = synthesizer_factory
        self.specifications = None
        self.synthesizer = None

    def update_context(self, graph_json: str) -> None:
        self.specifications = self.grounding.get_specifications(graph_json)
        formulas = [
            item["constraint"]
            for items in self.specifications.values()
            for item in items
            if item["constraint"] != "NONE"
        ]
        self.synthesizer = self.synthesizer_factory(formulas)

    def get_safety_specs(self):
        return self.specifications

    def validate_plan(self, plan):
        if self.synthesizer is None:
            raise RuntimeError("RoboGuard context has not been initialized")
        return self.synthesizer.validate_action_sequence(plan)
