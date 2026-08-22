"""Thin plumbing over the SQL visibility functions — NO LOGIC HERE by design.

engram_readable_scopes()/engram_writable_scopes() in the database are the single
authority (design/implementation/visibility-and-rls-plan.md). This module only
SELECTs them. Re-deriving visibility in Python is a design violation.

Callers are responsible for the GUC protocol (engraphy/server/db.py's transaction
wrapper sets engram.space_id / engram.principal before this ever runs) — this
module does not set, validate, or cache identity.
"""


def readable_scopes(cursor) -> set[str]:
    """The current session identity's readable scope ids."""
    cursor.execute("SELECT engram_readable_scopes()")
    return {row[0] for row in cursor.fetchall()}


def writable_scopes(cursor) -> set[str]:
    """The current session identity's writable scope ids."""
    cursor.execute("SELECT engram_writable_scopes()")
    return {row[0] for row in cursor.fetchall()}


def ambient_scope_set(cursor, explicit_scopes: set[str]) -> set[str]:
    """explicit_scopes ∪ {ambient scopes the current identity can read} — the
    query-time scope-set expansion design/06 defines for search/dedup/briefing
    (e.g. "search(scope=X) -> X ∪ P-readable ambient scopes"). Ambient status
    doesn't change readability; `scopes`' own RLS policy (space match AND id IN
    readable) already filters this SELECT to readable rows, so no readability
    re-derivation happens here either."""
    cursor.execute(
        "SELECT id FROM scopes WHERE space_id = current_setting('engram.space_id', true) "
        "AND ambient = true"
    )
    ambient_readable = {row[0] for row in cursor.fetchall()}
    return explicit_scopes | ambient_readable


async def readable_scopes_async(cursor) -> set[str]:
    """Async twin of readable_scopes, for async pipeline code."""
    await cursor.execute("SELECT engram_readable_scopes()")
    return {row[0] for row in await cursor.fetchall()}


async def writable_scopes_async(cursor) -> set[str]:
    """Async twin of writable_scopes, for async pipeline code."""
    await cursor.execute("SELECT engram_writable_scopes()")
    return {row[0] for row in await cursor.fetchall()}


async def ambient_scope_set_async(cursor, explicit_scopes: set[str]) -> set[str]:
    """Async twin of ambient_scope_set, for async pipeline code (dedup write path
    onward) running on psycopg AsyncCursor rather than the sync test cursors above."""
    await cursor.execute(
        "SELECT id FROM scopes WHERE space_id = current_setting('engram.space_id', true) "
        "AND ambient = true"
    )
    ambient_readable = {row[0] for row in await cursor.fetchall()}
    return explicit_scopes | ambient_readable
