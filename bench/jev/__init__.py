"""Jev reranking experiment: does a decision model reorder search's candidates
better than search does on its own?

Bench-only, on purpose. IMPLEMENTER.md rule 4 keeps model calls out of the
engine, and a remote scorer is exactly the kind of thing that rule audits for.
Nothing under `engraphy/` imports this package; the experiment reads the
shipped `search()` output and reorders it here. If the numbers justify it, the
wiring decision (and where the call lives) is a separate, recorded one.

- `client` -- the scorer seam: `JevClient` (the real API, stubbed until an
  account exists), `LexicalScorer` (a free offline control arm).
- `rerank` -- pure reorder arithmetic over search's top-N.
"""
