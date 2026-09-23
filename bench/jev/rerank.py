"""Pure reorder arithmetic for the Jev experiment. No I/O.

Every arm takes search's own top-N (`base_ids`, best first) and returns a
permutation of it: nothing is added or dropped, so recall@N is identical across
arms and any recall@k difference (k < N) is purely ordering.

- `by_score` -- sort by the scorer's value alone; ties keep search's order.
  The "trust Jev outright" arm.
- `rrf_with_base` -- the scorer's ordering fused with search's through the
  engine's own `rerank_fuse` (the dark seam in core/rerank.py). The "Jev as one
  more vote" arm, and the shape an engine integration would actually take.
"""
from __future__ import annotations

from engraphy.core.rerank import rerank_fuse


def by_score(base_ids: list[str], scores: dict[str, float]) -> list[str]:
    rank = {nid: i for i, nid in enumerate(base_ids)}
    return sorted(base_ids, key=lambda nid: (-scores.get(nid, 0.0), rank[nid]))


def rrf_with_base(base_ids: list[str], scores: dict[str, float]) -> list[str]:
    # rerank_fuse only reads base_fused's ORDER in the fusing branch; the score
    # field is a placeholder. created_at is omitted, so ties fall to id order,
    # which is fine for a measurement that only compares arms on the same data.
    base_fused = [(nid, 0.0) for nid in base_ids]
    fused = rerank_fuse(base_fused, [by_score(base_ids, scores)])
    return [nid for nid, _ in fused]


ARMS = {"by_score": by_score, "rrf_with_base": rrf_with_base}
