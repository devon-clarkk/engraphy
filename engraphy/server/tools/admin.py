"""Space-admin tools (design/06 §Space administration, E2-plan.md §5.5,
DECISIONS-DELTA 2026-07-19): admin_member_add, admin_token_create
(display-once), admin_scope_visibility, admin_grant.

Each is a dispatcher of the same (pool, ctx, arguments) -> 07-envelope shape as
every other tool. Two gates apply, in this order:

  1. Token-role gate (app.py, before dispatch): admin_* are in auth.WRITE_TOOLS,
     so a readonly token is refused with ENGRAPHY_ROLE before we run at all.
  2. Space-admin gate (here, _assert_space_admin): a readwrite token whose
     principal is a plain member is refused with ENGRAPHY_ROLE. This is the
     primary space-admin check; migration 0016's RLS policies are the backstop
     (space-match AND caller is space_admin), so even if this gate were bypassed
     the write itself still fails closed.

The whole "not registered at all when space_admin_tools=false" behavior lives in
tool_registry, not here -- when disabled, these dispatchers are simply never
reached (absent from tools/list and from resolve_dispatch).

Every mutating call writes exactly one audit_log row (design/03 §Audit). For
admin_token_create that row records the mint metadata ONLY -- never the token or
its hash (auth.mint_token is the sole place the plaintext exists, and it returns
it exactly once to the caller's envelope).
"""
import psycopg
from psycopg.types.json import Jsonb

from engraphy.core.scopes import _SCOPE_COLS, _scope_row
from engraphy.server.auth import ToolError, mint_token
from engraphy.server.db import transaction
from engraphy.server.tools.errors import to_tool_error

_PRINCIPAL_COLS = "id, display_name, role, archived, created_at"


def _principal_row(row) -> dict:
    pid, display_name, role, archived, created_at = row
    return {
        "id": pid, "display_name": display_name, "role": role,
        "archived": archived, "created_at": created_at.isoformat(),
    }


async def _assert_space_admin(conn, ctx) -> None:
    """Raise ENGRAPHY_ROLE unless the acting principal is a space_admin. Reads
    principals under RLS (principals_read is a plain space-match SELECT policy,
    so the caller's own row is always visible). This is the exact condition
    migration 0016's write policies also encode -- app gate and RLS backstop
    agree by construction."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT role FROM principals WHERE space_id = %s AND id = %s",
        (ctx.space_id, ctx.principal),
    )
    row = await cur.fetchone()
    if row is None or row[0] != "space_admin":
        raise ToolError("ROLE", "this principal is not a space_admin")


async def _audit(conn, ctx, action: str, detail: dict) -> None:
    """One audit_log row per mutating admin call (design/03 §Audit). audit_log
    is instance-level (not RLS-covered); the ungated INSERT grant covers it.
    `detail` must never carry a token or token hash."""
    cur = conn.cursor()
    await cur.execute(
        "INSERT INTO audit_log (space_id, principal, client_name, action, detail) "
        "VALUES (%s, %s, %s, %s, %s)",
        (ctx.space_id, ctx.principal, ctx.client_name, action, Jsonb(detail)),
    )


async def admin_member_add(pool, ctx, arguments: dict) -> dict:
    """{id, display_name, role?} -> {"v": 1, "principal": {created row}}.

    Creates a principal only (Devon-approved 3a): the personal-scope
    auto-creation design/06 describes at member-creation is DEFERRED to the CLI
    `space create` path, so this matches §5.5's pinned envelope exactly. `role`
    defaults to 'member'; a 'space_admin' value is legal (a space_admin may mint
    another). Duplicate id / bad id-or-role -> ENGRAPHY_VALIDATION."""
    try:
        member_id = arguments["id"]
        display_name = arguments["display_name"]
        role = arguments.get("role", "member")
        async with transaction(pool, ctx.space_id, ctx.principal) as conn:
            await _assert_space_admin(conn, ctx)
            cur = conn.cursor()
            try:
                await cur.execute(
                    "INSERT INTO principals (space_id, id, display_name, role) "
                    f"VALUES (%s, %s, %s, %s) RETURNING {_PRINCIPAL_COLS}",
                    (ctx.space_id, member_id, display_name, role),
                )
                row = await cur.fetchone()
            except psycopg.errors.UniqueViolation as exc:
                raise ToolError("VALIDATION", f"principal '{member_id}' already exists") from exc
            await _audit(conn, ctx, "admin_member_add",
                         {"principal": member_id, "display_name": display_name, "role": role})
        return {"v": 1, "principal": _principal_row(row)}
    except ToolError:
        raise
    except Exception as exc:
        raise to_tool_error(exc) from exc


async def admin_token_create(pool, ctx, arguments: dict) -> dict:
    """{principal, client_name, role, no_scope_all?} -> {"v": 1, "token":
    "<plaintext>", "principal", "client_name", "role", "no_scope_all",
    "created_at"}.

    SECURITY-CRITICAL display-once mint. The plaintext bearer exists in exactly
    one place ever -- this envelope's "token" field. auth.mint_token generates
    it, stores only its SHA-256 in api_tokens.token_hash, and returns it once;
    the audit row and every other surface see only mint metadata. There is no
    retrieve-later path (a lost token is revoked + re-minted). `role` is the
    TOKEN role ('readwrite'|'readonly'), distinct from the principal role.

    `no_scope_all` (optional, default false) is the second per-token capability
    (migration 0023): a token minted with it may not use scope='all' on search
    or briefing and is refused with ENGRAPHY_SCOPE. It is for credentials handed to
    unattended sessions -- read whatever scope you name, never every scope at
    once. Mint-time only, like `role`: narrowing an existing token in place would
    change what a running client can do with no mint record, so a change of mind
    is a revoke plus a re-mint. It goes in the audit detail (a capability of the
    credential, not a secret) but never near the plaintext."""
    try:
        target = arguments["principal"]
        client_name = arguments["client_name"]
        role = arguments["role"]
        no_scope_all = bool(arguments.get("no_scope_all", False))
        async with transaction(pool, ctx.space_id, ctx.principal) as conn:
            await _assert_space_admin(conn, ctx)
            try:
                raw, metadata = await mint_token(
                    conn, ctx.space_id, target, client_name, role,
                    no_scope_all=no_scope_all,
                )
            except psycopg.errors.UniqueViolation as exc:
                raise ToolError(
                    "VALIDATION",
                    f"a token for principal '{target}' client '{client_name}' already exists",
                ) from exc
            except psycopg.errors.ForeignKeyViolation as exc:
                raise ToolError("VALIDATION", f"principal '{target}' does not exist") from exc
            # detail carries mint metadata ONLY -- never `raw` or its hash.
            await _audit(conn, ctx, "admin_token_create",
                         {"principal": target, "client_name": client_name, "role": role,
                          "no_scope_all": no_scope_all})
        return {"v": 1, "token": raw, **metadata}
    except ToolError:
        raise
    except Exception as exc:
        raise to_tool_error(exc) from exc


async def admin_scope_visibility(pool, ctx, arguments: dict) -> dict:
    """{scope_id, visibility} -> {"v": 1, "scope": {scope_list's field shape}}.

    A space_admin may retarget ANY scope in the space (0016's scopes_admin_update
    USING is space-match AND space_admin, independent of readability), so the
    UPDATE uses RETURNING -- a separate read-back SELECT would fail for a private
    scope the admin cannot otherwise read. Unknown scope -> ENGRAPHY_NOT_FOUND;
    bad visibility value -> ENGRAPHY_VALIDATION."""
    try:
        scope_id = arguments["scope_id"]
        visibility = arguments["visibility"]
        async with transaction(pool, ctx.space_id, ctx.principal) as conn:
            await _assert_space_admin(conn, ctx)
            cur = conn.cursor()
            await cur.execute(
                f"UPDATE scopes SET visibility = %s WHERE space_id = %s AND id = %s "
                f"RETURNING {_SCOPE_COLS}",
                (visibility, ctx.space_id, scope_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise ToolError("NOT_FOUND", f"scope '{scope_id}' does not exist")
            await _audit(conn, ctx, "admin_scope_visibility",
                         {"scope_id": scope_id, "visibility": visibility})
        return {"v": 1, "scope": _scope_row(row)}
    except ToolError:
        raise
    except Exception as exc:
        raise to_tool_error(exc) from exc


async def admin_grant(pool, ctx, arguments: dict) -> dict:
    """{scope_id, principal, level} -> {"v": 1, "grant": {scope_id, principal,
    level}}. A per-principal scope-access exception (design/06 §Visibility
    model). INSERT-only: a duplicate (scope, principal) grant ->
    ENGRAPHY_VALIDATION (re-grant-with-different-level upsert semantics aren't
    pinned by any contract; kept boring). Unknown scope or principal ->
    ENGRAPHY_VALIDATION (bad caller input); bad level -> ENGRAPHY_VALIDATION."""
    try:
        scope_id = arguments["scope_id"]
        target = arguments["principal"]
        level = arguments["level"]
        async with transaction(pool, ctx.space_id, ctx.principal) as conn:
            await _assert_space_admin(conn, ctx)
            cur = conn.cursor()
            try:
                await cur.execute(
                    "INSERT INTO scope_grants (space_id, scope_id, principal, level) "
                    "VALUES (%s, %s, %s, %s)",
                    (ctx.space_id, scope_id, target, level),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise ToolError(
                    "VALIDATION",
                    f"a grant for principal '{target}' on scope '{scope_id}' already exists",
                ) from exc
            except psycopg.errors.ForeignKeyViolation as exc:
                raise ToolError(
                    "VALIDATION",
                    f"scope '{scope_id}' or principal '{target}' does not exist",
                ) from exc
            await _audit(conn, ctx, "admin_grant",
                         {"scope_id": scope_id, "principal": target, "level": level})
        return {"v": 1, "grant": {"scope_id": scope_id, "principal": target, "level": level}}
    except ToolError:
        raise
    except Exception as exc:
        raise to_tool_error(exc) from exc
