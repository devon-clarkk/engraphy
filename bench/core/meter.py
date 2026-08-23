"""The three metrics, measured in one place (design/09 §Metrics).

One module owns token counting and the clock so that no stage can be measured
differently from another — the commonest way a harness produces numbers that
cannot be compared to anyone else's is to time or count two things two ways.

**Token counting is pluggable, and its absence is recorded rather than faked.**
Counting Claude tokens requires the `count_tokens` API (tiktoken undercounts
Claude substantially — see `llm.py`). When no counter is available, `tokens`
comes back `None` and the payload's byte size is reported instead, explicitly
labelled. A `None` token count is honest; a locally-estimated one would look
like a measurement and quietly bias the headline metric.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = ["Meter", "StageTiming", "TokenCounter", "percentile", "wilson_interval"]


@runtime_checkable
class TokenCounter(Protocol):
    def count_tokens(self, text: str) -> int: ...


@dataclass(frozen=True, slots=True)
class StageTiming:
    stage: str
    seconds: float


@dataclass
class Meter:
    """Per-item measurement. One instance per question or per probe."""

    counter: TokenCounter | None = None
    stages: list[StageTiming] = field(default_factory=list)

    @contextmanager
    def stage(self, name: str):
        """Time one stage. `perf_counter`, matching engraphy/tests/bench.py."""
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.stages.append(StageTiming(name, time.perf_counter() - t0))

    def seconds_for(self, *names: str) -> float:
        wanted = set(names)
        return sum(s.seconds for s in self.stages if s.stage in wanted)

    @property
    def total_seconds(self) -> float:
        return sum(s.seconds for s in self.stages)

    def measure_payload(self, envelope: dict) -> dict:
        """Measure exactly what a client receives.

        Counted on the serialized envelope — keys and structure included, not
        just body text — because that is what an agent actually pays to have
        this memory in front of it (design/09 §Token accounting).
        """
        blob = json.dumps(envelope)
        tokens = None
        if self.counter is not None:
            tokens = self.counter.count_tokens(blob)
        return {
            "tokens": tokens,
            "bytes": len(blob.encode("utf-8")),
            "token_counter": type(self.counter).__name__ if self.counter else None,
        }

    def as_dict(self) -> dict:
        by_stage: dict[str, float] = {}
        for s in self.stages:
            by_stage[s.stage] = round(by_stage.get(s.stage, 0.0) + s.seconds, 6)
        return {"stages_seconds": by_stage, "total_seconds": round(self.total_seconds, 6)}


def percentile(samples: list[float], p: float) -> float:
    """p50/p95 with the same nearest-rank convention as engraphy/tests/bench.py,
    so harness latency figures are directly comparable to the CI budget."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    k = max(0, min(len(ordered) - 1, round((p / 100.0) * len(ordered) + 0.5) - 1))
    return ordered[k]


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval (design/09 §Accuracy).

    LoCoMo's smaller categories are a few dozen questions; a bare point estimate
    there is not a claim. Wilson rather than normal-approximation because it
    stays inside [0, 1] and behaves at the extremes, which is exactly where
    small per-category counts land.
    """
    if total == 0:
        return (0.0, 0.0)
    phat = successes / total
    denom = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denom
    margin = (z * ((phat * (1 - phat) + z * z / (4 * total)) / total) ** 0.5) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))
