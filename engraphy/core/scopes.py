"""scope_list / scope_create / scope_guide -- E2-plan.md s.3/s.5.4's resolved
shapes. Grant enumeration (who else can see a scope) is deliberately excluded
from all: that's admin_grant's surface, not a plain list every readwrite token
can call.

`description` (added migration 0022) is the free-text "what this scope governs +
when to write here". It is MANDATORY on create -- enforced at the wire/tool layer
(wire_types marks it required; this module rejects an empty one), not by a
storage NOT NULL constraint (see the migration's rationale). `scope_guide` is the
read-only routing manifest built from it.
"""
import psycopg

from engraphy.core.dedup import ValidationError
from engraphy.server.db import transaction

_SCOPE_COLS = "id, display_name, description, visibility, ambient, hints, owner_principal, created_at"


def _scope_row(row) -> dict:
    sid, display_name, description, visibility, ambient, hints, owner_principal, created_at = row
    return {
        "id": sid, "display_name": display_name, "description": description,
        "visibility": visibility, "ambient": ambient, "hints": list(hints),
        "owner_principal": owner_principal, "created_at": created_at.isoformat(),
    }


async def scope_list(pool, space_id: str, principal: str) -> dict:
    """Row set is exactly `engraphy_readable_scopes()` -- no separate
    visibility logic here; the scopes table's own RLS SELECT policy
    (`id IN engraphy_readable_scopes()`) already excludes archived scopes and
    anything this principal cannot read. `description` is included (migration
    0022) alongside the existing fields."""
    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()
        await cur.execute(f"SELECT {_SCOPE_COLS} FROM scopes WHERE space_id = %s ORDER BY id", (space_id,))
        rows = await cur.fetchall()
    return {"v": 1, "scopes": [_scope_row(r) for r in rows]}


async def scope_guide(pool, space_id: str, principal: str) -> dict:
    """The routing manifest (read-only): one entry per scope this token can
    read, each with its description -- what it governs and when to write there --
    so an agent can decide where a new memory belongs before writing. RLS-scoped
    exactly like scope_list (the `scopes` SELECT policy restricts the row set to
    engraphy_readable_scopes(); nothing here filters cross-space, so isolation is
    the database's, not this query's). A focused subset of scope_list's row: id,
    display_name, description -- see the module/tool docs for the frozen shape."""
    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()
        await cur.execute(
            "SELECT id, display_name, description FROM scopes WHERE space_id = %s ORDER BY id",
            (space_id,),
        )
        rows = await cur.fetchall()
    return {
        "v": 1,
        "space": space_id,
        "scopes": [{"id": r[0], "display_name": r[1], "description": r[2]} for r in rows],
    }


async def scope_create(
    pool, space_id: str, principal: str, scope_id: str, display_name: str,
    description: str, confirm: bool,
) -> dict:
    """Creates a new scope, `visibility='private'`, `owner_principal=principal`
    (the creator owns what they create). `confirm` missing/false ->
    ENGRAPHY_VALIDATION (a deliberate speed bump: scope creation is rarer and
    more consequential than a plain write). A missing/empty `description` ->
    ENGRAPHY_VALIDATION: descriptions are mandatory so every scope carries routing
    text for scope_guide. (A missing key is already refused one layer up by
    wire_types.validate's required check; the empty-string refusal here is the
    load-bearing half, since a present "" passes the wire type check.) Duplicate
    id -> ENGRAPHY_VALIDATION naming the id -- this can reveal a name collision with
    an otherwise-unreadable scope, accepted as unavoidable given a unique key must
    fail somehow (E2-plan.md s.3, documented rather than papered over).

    Role gating (`readwrite` required) is the transport layer's job
    (auth.WRITE_TOOLS already lists 'scope_create'), not this function's."""
    if not confirm:
        raise ValidationError("ENGRAPHY_VALIDATION: scope_create requires confirm: true")
    if not description or not description.strip():
        raise ValidationError("ENGRAPHY_VALIDATION: scope_create requires a non-empty description")

    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()
        try:
            # No RETURNING: scopes_read's policy checks id IN
            # engraphy_readable_scopes(), a STABLE SECURITY DEFINER function
            # that queries `scopes` itself -- self-referential, so within
            # THIS statement it doesn't see the row THIS statement is still
            # inserting, and RETURNING's implicit SELECT-policy check fails
            # with a spurious InsufficientPrivilege. A separate SELECT as its
            # own statement (command counter advances) reads it back fine.
            await cur.execute(
                "INSERT INTO scopes (space_id, id, display_name, description, owner_principal, visibility) "
                "VALUES (%s, %s, %s, %s, %s, 'private')",
                (space_id, scope_id, display_name, description, principal),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise ValidationError(f"ENGRAPHY_VALIDATION: scope id '{scope_id}' already in use") from exc
        await cur.execute(
            f"SELECT {_SCOPE_COLS} FROM scopes WHERE space_id = %s AND id = %s",
            (space_id, scope_id),
        )
        row = await cur.fetchone()
    return {"v": 1, "scope": _scope_row(row)}
