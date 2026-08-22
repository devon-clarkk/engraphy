"""Recursive-CTE traversal. Normative: design/02 §Traversal (the recursive-CTE
SQL is inlined and normative there), design/07 §Canonical I/O (traverse envelope
+ detail levels), design/03 tool signature.

Path-tracked cycle guard, max_depth<=4, limit<=50, bidirectional edge types
walked both ways regardless of direction, edges require BOTH endpoints readable
(falls out of the edges_read RLS policy composing with the node policies -- the
CTE carries no visibility logic of its own). Nodes hydrate to canonical on merge
chains (resolved_from annotation); depth = minimum hops from start; start node
included at depth 0.

Boring readings (DECISIONS-DELTA.md 2026-07-17): edge_types omitted = every edge
type; max_depth default/cap 4; limit default/cap 50; direction required; detail
default summary. limit caps WALK ROWS (the inlined SQL's LIMIT); truncated = more
rows existed. A deterministic ORDER BY (depth, src_id, dst_id, type) precedes
LIMIT so the envelope is reproducible (the inlined SQL omits it). edges are the
RAW walked endpoints. Recall bumped for every returned node (02 batched-on-read).
"""
from engraphy.core.dedup import _resolve_canonical
from engraphy.core.recall import bump_recall
from engraphy.core.search import _node_envelope
from engraphy.server.db import transaction

_MAX_DEPTH_CAP = 4
_LIMIT_CAP = 50
_ENVELOPE_COLS = "id, type, scope_id, title, body, attrs, status, author_principal, created_at"

# One CTE for all directions. A "step" from a frontier node follows an edge
# forward (src->dst) when direction is out/both, and backward (dst->src) when
# direction is in/both; a bidirectional edge type is always walkable both ways
# (02). `node` is the frontier reached; `path` tracks visited nodes for the
# cycle guard; src_id/dst_id/type stay the RAW edge for the envelope.
_WALK_SQL = """
WITH RECURSIVE walk AS (
  SELECT e.src_id, e.dst_id, e.type,
         CASE WHEN e.src_id = %(start)s::uuid THEN e.dst_id ELSE e.src_id END AS node,
         1 AS depth,
         ARRAY[%(start)s::uuid,
               CASE WHEN e.src_id = %(start)s::uuid THEN e.dst_id ELSE e.src_id END] AS path
  FROM edges e JOIN edge_types et ON et.space_id = e.space_id AND et.name = e.type
  WHERE e.space_id = %(space)s
    AND (%(edge_types)s::text[] IS NULL OR e.type = ANY(%(edge_types)s))
    AND ( (e.src_id = %(start)s::uuid AND (%(fwd)s OR et.bidirectional))
       OR (e.dst_id = %(start)s::uuid AND (%(rev)s OR et.bidirectional)) )
  UNION ALL
  SELECT e.src_id, e.dst_id, e.type,
         CASE WHEN e.src_id = w.node THEN e.dst_id ELSE e.src_id END,
         w.depth + 1,
         w.path || CASE WHEN e.src_id = w.node THEN e.dst_id ELSE e.src_id END
  FROM edges e JOIN edge_types et ON et.space_id = e.space_id AND et.name = e.type
  JOIN walk w ON (e.src_id = w.node OR e.dst_id = w.node)
  WHERE e.space_id = %(space)s AND w.depth < %(max_depth)s
    AND (%(edge_types)s::text[] IS NULL OR e.type = ANY(%(edge_types)s))
    AND ( (e.src_id = w.node AND (%(fwd)s OR et.bidirectional))
       OR (e.dst_id = w.node AND (%(rev)s OR et.bidirectional)) )
    AND NOT (CASE WHEN e.src_id = w.node THEN e.dst_id ELSE e.src_id END) = ANY(w.path)
)
SELECT src_id, dst_id, type, node, depth FROM walk
ORDER BY depth, src_id, dst_id, type
LIMIT %(lim)s
"""


async def traverse(
    pool,
    space_id: str,
    principal: str,
    start_id: str,
    direction: str,
    edge_types: list[str] | None = None,
    max_depth: int = _MAX_DEPTH_CAP,
    limit: int = _LIMIT_CAP,
    detail: str = "summary",
) -> dict:
    """Walk the graph from start_id and return the 07 traverse envelope:
    {"v": 1, "detail", "nodes": [{…node…, "depth", "resolved_from?"}], "edges":
    [{"src", "dst", "type"}], "truncated"}. direction in out|in|both (required).
    """
    if direction not in ("out", "in", "both"):
        raise ValueError(f"direction must be out|in|both, got {direction!r}")
    max_depth = max(1, min(max_depth, _MAX_DEPTH_CAP))
    limit = max(1, min(limit, _LIMIT_CAP))
    fwd = direction in ("out", "both")
    rev = direction in ("in", "both")

    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()
        await cur.execute(_WALK_SQL, {
            "start": start_id, "space": space_id,
            "edge_types": list(edge_types) if edge_types else None,
            "fwd": fwd, "rev": rev, "max_depth": max_depth, "lim": limit + 1,
        })
        rows = await cur.fetchall()
        truncated = len(rows) > limit
        rows = rows[:limit]

        # min depth per reached (raw) node id + de-duplicated raw edges.
        depth_of: dict[str, int] = {}
        edges_out, seen = [], set()
        for src_id, dst_id, etype, node, depth in rows:
            nid = str(node)
            depth_of[nid] = min(depth, depth_of.get(nid, depth))
            key = (str(src_id), str(dst_id), etype)
            if key not in seen:
                seen.add(key)
                edges_out.append({"src": str(src_id), "dst": str(dst_id), "type": etype})

        # Resolve merged -> canonical (hydration resolves merge chains); the start
        # is included at depth 0 as given. Keep the min-depth entry per canonical,
        # carrying its resolved_from (the original id when a chain was followed).
        entries: dict[str, tuple[int, str | None]] = {str(start_id): (0, None)}
        for nid, d in depth_of.items():
            canon = str(await _resolve_canonical(cur, nid))
            rf = nid if canon != nid else None
            if canon not in entries or d < entries[canon][0]:
                entries[canon] = (d, rf)

        await cur.execute(
            f"SELECT {_ENVELOPE_COLS} FROM nodes WHERE space_id = %s AND id = ANY(%s)",
            (space_id, list(entries.keys())),
        )
        node_rows = {str(r[0]): r for r in await cur.fetchall()}

        nodes_out = []
        # deterministic node order: depth then id (07 doesn't pin it; this does).
        for cid, (d, rf) in sorted(entries.items(), key=lambda kv: (kv[1][0], kv[0])):
            row = node_rows.get(cid)
            if row is None:
                continue  # not readable (e.g. an unreadable start, or a merged
                          # node whose canonical the reader cannot see)
            env = _node_envelope(row, detail)
            env["depth"] = d
            if rf is not None:
                env["resolved_from"] = rf
            nodes_out.append(env)

        await bump_recall(cur, [n["id"] for n in nodes_out])

    return {"v": 1, "detail": detail, "nodes": nodes_out, "edges": edges_out, "truncated": truncated}
