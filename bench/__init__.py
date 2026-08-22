"""Engraphy's benchmark harness (design/09-benchmark-harness.md).

Deliberately a top-level package rather than `engraphy/bench/`: the harness needs
LLM clients (extractor, reader, judge) and is by construction a thing that pulls
a lot of content out of the store, both of which IMPLEMENTER.md rule 4 forbids
inside the engine. The dependency arrow points one way only -- `bench` imports
`engraphy`, never the reverse -- and `scripts/check_engine_does_not_import_bench.py`
proves it in CI.
"""

__all__ = []
