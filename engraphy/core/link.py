"""link: attach edges between two EXISTING nodes, outside any write. Sibling
to dedup.py's write-path `_attach_links` rather than a reuse of it -- that
helper's shape presumes ONE explicit endpoint (the other being the node
being written); `link` has no such implicit node, so both endpoints are
always explicit and required (QUESTIONS.md links-wire-shape, 2026-07-16,
Fable option c, extended to the standalone tool: "both required, unlike
write's links").

E2-plan.md s.3's link row: no dedicated wire fixture, no QUESTIONS.md entry
for the envelope shape -- `{"v": 1, "attached": N, "skipped": M}` is the
boring default (mirrors the attached/skipped counting write()'s own
_attach_links already does on MERGE). Error-class split, same as write's
links: pure shape here (VALIDATION) -> endpoint existence at attach time
(NOT_FOUND) -> type/rule at the DB (EDGE_RULE, via the edges_validate
trigger's CheckViolation, translated at the tool layer).
"""
from engraphy.core.dedup import NotFoundError, ValidationError
from engraphy.server.db import transaction


def _validate_edges_shape(edges) -> None:
    """Each item: {type, src_id, dst_id}, ALL required (unlike write's
    links, which name exactly one explicit endpoint). Pure and structural:
    run before the transaction opens, so a malformed request never takes any
    lock or touches the DB."""
    for edge in edges:
        if not isinstance(edge, dict) or not isinstance(edge.get("type"), str):
            raise ValidationError("ENGRAPHY_VALIDATION: each item requires a string 'type'")
        extra = set(edge) - {"type", "src_id", "dst_id"}
        if extra:
            raise ValidationError(
                f"ENGRAPHY_VALIDATION: item has unknown field(s) {sorted(extra)}; "
                f"allowed: type, src_id, dst_id"
            )
        if not isinstance(edge.get("src_id"), str) or not isinstance(edge.get("dst_id"), str):
            raise ValidationError(
                "ENGRAPHY_VALIDATION: each item requires both src_id and dst_id"
            )


async def link(pool, space_id: str, principal: str, edges: list[dict]) -> dict:
    """Attach each {type, src_id, dst_id} edge, rule-checked, and return
    {"v": 1, "attached": N, "skipped": M}. Endpoint readability is checked
    FIRST via an RLS-filtered SELECT (unknown or unreadable -> ENGRAPHY_NOT_FOUND,
    06's "existence is information" collapse -- the same pattern write's
    _attach_links uses), so the edges FK never surfaces its own "endpoint does
    not exist" in the wrong error class. ON CONFLICT DO NOTHING makes an
    already-present edge a skip, not an error; a (type, src, dst) with no
    edge_rules row still raises the edges_validate trigger's CheckViolation
    (translated to ENGRAPHY_EDGE_RULE at the tool layer)."""
    _validate_edges_shape(edges)
    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()
        attached = skipped = 0
        for edge in edges:
            src_id, dst_id, etype = edge["src_id"], edge["dst_id"], edge["type"]
            for peer in (src_id, dst_id):
                await cur.execute("SELECT 1 FROM nodes WHERE id = %s AND space_id = %s", (peer, space_id))
                if await cur.fetchone() is None:
                    raise NotFoundError(f"ENGRAPHY_NOT_FOUND: link endpoint {peer} not found")
            await cur.execute(
                "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (src_id, dst_id, type) DO NOTHING",
                (space_id, src_id, dst_id, etype),
            )
            if cur.rowcount == 1:
                attached += 1
            else:
                skipped += 1

    return {"v": 1, "attached": attached, "skipped": skipped}
