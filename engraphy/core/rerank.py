"""Reorder-only reranking. Normative: design/07 §Exact formulas (RRF);
design/implementation/reranking-plan.md (architecture, neutrality guard).

Ships dark: `search()`'s `rerank` parameter defaults False, and hybrid_fuse
never calls into this module unless the caller opts in (mirrors the briefing
floor's `vector_floor=None` precedent -- one shared implementation, an
explicit off-by-default argument, no fork). Two pieces:

- `rerank_fuse` -- pure, no I/O. RRF-fuses the base order (search's own
  already-fused [(id, score)] list) together with zero or more signal
  orderings. Fixtures: rerank_cases.yaml, byte-exact.
- `rerank_hybrid` -- the async orchestrator hybrid_fuse calls: gathers the
  default signal producers' orderings (Stage 1: none; node-distance is the
  sole default signal once wired) and delegates the arithmetic to
  `rerank_fuse`. This is the only piece that touches the DB.

Every signal produces an ORDERING of (some subset of) the candidate ids, not
scores -- see the plan §1. The base order always participates as one more
rank-list, which is what guarantees every base id keeps a positive score and
survives (reorder-only, §5.2 of the plan): nothing is ever added to or
dropped from the base id set.
"""
import functools

_K = 60          # RRF consensus constant, same as rrf_fuse (07).
_SEED_N = 3      # top-N base-RRF hits anchor the query-relevant centroid (plan §1).
_MAX_DEPTH = 3   # bounded hop distance (plan §1 / §5.5, fixed and pinned, not swept).


def rerank_fuse(
    base_fused: list[tuple[str, float]],
    signal_ranked: list[list[str]],
    created_at: dict[str, str] | None = None,
    k: int = _K,
) -> list[tuple[str, float]]:
    """Reorder `base_fused` using `signal_ranked` as extra RRF rank-lists.

    Two branches, not one formula for every input (rerank_cases.yaml pins
    both):

    - `signal_ranked` empty -> IDENTITY. `base_fused` returned completely
      unchanged (same score floats). This is what "zero signals is a
      provable no-op" means: no arithmetic runs, so nothing can disagree with
      today's `fused`. A recompute that merely *preserves order* is not
      sufficient here -- it would still change the score field, which is
      byte-identity's whole point (search()'s rerank=False path must be
      indistinguishable from before this module existed).
    - `signal_ranked` non-empty -> every id in `base_fused` gets
      score = sum over ALL rank-lists (base_fused's own order counts as one,
      plus each entry in `signal_ranked`) of 1/(k+rank) in that list, 0 if
      absent from a given list -- exactly rrf_fuse's arithmetic (07),
      generalized from 2 legs to N. `base_fused`'s own score values are
      discarded in this branch; only its ORDER feeds the recompute.

    Reorder-only in both branches: the output id set always equals
    `base_fused`'s id set. An id a signal ranks but `base_fused` never
    carried is ignored (never grows the set); an id `base_fused` carries but
    every signal omits still holds a positive score from the base rank-list
    alone (never drops the set). Ties -> created_at desc, then id asc (same
    total order as rrf_fuse).
    """
    if not signal_ranked:
        return list(base_fused)

    base_ranked = [node_id for node_id, _ in base_fused]
    scores: dict[str, float] = {node_id: 0.0 for node_id in base_ranked}
    for rank, node_id in enumerate(base_ranked, start=1):
        scores[node_id] += 1.0 / (k + rank)
    for signal in signal_ranked:
        for rank, node_id in enumerate(signal, start=1):
            if node_id in scores:
                scores[node_id] += 1.0 / (k + rank)

    created_at = created_at or {}

    def _cmp(a: str, b: str) -> int:
        if scores[a] != scores[b]:
            return -1 if scores[a] > scores[b] else 1
        ca, cb = created_at.get(a, ""), created_at.get(b, "")
        if ca != cb:
            return -1 if ca > cb else 1
        return -1 if a < b else (1 if a > b else 0)

    ordered = sorted(scores, key=functools.cmp_to_key(_cmp))
    return [(node_id, round(scores[node_id], 6)) for node_id in ordered]


async def rerank_hybrid(cur, space_id, base_fused, similarity, created_at_map, node_map):
    """The orchestrator hybrid_fuse calls when `rerank=True`: gathers every
    default signal's ordering and RRF-fuses them with `base_fused`.
    node-distance is the sole default signal (plan §1 recommendation, §3
    Stage 2); recency/lexical-overlap/MMR are ablation-only, never wired here
    (plan §1, §5.6 -- no per-question/category branching, and no signal is
    added to the default set without a recorded decision).

    Signature carries everything a signal producer could need (`cur`/
    `space_id` for graph walks, `similarity` for vector-adjacent signals,
    `created_at_map` for the tie-break, `node_map` for text) so a future
    signal slots in here without another `hybrid_fuse` change.
    """
    signal_ranked = [await node_distance_signal(cur, space_id, base_fused)]
    return rerank_fuse(base_fused, signal_ranked, created_at=created_at_map)


# ---- node-distance -- the centerpiece signal (plan §1) -----------------------
# Zep-adapted: reorder candidates by graph distance from the query-relevant
# centroid (the top-N base-RRF hits). Reuses traverse.py's recursive-CTE walk
# shape (path-tracked cycle guard, bounded depth) but purpose-built for
# distance-only output: direction is always "both" (undirected proximity is
# what this signal wants, unlike traverse()'s directional envelope), which
# also means the edge_types join traverse() needs for its fwd/rev/bidirectional
# check is unnecessary here -- with both directions always walkable the join
# resolves to a constant true. RLS is unaffected either way: edges_read (0007)
# is a table-level FORCE ROW LEVEL SECURITY policy enforced by Postgres on
# every SELECT against `edges`, independent of this query's shape, exactly as
# it is for traverse()'s own walk.

_DISTANCE_WALK_SQL = """
WITH RECURSIVE walk AS (
  SELECT CASE WHEN e.src_id = %(start)s::uuid THEN e.dst_id ELSE e.src_id END AS node,
         1 AS depth,
         ARRAY[%(start)s::uuid,
               CASE WHEN e.src_id = %(start)s::uuid THEN e.dst_id ELSE e.src_id END] AS path
  FROM edges e
  WHERE e.space_id = %(space)s
    AND (e.src_id = %(start)s::uuid OR e.dst_id = %(start)s::uuid)
  UNION ALL
  SELECT CASE WHEN e.src_id = w.node THEN e.dst_id ELSE e.src_id END,
         w.depth + 1,
         w.path || CASE WHEN e.src_id = w.node THEN e.dst_id ELSE e.src_id END
  FROM edges e
  JOIN walk w ON (e.src_id = w.node OR e.dst_id = w.node)
  WHERE e.space_id = %(space)s AND w.depth < %(max_depth)s
    AND NOT (CASE WHEN e.src_id = w.node THEN e.dst_id ELSE e.src_id END) = ANY(w.path)
)
SELECT node, MIN(depth) AS depth FROM walk GROUP BY node
"""


async def _min_hop_distances(cur, space_id, seed_ids, max_depth=_MAX_DEPTH):
    """Min hop-distance from ANY seed to every node reached within
    `max_depth`, walking outward from each seed independently and merging by
    minimum (N is fixed and small -- _SEED_N=3 -- so N separate bounded walks
    stay cheap; a multi-source single CTE would need `traverse.py`'s shape
    changed, which this deliberately does not do). Seeds are their own
    distance 0, whether or not any edge touches them.
    """
    best: dict[str, int] = {}
    for seed in seed_ids:
        await cur.execute(_DISTANCE_WALK_SQL, {"start": seed, "space": space_id, "max_depth": max_depth})
        for node, depth in await cur.fetchall():
            nid = str(node)
            if nid not in best or depth < best[nid]:
                best[nid] = depth
    for seed in seed_ids:
        best[seed] = 0
    return best


async def node_distance_signal(cur, space_id, base_fused, max_depth=_MAX_DEPTH, seed_n=_SEED_N):
    """The node-distance signal (plan §1): an ORDERING (not scores) of
    `base_fused`'s id set, sorted `(distance asc, base-RRF-rank asc)` from
    the top-`seed_n` base-RRF hits. Unreachable candidates (no path within
    `max_depth`) sort last -- via the `max_depth + 1` sentinel, always worse
    than any reached distance -- but are RETAINED, never dropped (the base
    leg in `rerank_fuse` keeps every id present regardless of what a signal
    returns).

    A no-op on an edgeless store falls out of the tie-break rather than
    needing a special case: with no edges, every non-seed candidate ties at
    the sentinel distance, so the secondary key (original base rank) alone
    determines order -- which reproduces `base_fused`'s own order exactly,
    because the seeds (already the top-`seed_n` base ranks) sort first at
    distance 0 and everyone else keeps their relative base-rank order.
    """
    base_ranked = [node_id for node_id, _ in base_fused]
    if len(base_ranked) < 2:
        return base_ranked
    seeds = base_ranked[:seed_n]
    distances = await _min_hop_distances(cur, space_id, seeds, max_depth=max_depth)
    rank_of = {node_id: i for i, node_id in enumerate(base_ranked)}
    return sorted(base_ranked, key=lambda nid: (distances.get(nid, max_depth + 1), rank_of[nid]))
