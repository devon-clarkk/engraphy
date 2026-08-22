"""The inbox: dumb capture, triage listing, deliberate promotion, discard.
Normative: design/02 §The inbox, design/03 (inbox_review action list|promote|
discard; POST /inbox accepts {kind, payload, scope?}), design/01 (inbox table).

The engine layer only; E2's engraphy/server/tools/inbox.py wraps these as the
`inbox_review` MCP tool + the /inbox capture endpoint, the same way E2 wraps the
dedup/search/briefing/traverse core functions.

Design decisions (Devon, 2026-07-17, DECISIONS-DELTA.md):
- **Promotion is authoring, not forwarding (D1).** The reviewer supplies the
  write fields (type/scope/title/body/attrs/links); the captured `kind`/`payload`
  are OPAQUE metadata -- never parsed here, never the source of node_type. "Dumb
  capture != memory": structure is added at promote time.
- **RLS.** capture/promote/discard obey `space AND (scope_id IS NULL OR scope_id
  IN writable)`; only a scope-writer may promote/discard (the writable check is
  enforced at this layer *and* by migration 0014's inbox_update policy, two-layer).
  The NULL branch keeps an unscoped capture actionable by anyone in the space.
- **PENDING promote leaves the row pending (Q3a).** A terminal write
  (inserted/merged) flips the inbox row to 'promoted'; a PENDING outcome parks a
  pending_writes row and NO node -- so the inbox row stays 'pending' and nothing
  is lost. The reviewer completes it via resolve_duplicate; the row is then
  cleared by a discard or a re-promote (which now auto-merges into the written
  node). resolve_duplicate has no inbox awareness -- it never did.

Transaction seam: write() owns its own transaction (and runs resonance in a
second, post-commit one), so promote cannot wrap the node write and the inbox-row
flip in one atomic unit -- the flip is a separate statement after write() commits.
This is safe because a terminal outcome is self-healing: a replay of an already-
promoted item re-runs dedup, hits >= t_high, and auto-merges rather than
duplicating (DECISIONS-DELTA.md).
"""
from psycopg.types.json import Jsonb

from engraphy.core import embedding  # noqa: F401  (re-exported for tests that patch it)
from engraphy.core import metrics
from engraphy.core.dedup import (
    NotFoundError,
    ScopeUnknownError,
    ValidationError,
    embed_with_attr_surface,
    write,
)
from engraphy.core.visibility import writable_scopes_async
from engraphy.server.db import transaction


async def capture(pool, space_id, principal, kind, payload, scope_id=None) -> dict:
    """POST /inbox {kind, payload, scope?}: park one pending inbox row carrying
    the opaque capture verbatim. `payload` is the client's business (a raw tool
    failure, a chat excerpt, ...) and is stored as-is; it is not a node. RLS
    inbox_write gates the scope (NULL, or writable)."""
    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()
        await cur.execute(
            "INSERT INTO inbox (space_id, scope_id, kind, payload, status) "
            "VALUES (%s, %s, %s, %s, 'pending') RETURNING id, created_at",
            (space_id, scope_id, kind, Jsonb(payload)),
        )
        inbox_id, created_at = await cur.fetchone()
    return {
        "v": 1,
        "id": str(inbox_id),
        "kind": kind,
        "payload": payload,
        "scope": scope_id,
        "status": "pending",
        "created_at": created_at.isoformat(),
    }


async def list_pending(pool, space_id, principal, limit: int = 25, offset: int = 0) -> dict:
    """The triage queue: pending rows visible under the SELECT policy (space +
    NULL-or-readable scope). OLDEST first (created_at asc, id asc tiebreak) -- the
    triage/nag philosophy is about aged items, so the caller pages from the top of
    the backlog. `limit`/`offset` are caller-controlled (Devon, 2026-07-17): a
    naive agent gets the default 25 (honoring 02's "worst read <= 25", so it can
    never dump the whole queue), while a dashboard passes a larger explicit limit
    or pages with offset. No recall bump -- inbox rows are not nodes.

    E2-plan.md s.3's resolved wire envelope: {"v": 1, "items": [{"id", "kind",
    "payload", "scope", "status", "created_at"}], "truncated": bool}. `status`
    is always 'pending' today (the only filter this function runs), included
    for envelope completeness rather than filter flexibility -- no resolution
    exists for a caller-chosen status filter, so none is invented here."""
    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()
        await cur.execute(
            "SELECT id, scope_id, kind, payload, status, created_at FROM inbox "
            "WHERE space_id = %s AND status = 'pending' "
            "ORDER BY created_at ASC, id ASC LIMIT %s OFFSET %s",
            (space_id, limit + 1, offset),
        )
        rows = await cur.fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    return {
        "v": 1,
        "items": [
            {
                "id": str(i),
                "scope": scope_id,
                "kind": kind,
                "payload": payload,
                "status": status,
                "created_at": created_at.isoformat(),
            }
            for i, scope_id, kind, payload, status, created_at in rows
        ],
        "truncated": truncated,
    }


async def _load_actionable(cur, space_id, inbox_id, verb):
    """Shared promote/discard precondition: the row must exist and be readable
    (else NOT_FOUND, 07's existence-is-information collapse), its scope must be
    writable (else SCOPE_UNKNOWN -- the same collapse at scope level, mirroring
    supersede's writable-scope guard), and it must be 'pending' (only a pending
    item is actionable). Returns nothing; raises on any failure."""
    await cur.execute(
        "SELECT scope_id, status FROM inbox WHERE id = %s AND space_id = %s",
        (inbox_id, space_id),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"ENGRAPHY_NOT_FOUND: inbox item {inbox_id} not found")
    scope_id, status = row
    if scope_id is not None and scope_id not in await writable_scopes_async(cur):
        raise ScopeUnknownError(
            f"ENGRAPHY_SCOPE_UNKNOWN: inbox item {inbox_id} scope {scope_id} is not writable"
        )
    if status != "pending":
        raise ValidationError(
            f"ENGRAPHY_VALIDATION: inbox item {inbox_id} is not pending (status "
            f"'{status}'); only a pending item can be {verb}"
        )


async def promote(
    pool,
    space_id: str,
    principal: str,
    inbox_id: str,
    node_type: str,
    scope_id: str,
    title: str,
    body: str,
    attrs: dict,
    source_client: str,
    links: list[dict] | None = None,
    thresholds=None,
    resonance_floor: float | None = None,
    embedding_vector: list[float] | None = None,
    source_session: str | None = None,
    extra_search: str = "",
) -> dict:
    """Deliberate promotion: run the FULL dedup pipeline over the reviewer's
    authored fields (insert/merge/pending bands, resonance -- exactly write()).
    Returns write()'s envelope unchanged (07 gives inbox no envelope of its own).

    The captured kind/payload are NOT read here (D1). node_type/scope_id/title/
    body/attrs/links are the reviewer's, embedded and written like any write().

    embedding_vector: test-surface override, like write()'s own -- when None
    (production) the title+body is embedded here with the document prefix; tests
    pass a synthetic vector so band forcing (via `thresholds`) is model-free.
    """
    async with transaction(pool, space_id, principal) as conn:
        await _load_actionable(conn.cursor(), space_id, inbox_id, "promoted")

    if embedding_vector is None:
        # Production fallback: resolve the attr surface + embed searchable_text
        # (Phase C §2.4). A caller that supplied the vector supplies its paired
        # extra_search too (the tool layer does).
        embedding_vector, extra_search = await embed_with_attr_surface(
            pool, space_id, node_type, title, body, attrs)

    envelope = await write(
        pool, space_id, principal, node_type, scope_id, title, body, attrs,
        embedding_vector, source_client, thresholds=thresholds,
        resonance_floor=resonance_floor, links=links, source_session=source_session,
        extra_search=extra_search,
    )

    # Terminal outcome (inserted/merged) consumes the inbox row; a PENDING
    # outcome parks a pending_writes row and no node, so the inbox row stays
    # 'pending' (Q3a) -- nothing is lost and it still counts in the footer.
    if envelope["outcome"] != "needs_confirmation":
        async with transaction(pool, space_id, principal) as conn:
            await conn.cursor().execute(
                "UPDATE inbox SET status = 'promoted' WHERE id = %s AND space_id = %s",
                (inbox_id, space_id),
            )

    # Usage metric (design/03 §stats): count the promote ACTION. The write()
    # above already counted its own facts_stored/duplicates_prevented outcome;
    # this is the additional "an inbox item was promoted" signal. Best-effort,
    # own transaction -- reached only if write() returned (did not raise).
    await metrics.bump_safe(pool, space_id, principal, {metrics.PROMOTES: 1})
    return envelope


async def discard(pool, space_id: str, principal: str, inbox_id: str) -> dict:
    """Drop a captured item without promoting it: pending -> discarded, in one
    transaction. design/02's 30-day purge of discarded rows is an operations
    sweep (no sweeper exists in-repo yet -- deferred with the pending_writes
    nightly sweep, DECISIONS-DELTA.md).

    E2-plan.md s.3's resolved envelope: {"v": 1, "outcome": "discarded", "id":
    "…"} -- a status flip, never a delete (the 30-day purge is the nightly
    sweep's job, not this function's)."""
    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()
        await _load_actionable(cur, space_id, inbox_id, "discarded")
        await cur.execute(
            "UPDATE inbox SET status = 'discarded' WHERE id = %s AND space_id = %s",
            (inbox_id, space_id),
        )
    return {"v": 1, "outcome": "discarded", "id": str(inbox_id)}
