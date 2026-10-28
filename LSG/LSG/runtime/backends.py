"""Stable model boundary. Importing this module never loads a model."""
from dataclasses import dataclass
from typing import Protocol, TypedDict

class GatePrediction(TypedDict):
    decision: str
    task_type: str | None
    response_branch: str | None
    defer_type: str | None
    confidence: dict[str, float]
    raw_heads: dict[str, str]
    gate_input: str

@dataclass(frozen=True)
class SplitterResult:
    raw_output: str

class SplitterBackend(Protocol):
    def split(self, text: str) -> SplitterResult: ...

class GateBackend(Protocol):
    def predict(self, texts: list[str], runtime_candidates: list[dict]) -> list[GatePrediction]: ...

class FakeSplitterBackend:
    """Explicitly fake; callers supply deterministic splitting behavior."""
    def __init__(self, split_fn=None):
        self.split_fn = split_fn or (lambda text: [part.strip() for part in text.split('；')])
    def split(self, text):
        parts = self.split_fn(text)
        return SplitterResult('\n'.join(f'[意图{i}] {part}' for i, part in enumerate(parts, 1)))

class FakeGateBackend:
    """Explicitly fake; returns the same contract as production backends."""
    def __init__(self, predict_fn): self.predict_fn = predict_fn
    def predict(self, texts, runtime_candidates):
        if len(texts) != len(runtime_candidates): raise ValueError('gate_batch_length_mismatch')
        return [self.predict_fn(text, candidates) for text, candidates in zip(texts, runtime_candidates)]

class InstructSplitterBackend:
    def __init__(self):
        from .models import Splitter  # lazy: only a real service start loads weights
        self._model = Splitter()
    def split(self, text): return SplitterResult(self._model.predict(text))

class EmbeddingGateBackend:
    def __init__(self):
        from .models import Gate  # lazy: only a real service start loads weights
        self._model = Gate()
    def predict(self, texts, runtime_candidates):
        if len(texts) != len(runtime_candidates): raise ValueError('gate_batch_length_mismatch')
        predictions=[]
        for index,(text,candidates) in enumerate(zip(texts,runtime_candidates),1):
            try:
                predictions.append(self._model.predict(text,candidates))
            except ValueError as error:
                raise ValueError(f"gate_intent_{index}:{error}") from error
        return predictions

class InstructGateBackend:
    def __init__(self):
        raise RuntimeError('instruct_gate_artifact_not_frozen')
    def predict(self, texts, runtime_candidates):
        raise RuntimeError('instruct_gate_artifact_not_frozen')
