"""Point lookup by id (design/07 s.Canonical tool I/O: `get`). Normative fixture:
fixtures/wire/get.json.

Full node envelope + top-level addenda (Q1: merge history is surfaced exactly
once, here, never inside `attrs`) + capped edge summaries both directions.
Unreadable or unknown id -> the `missing` list, never an error (06: "existence
is information" -- nodes' own RLS policy makes an unreadable row simply not
come back from the SELECT, so "not found" and "not readable" collapse for
free, the same way write()'s NotFoundError collapse works elsewhere).
"""
from engraphy.server.db import transaction

_MAX_IDS = 25       # design/03's tool table: get(ids <= 25)
_MAX_EDGES = 10      # 07: edges.out/in each capped at 10


async def get(pool, space_id: str, principal: str, ids: list[str]) -> dict:
    """RLS-filtered lookup of each id in `ids` (silently clamped to the first
    25 -- the same boring over-limit handling search() uses for `limit`).
    Returns the 07 envelope: `nodes` (found, in no particular guaranteed
    order beyond the query's own) and `missing` (every id that came back
    empty, whether unknown or simply unreadable by this principal)."""
    ids = ids[:_MAX_IDS]

    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()
        nodes = []
        found_ids = set()
        for node_id in ids:
            await cur.execute(
                "SELECT id, type, scope_id, title, body, attrs, status, author_principal, "
                "created_at FROM nodes WHERE id = %s AND space_id = %s",
                (node_id, space_id),
            )
            row = await cur.fetchone()
            if row is None:
                continue
            found_ids.add(node_id)
            nid, ntype, scope, title, body, attrs, status, author, created_at = row
            addenda = attrs.get("addenda", [])
            wire_attrs = {k: v for k, v in attrs.items() if k != "addenda"}
            nodes.append({
                "id": str(nid),
                "type": ntype,
                "scope": scope,
                "title": title,
                "body": body,
                "attrs": wire_attrs,
                "status": status,
                "author": author,
                "created_at": created_at.isoformat(),
                "addenda": addenda,
                "edges": {
                    "out": await _edges_out(cur, space_id, nid),
                    "in": await _edges_in(cur, space_id, nid),
                },
            })

        missing = [i for i in ids if i not in found_ids]

    return {"v": 1, "nodes": nodes, "missing": missing}


async def _edges_out(cur, space_id, node_id) -> list[dict]:
    """Edges where node_id is src, readable-filtered by the join to nodes
    (edges' own RLS policy already requires both endpoints readable)."""
    await cur.execute(
        "SELECT e.src_id, e.dst_id, e.type FROM edges e JOIN nodes n ON n.id = e.dst_id "
        "WHERE e.space_id = %s AND e.src_id = %s ORDER BY e.dst_id, e.type LIMIT %s",
        (space_id, node_id, _MAX_EDGES),
    )
    return [{"src": str(s), "dst": str(d), "type": t} for s, d, t in await cur.fetchall()]


async def _edges_in(cur, space_id, node_id) -> list[dict]:
    """Edges where node_id is dst, readable-filtered symmetrically to _edges_out."""
    await cur.execute(
        "SELECT e.src_id, e.dst_id, e.type FROM edges e JOIN nodes n ON n.id = e.src_id "
        "WHERE e.space_id = %s AND e.dst_id = %s ORDER BY e.src_id, e.type LIMIT %s",
        (space_id, node_id, _MAX_EDGES),
    )
    return [{"src": str(s), "dst": str(d), "type": t} for s, d, t in await cur.fetchall()]
