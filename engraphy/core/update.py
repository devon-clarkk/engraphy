"""update: pure content replacement, never dedup-banded. Normative:
QUESTIONS.md "update-reembed-semantics" (resolved 2026-07-18, Fable) + design/03's
one-line table entry ("Re-embeds on text change"). E2-plan.md s.4 is the full
build spec this module implements; see that document for the "why" behind
each rule below.

Never re-runs banding (`supersede` already owns the banded-edit job). Re-embeds
iff `title` or `body` is supplied and the resulting `title + "\n" + body`
differs from the stored value -- an attrs-only call or a byte-identical repeat
skips embedding entirely. `updated_at` bumps via the normal `nodes_touch`
trigger path (an update IS a content modification, unlike a recall bump).
`attrs.addenda` is preserved across any attrs replacement (Q1) and is a
reserved key on the caller's `attrs`, same as write().
"""
from psycopg.types.json import Jsonb

from engraphy.core import embedding
from engraphy.core.attr_spec import RESERVED_ATTR_KEYS, sanitize_attrs
from engraphy.core.attr_spec import searchable_keys as _searchable_keys
from engraphy.core.dedup import (
    ATTR_SURFACE_KEY,
    NotFoundError,
    ValidationError,
    _config_bool,
    _vector_literal,
)
from engraphy.server.db import transaction

_ENVELOPE_COLS = "id, type, scope_id, title, body, attrs, status, author_principal, created_at"


def _node_envelope(row) -> dict:
    """The write.node shape (Q1: attrs stripped of addenda)."""
    nid, ntype, scope, title, body, attrs, status, author, created_at = row
    wire_attrs = {k: v for k, v in attrs.items() if k != "addenda"}
    return {
        "id": str(nid), "type": ntype, "scope": scope, "title": title, "body": body,
        "attrs": wire_attrs, "status": status, "author": author,
        "created_at": created_at.isoformat(),
    }


async def update(
    pool,
    space_id: str,
    principal: str,
    node_id: str,
    title: str | None = None,
    body: str | None = None,
    attrs: dict | None = None,
    embed_document=embedding.embed_document,
) -> dict:
    """Replace title/body/attrs on an existing node. Any of the three may be
    omitted (None = unchanged). Returns {"v": 1, "outcome": "updated", "node":
    {...}} (07 gives update no shape; this mirrors write's inserted node).

    Unknown/unreadable id -> ENGRAPHY_NOT_FOUND (the nodes_update RLS policy --
    USING readable, WITH CHECK writable -- is the backstop). Caller-supplied
    attrs.addenda -> ENGRAPHY_VALIDATION, same reserved-key rule as write().
    """
    if attrs is not None:
        for key in sorted(RESERVED_ATTR_KEYS & set(attrs)):
            raise ValidationError(f"ENGRAPHY_VALIDATION: attrs.{key} is a reserved key")

    # Phase 1: read current values (needed to decide whether the SEARCHABLE TEXT
    # changed and to preserve stored addenda) -- read-only, its own transaction.
    # Phase C (§2.4): the re-embed rule is now "searchable text changed", where
    # searchable text = title + body + rendered searchable attrs. An attrs-only
    # change to a SEARCHABLE attr must re-embed (the amend-path hole in I4); a
    # change touching only non-searchable attrs still skips the model call.
    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()
        await cur.execute(
            "SELECT type, title, body, attrs, extra_search, status FROM nodes WHERE id = %s",
            (node_id,))
        row = await cur.fetchone()
        if row is None:
            raise NotFoundError(f"ENGRAPHY_NOT_FOUND: node {node_id} not found")
        node_type, cur_title, cur_body, cur_attrs, cur_extra, cur_status = row
        # An archived node is quarantined content (design/04): readable by id,
        # restored by an operator, and not rewritten through the agent surface,
        # the same way supersede refuses a non-active old_id. Both UPDATEs below
        # also carry `status <> 'archived'`, so a node archived between the two
        # phases is never rewritten either.
        if cur_status == "archived":
            raise ValidationError(
                f"ENGRAPHY_VALIDATION: node {node_id} is archived; an archived node is read-only")

        new_title = title if title is not None else cur_title
        new_body = body if body is not None else cur_body
        stored_addenda = cur_attrs.get("addenda")
        if attrs is not None:
            new_attrs = dict(attrs)
            if stored_addenda is not None:
                new_attrs["addenda"] = stored_addenda
        else:
            new_attrs = cur_attrs

        # Resolve the type's searchable keys + the space's flag (non-RLS reference
        # tables, read in this same transaction) and render the new surface.
        await cur.execute(
            "SELECT attr_spec FROM node_types WHERE space_id = %s AND name = %s",
            (space_id, node_type))
        srow = await cur.fetchone()
        spec = srow[0] if srow and srow[0] is not None else {}
        # Never destroy the amended node over a bad attr value: quarantine an
        # invalid attr into the `dropped` bucket, the same way the write path
        # does. An update() that sets occurred_on to an unparseable date keeps
        # the node. Sanitize runs on the merged attrs (the caller's plus any
        # preserved addenda); addenda and dropped are reserved and untouched.
        new_attrs, dropped_attrs = sanitize_attrs(spec, new_attrs)
        await cur.execute(
            "SELECT value FROM config WHERE space_id = %s AND key = %s",
            (space_id, ATTR_SURFACE_KEY))
        crow = await cur.fetchone()
        surface_on = _config_bool(crow[0] if crow else None, ATTR_SURFACE_KEY, True)
        keys = _searchable_keys(spec)
        new_extra = embedding.render_attr_surface(new_attrs, keys) if surface_on else ""

        new_searchable = embedding.searchable_text(new_title, new_body, new_extra)
        cur_searchable = embedding.searchable_text(cur_title, cur_body, cur_extra)
        need_reembed = new_searchable != cur_searchable

        if not need_reembed:
            # No slow call needed -- read and write in the SAME transaction,
            # atomic, no TOCTOU gap. extra_search is unchanged by construction
            # (searchable text unchanged => rendered surface unchanged), but set
            # it explicitly so the column always tracks new_attrs.
            await cur.execute(
                f"UPDATE nodes SET title = %s, body = %s, attrs = %s, extra_search = %s "
                f"WHERE id = %s AND status <> 'archived' RETURNING {_ENVELOPE_COLS}",
                (new_title, new_body, Jsonb(new_attrs), new_extra, node_id),
            )
            updated_row = await cur.fetchone()
            if updated_row is None:
                raise NotFoundError(f"ENGRAPHY_NOT_FOUND: node {node_id} not found")
            env = {"v": 1, "outcome": "updated", "node": _node_envelope(updated_row)}
            if dropped_attrs:
                env["dropped_attrs"] = dropped_attrs
            return env

    # Phase 2 (only when re-embedding): embed OUTSIDE any transaction (trap 3),
    # then a SECOND transaction to apply it. The searchable text -- title + body +
    # the rendered searchable surface -- is what is embedded and what is stored in
    # extra_search, so the embedding and the tsvector weight-C leg stay one source.
    embedding_vector = embed_document(new_searchable)

    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()
        await cur.execute(
            f"UPDATE nodes SET title = %s, body = %s, attrs = %s, extra_search = %s, "
            f"embedding = %s::vector, embedding_model = %s "
            f"WHERE id = %s AND status <> 'archived' RETURNING {_ENVELOPE_COLS}",
            (
                new_title, new_body, Jsonb(new_attrs), new_extra,
                _vector_literal(embedding_vector), embedding.MODEL_STAMP, node_id,
            ),
        )
        updated_row = await cur.fetchone()
        if updated_row is None:
            raise NotFoundError(f"ENGRAPHY_NOT_FOUND: node {node_id} not found")
        env = {"v": 1, "outcome": "updated", "node": _node_envelope(updated_row)}
        if dropped_attrs:
            env["dropped_attrs"] = dropped_attrs
        return env
