"""Hybrid search. Normative: design/02 §Search + design/07 §Exact formulas (RRF).

Vector leg (top-30 cosine) + lexical leg (websearch_to_tsquery, top-30, ts_rank_cd),
RRF k=60, tie-break: created_at DESC then id ASC (total order).
Scope set: requested ∪ reader's readable ambient scopes; 'all' = all readable.
Fixtures: rrf_cases.yaml (pure fusion arithmetic, byte-exact).

The embed step is the CALLER's job's twin here (search embeds the QUERY, with the
search_query: prefix) and MUST stay outside the transaction -- trap 3, same as the
write path. Recall stats batched on every read (02): bump_recall over the SURFACED
results only, inside the read's transaction (QUESTIONS.md
"recall-stats-vs-nodes-touch", resolved 2026-07-16; migration 0012).
"""
import functools

from psycopg.types.json import Jsonb

from engraphy.core import metrics
from engraphy.core.dedup import _vector_literal
from engraphy.core.embedding import embed_query
from engraphy.core.recall import bump_recall
from engraphy.core.scope_set import resolve_scope_set
from engraphy.core.sentinel import SENTINEL_NODE_TYPE
from engraphy.server.db import transaction

# E1 constants. 02 says "Constants per space in config"; per-space config-reading
# for search is deferred to the search gate (mirroring how dedup thresholds
# started fixed and became config-read only after Fable's review) -- see the gate
# report / DECISIONS-DELTA. k=60 is baked into rrf_fuse.
_LEG_CAP = 30       # top-N per leg before fusion (02)
_MAX_LIMIT = 25     # result cap (02 "return limit (<= 25)")

# Read-time near-duplicate collapse threshold. Two ACTIVE nodes whose stored
# document embeddings are >= this cosine are treated as the same fact restated,
# and only the higher-ranked one is surfaced. The value is the dedup MERGE band
# (`dedup.t_high`, default 0.95 -- design/02 §Deduplication write path): the
# write path AUTO-MERGES at >= t_high precisely because that is its calibrated
# "same fact restated" bar. Read-time collapse reuses the SAME bar, on the SAME
# document<->document cosine scale the bands are calibrated on, so it is the exact
# threshold the system already trusts to mean "identical fact" -- NOT a number
# chosen against any benchmark. Duplicates persist into the read set for the
# reasons write-time dedup cannot catch them: they were written to different
# scopes (dedup candidates are scope-filtered), or the confirm band resolved them
# `distinct`. Deliberately conservative: at 0.95 only near-verbatim restatements
# collapse, so a genuinely distinct fact is never dropped.
#
# Because it IS the merge band, it tracks the merge band across embedding
# profiles rather than restating a literal. On int8 the write path merges at 0.94,
# so read-time collapse must too: leaving this at 0.95 would surface as duplicates
# exactly the pairs the write path had already decided were one fact.
#
# ## Where a profile needs its own value, and why one does
#
# Tracking the write band is the right default and it is not always the right
# answer, because the two decisions do not fail the same way. A write threshold
# that is too high merges facts that were distinct, and the store is silently
# wrong afterwards with no way to tell; that asymmetry is why every write band in
# this system takes the MIDDLE of the window its fixtures admit, buying the most
# room on both sides. A read threshold that is too low merely hides a result from
# one query, which is recoverable and visible, so it can afford to sit at the
# permissive END of that same window.
#
# On the fp32 and int8 spaces the distinction has never mattered enough to
# measure: their similarities are spread widely enough that the write band and
# the top of its window are close together, and both profiles keep the default.
# On `micro` they are not. gte-small packs its similarities into a narrower
# range, so at the write band of 0.955 read-time collapse starts folding together
# turn-level facts that are genuinely different, and the cost is measurable:
# fused recall@10 over 498 LoCoMo questions is
#
#     collapse at 0.955 (the write band)   0.6124
#     collapse at 0.97                     0.6446
#     collapse disabled entirely           0.6406
#
# 0.97 is not fitted to that measurement. It comes from the same labelled window
# the write band comes from: micro's 17 dedup fixtures admit `t_high` anywhere in
# (0.9393, 0.9710] on every host measured, so 0.97 still merges every pair the
# fixtures call a merge and still separates every pair they call pending. The
# benchmark's role was to show that the difference between the two ends of that
# window is worth something at read time, and it is worth roughly 3 points of
# recall. (0.9710, the exact top of the window, measures 0.6386, which is three
# questions from the 0.97 figure and inside the noise of one 498-question run.
# 0.97 is preferred for the cross-host margin, not for those three questions.)
#
# Whether fp32 and int8 would also gain from moving off their write bands is an
# open question that has NOT been measured here, and they are deliberately left
# where they are rather than moved on an argument.


#: Profiles whose read-time collapse sits somewhere other than their write band.
#: See the reasoning above; a profile absent from here uses `dedup.t_high`.
_PROFILE_READ_DEDUP_SIM = {
    "micro": 0.97,
}


def _read_dedup_sim() -> float:
    from engraphy.core.dedup import BandThresholds

    profile = _embedding_profile()
    if profile in _PROFILE_READ_DEDUP_SIM:
        return _PROFILE_READ_DEDUP_SIM[profile]
    return BandThresholds.for_profile(profile).t_high


def _embedding_profile() -> str:
    from engraphy.core import embedding

    return embedding.profile()


#: The fp32 value, kept as a module constant for the callers and tests that state
#: it directly. `_read_dedup_sim()` is what the query paths call.
_READ_DEDUP_SIM = 0.95


def rrf_fuse(
    vector_ranked: list[str],
    lexical_ranked: list[str],
    created_at: dict[str, str] | None = None,
    k: int = 60,
    limit: int | None = None,
) -> list[tuple[str, float]]:
    """Nodes appearing in either leg, fused by RRF score descending.

    created_at values are RFC 3339 UTC strings (07's uniform timestamp format) — same
    format + precision sorts lexicographically identically to chronologically, so ties
    are broken by direct string comparison, no datetime parsing needed.
    """
    scores: dict[str, float] = {}
    for rank, node_id in enumerate(vector_ranked, start=1):
        scores[node_id] = scores.get(node_id, 0.0) + 1.0 / (k + rank)
    for rank, node_id in enumerate(lexical_ranked, start=1):
        scores[node_id] = scores.get(node_id, 0.0) + 1.0 / (k + rank)

    created_at = created_at or {}

    def _cmp(a: str, b: str) -> int:
        if scores[a] != scores[b]:
            return -1 if scores[a] > scores[b] else 1
        ca, cb = created_at.get(a, ""), created_at.get(b, "")
        if ca != cb:
            return -1 if ca > cb else 1
        return -1 if a < b else (1 if a > b else 0)

    ordered = sorted(scores, key=functools.cmp_to_key(_cmp))
    if limit is not None:
        ordered = ordered[:limit]
    return [(node_id, round(scores[node_id], 6)) for node_id in ordered]


async def search(
    pool,
    space_id: str,
    principal: str,
    scope: str,
    query: str,
    source_client: str,
    types: list[str] | None = None,
    limit: int = _MAX_LIMIT,
    include_inactive: bool = False,
    detail: str = "full",
    collapse_near_dupes: bool = True,
    no_scope_all: bool = False,
) -> dict:
    """Hybrid search (design/02 §Search): embed the query (search_query prefix),
    run the vector and lexical legs over the resolved scope set, RRF-fuse, cap
    to `limit`, and return the 07 §Canonical I/O search envelope.

    scope: a specific scope id, or 'all' (every readable scope). The resolved
    set is intersected with the reader's readable scopes, so a requested-but-
    unreadable scope contributes nothing and never leaks via scopes_searched.
    detail: 'full' (default, includes body) | 'summary' (body omitted).
    collapse_near_dupes: default-on read-time near-duplicate collapse (design/02
    merge band reused at read time -- see `_READ_DEDUP_SIM`). Keeps the highest-
    ranked instance of each near-identical cluster; a genuinely distinct fact is
    never dropped. Disableable for ablation, but shipped on.
    no_scope_all: the CALLING TOKEN's scope restriction (api_tokens.no_scope_all,
    migration 0023), threaded from AuthContext by the tool dispatcher. True means
    scope='all' is refused outright (ENGRAPHY_SCOPE) instead of resolved -- see
    core.scope_set. Defaults False so every direct/test caller keeps the old
    unrestricted behaviour.
    A scope='all' read writes one audit_log row -- the exfiltration tripwire (03).
    """
    query_vec = embed_query(query)  # OUTSIDE the transaction (trap 3)
    limit = max(1, min(limit, _MAX_LIMIT))

    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()

        scope_set = await resolve_scope_set(cur, scope, no_scope_all, "search")
        scopes_searched = sorted(scope_set)

        results: list[dict] = []
        truncated = False
        if scope_set:
            top, similarity, node_map, truncated = await hybrid_fuse(
                cur, space_id, query_vec, query, scope_set, types, include_inactive, limit,
                collapse_near_dupes=collapse_near_dupes,
            )
            if top:
                edge_counts = await _edge_counts(cur, space_id, [nid for nid, _ in top])
                for nid, score in top:
                    item = {"node": _node_envelope(node_map[nid], detail), "score": score}
                    if nid in similarity:
                        item["similarity"] = round(similarity[nid], 2)
                    item["edge_count"] = edge_counts.get(nid, 0)
                    results.append(item)

                # Recall stats batched on every read (02): bump the SURFACED
                # nodes only. The nodes_touch trigger (0012) leaves updated_at
                # alone for this recall-only update.
                await bump_recall(cur, [nid for nid, _ in top])

        if scope == "all":
            await cur.execute(
                "INSERT INTO audit_log (space_id, principal, client_name, action, detail) "
                "VALUES (%s, %s, %s, %s, %s)",
                (space_id, principal, source_client, "search",
                 Jsonb({"scope": "all", "query": query, "results": len(results)})),
            )

    # Usage metrics (design/03 §stats): one question asked, answered iff it
    # surfaced anything, and memory_reused += the count of stored facts returned
    # (a reuse proxy -- see core.metrics). Best-effort, in its OWN transaction
    # after the read above committed: a metrics hiccup never fails a search.
    await metrics.bump_safe(pool, space_id, principal, {
        metrics.QUESTIONS_ASKED: 1,
        metrics.ANSWERED: 1 if results else 0,
        metrics.MEMORY_REUSED: len(results),
    })

    return {
        "v": 1,
        "detail": detail,
        "results": results,
        "scopes_searched": scopes_searched,
        "truncated": truncated,
    }


async def hybrid_fuse(
    cur, space_id, query_vec, query, scope_set, types, include_inactive, limit,
    vector_floor=None, collapse_near_dupes=True,
):
    """Both hybrid legs + RRF fusion + union hydration, inside the CALLER's
    transaction (cur) -- the one shared hybrid implementation. search() calls it
    for its envelope; the briefing engine's `semantic: true` section calls it
    for hint retrieval (design/02 §The briefing engine: "the semantic: true
    section runs hybrid search over hint"), so there is exactly one place the
    leg SQL and fusion arithmetic live and the two paths can never drift.

    The query embedding is the caller's job (and must happen OUTSIDE the
    transaction -- trap 3); this function never embeds.

    vector_floor: briefing's semantic-section relevance floor (02 §The briefing
    engine / 07 §Exact formulas; QUESTIONS.md "semantic-section-relevance-floor",
    resolved 2026-07-17 Devon option b). When set, vector-leg candidates with
    query<->document similarity < vector_floor are dropped from the vector leg
    BEFORE fusion (`>=` survives -- the bands' boundary convention). The lexical
    leg is NEVER floor-dropped: a websearch_to_tsquery hit means the hint's own
    words occur in the node, an affirmative relevance signal. search() never
    passes this (default None = unfloored, byte-identical to shipped behavior);
    briefing passes its resolved floor -- one implementation, one leg SQL, one
    fusion arithmetic, diverging only by this explicit argument.

    collapse_near_dupes: default-on read-time near-duplicate collapse applied to
    the fused list BEFORE the `limit` cut, so the highest-ranked instance of each
    near-identical cluster survives and duplicates do not consume result slots
    (see `_READ_DEDUP_SIM` for the threshold and its justification). search() and
    briefing's semantic sections both get it; pass False to disable (ablation).

    Returns (top, similarity, node_map, truncated): top = fused [(id, score)]
    capped to `limit`; similarity = vector-leg cosine per id (leg hits only);
    node_map = hydrated envelope rows for every fused id; truncated = whether
    fusion (after collapse) produced more than `limit`.
    """
    vector_hits = await _vector_leg(cur, space_id, query_vec, scope_set, types, include_inactive)
    if vector_floor is not None:
        vector_hits = [(nid, sim) for nid, sim in vector_hits if sim >= vector_floor]
    lexical_ids = await _lexical_leg(cur, space_id, query, scope_set, types, include_inactive)
    vector_ids = [nid for nid, _ in vector_hits]
    similarity = {nid: sim for nid, sim in vector_hits}
    union = list({*vector_ids, *lexical_ids})
    if not union:
        return [], {}, {}, False

    node_map, created_at_map = await _hydrate_union(cur, union)
    fused = rrf_fuse(vector_ids, lexical_ids, created_at=created_at_map)
    if collapse_near_dupes and len(fused) > 1:
        fused = await _collapse_near_dupes(cur, space_id, fused)
    truncated = len(fused) > limit
    return fused[:limit], similarity, node_map, truncated


def _collapse_by_pairs(
    fused: list[tuple[str, float]], near_pairs: set[frozenset]
) -> list[tuple[str, float]]:
    """Pure collapse: walk `fused` in rank order and drop any node that is a
    near-duplicate of a HIGHER-ranked node already kept, so the best-ranked
    instance of each near-identical cluster survives. `near_pairs` is a set of
    2-element frozensets of node ids judged near-identical. Order-preserving and
    reorder-only in spirit: it never adds, never reorders, only removes proven
    restatements. Factored out of the SQL so the collapse logic is unit-testable
    without a database."""
    adj: dict[str, set[str]] = {}
    for pair in near_pairs:
        a, b = tuple(pair)
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    kept: list[tuple[str, float]] = []
    kept_ids: set[str] = set()
    for nid, score in fused:
        if adj.get(nid, set()) & kept_ids:
            continue  # near-duplicate of an already-surfaced, higher-ranked node
        kept.append((nid, score))
        kept_ids.add(nid)
    return kept


async def _near_dup_pairs(cur, space_id, ids, threshold=None):
    """Pairs among `ids` whose stored embeddings are >= `threshold` cosine.
    Computed in-DB via pgvector's cosine-distance operator (`<=>`); embeddings are
    unit-normalised at write time, so cosine distance = 1 - cosine similarity and
    `sim >= threshold` is `distance <= 1 - threshold`. A NULL embedding yields NULL
    distance and is excluded (never a duplicate). Runs inside the read transaction,
    so RLS already bounds it to readable rows.

    `threshold` defaults to the active profile's merge band rather than a literal,
    so read-time collapse and the write path agree on what "the same fact" means."""
    threshold = _read_dedup_sim() if threshold is None else threshold
    if len(ids) < 2:
        return set()
    await cur.execute(
        "SELECT a.id, b.id FROM nodes a JOIN nodes b "
        "ON a.space_id = b.space_id AND a.id < b.id "
        "WHERE a.space_id = %s AND a.id = ANY(%s) AND b.id = ANY(%s) "
        "AND (a.embedding <=> b.embedding) <= %s",
        (space_id, list(ids), list(ids), 1.0 - threshold),
    )
    return {frozenset((str(a), str(b))) for a, b in await cur.fetchall()}


async def _declared_distinct_pairs(cur, space_id, ids):
    """Pairs among `ids` joined by an engine-attached "declared distinct" edge and
    therefore EXEMPT from read-time collapse (Phase B §4;
    design/analysis/fact-searchability-model.md §2.5):

    - `same_topic`: the write path itself banded the pair as the same topic but
      judged their content distinct and kept BOTH. Collapsing them at read time
      would re-hide exactly the member row merge-link preserved -- the two
      features would fight.
    - `relates_to`: the same "declared distinct" assertion from the other
      direction -- the engine attaches it on a human/agent `distinct` resolution,
      and a hand-drawn `relates_to` between two >=0.95 texts is likewise an
      explicit statement that both belong. Included deliberately (one
      DECISIONS-DELTA line), not silently widened.

    Filtering by edge EXISTENCE (one indexed lookup, either orientation), never by
    a `dedup_log` join -- that is the point of the dedicated `same_topic` type."""
    if len(ids) < 2:
        return set()
    await cur.execute(
        "SELECT src_id, dst_id FROM edges "
        "WHERE space_id = %s AND type IN ('same_topic', 'relates_to') "
        "AND src_id = ANY(%s) AND dst_id = ANY(%s)",
        (space_id, list(ids), list(ids)),
    )
    return {frozenset((str(src), str(dst))) for src, dst in await cur.fetchall()}


async def _collapse_near_dupes(cur, space_id, fused, threshold=None):
    """Drop read-time near-duplicates from the fused list, keeping the highest-
    ranked instance of each near-identical cluster (design/02 merge band, reused
    at read time -- see `_READ_DEDUP_SIM`). Pairs the engine or a caller declared
    distinct (`same_topic`/`relates_to`, §4) are exempt: they are near-identical
    by embedding but deliberately kept as separate facts, so collapsing them would
    undo merge-link."""
    ids = [nid for nid, _ in fused]
    pairs = await _near_dup_pairs(cur, space_id, ids, threshold)
    if not pairs:
        return fused
    pairs -= await _declared_distinct_pairs(cur, space_id, ids)
    if not pairs:
        return fused
    return _collapse_by_pairs(fused, pairs)


def _status_and_type_clauses(sql: str, params: list, space_id, scope_set, types, include_inactive):
    """Shared filter tail: space + scope set (+ status='active' unless
    include_inactive, + type = ANY(types) when given). RLS on nodes independently
    limits to readable scopes; the explicit scope_set narrows within that."""
    sql += " WHERE space_id = %s AND scope_id = ANY(%s)"
    params += [space_id, list(scope_set)]
    # The restore sentinel is excluded by TYPE, not merely by status. It is
    # archived, which hides it from the default path -- but `include_inactive`
    # is an agent-callable argument, so status alone would let any client see an
    # engine-internal row simply by asking for inactive nodes. design/04 says the
    # sentinel can never surface in search; that guarantee has to be independent
    # of the caller's flags.
    sql += " AND type <> %s"
    params.append(SENTINEL_NODE_TYPE)
    if not include_inactive:
        sql += " AND status = 'active'"
    if types:
        sql += " AND type = ANY(%s)"
        params.append(list(types))
    return sql, params


async def _vector_leg(cur, space_id, query_vec, scope_set, types, include_inactive):
    """Top-30 by cosine (nearest first). Returns [(id, similarity)]; similarity =
    1 - cosine_distance, carried so vector-leg hits can report it in the envelope."""
    q = _vector_literal(query_vec)
    sql, params = _status_and_type_clauses(
        "SELECT id, 1 - (embedding <=> %s::vector) AS similarity FROM nodes",
        [q], space_id, scope_set, types, include_inactive,
    )
    sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
    params += [q, _LEG_CAP]
    await cur.execute(sql, params)
    return [(str(nid), sim) for nid, sim in await cur.fetchall()]


async def _lexical_leg(cur, space_id, query, scope_set, types, include_inactive):
    """Top-30 by ts_rank_cd over websearch_to_tsquery (02). Only rows the query
    actually matches; ordered best-rank first."""
    sql, params = _status_and_type_clauses(
        "SELECT id FROM nodes", [], space_id, scope_set, types, include_inactive,
    )
    sql += " AND search @@ websearch_to_tsquery('english', %s)"
    params.append(query)
    sql += " ORDER BY ts_rank_cd(search, websearch_to_tsquery('english', %s)) DESC LIMIT %s"
    params += [query, _LEG_CAP]
    await cur.execute(sql, params)
    return [str(nid) for (nid,) in await cur.fetchall()]


async def _hydrate_union(cur, union_ids):
    """One fetch of the <=60-id leg union: node rows for the envelope, and the
    created_at map (RFC 3339 strings) rrf_fuse tie-breaks on."""
    await cur.execute(
        "SELECT id, type, scope_id, title, body, attrs, status, author_principal, created_at "
        "FROM nodes WHERE id = ANY(%s)",
        (union_ids,),
    )
    node_map, created_at_map = {}, {}
    for row in await cur.fetchall():
        nid = str(row[0])
        node_map[nid] = row
        created_at_map[nid] = row[8].isoformat()
    return node_map, created_at_map


async def _edge_counts(cur, space_id, ids):
    """Per-node edge count through RLS-filtered edges (visibility plan trap 7:
    counts never cross the readable boundary -- the edges policy already requires
    both endpoints readable). Counts edges where the node is src OR dst."""
    if not ids:
        return {}
    await cur.execute(
        "SELECT node_id, count(*) FROM ("
        "  SELECT src_id AS node_id FROM edges WHERE space_id = %s AND src_id = ANY(%s) "
        "  UNION ALL "
        "  SELECT dst_id FROM edges WHERE space_id = %s AND dst_id = ANY(%s)"
        ") x GROUP BY node_id",
        (space_id, ids, space_id, ids),
    )
    return {str(nid): count for nid, count in await cur.fetchall()}


def _node_envelope(row, detail):
    """07 node envelope; 'summary' omits body, nothing else changes.

    Q1: addenda never ships in `attrs` on the wire -- merge history is
    surfaced exactly once, by `get`, as a top-level field (QUESTIONS.md Q1).
    Strip it here so search/traverse/briefing (all three build node dicts
    through this one function) can't leak it."""
    nid, ntype, scope, title, body, attrs, status, author, created_at = row
    node = {"id": str(nid), "type": ntype, "scope": scope, "title": title}
    if detail == "full":
        node["body"] = body
    if "addenda" in attrs:
        attrs = {k: v for k, v in attrs.items() if k != "addenda"}
    node["attrs"] = attrs
    node["status"] = status
    node["author"] = author
    node["created_at"] = created_at.isoformat()
    return node
