"""The shared core: everything that is identical across benchmarks.

design/09 §The shared core / shim boundary. Per-benchmark code lives in
`bench/adapters/` and may only translate (raw format -> `Corpus`) and score.
It may not ingest, retrieve, answer, count, or time -- those all live here, so
that no benchmark can be measured differently from another.
"""

__all__ = []
