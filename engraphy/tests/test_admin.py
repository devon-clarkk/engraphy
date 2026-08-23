"""engraphy.server.tools.admin — the four space-admin MCP tool dispatchers, plus
tool_registry's per-space registration gating (space_admin_tools).

Covers, on a live RLS pool (engraphy_app role, NOBYPASSRLS): the space_admin gate
(admin passes, plain member -> ENGRAPHY_ROLE), each tool's happy path and error
translation, the display-once token property (plaintext returned exactly once,
only its SHA-256 at rest, never in the audit trail), the migration-0016 RLS
backstop (a member's own transaction cannot write the admin tables even with the
app gate removed from the picture), and the config switch that makes the four
tools vanish from tools/list entirely.
"""
import pytest

from engraphy.server import tool_registry
from engraphy.server.auth import AuthContext, ToolError, hash_token
from engraphy.server.db import transaction
from engraphy.server.tools.admin import (
    admin_grant,
    admin_member_add,
    admin_scope_visibility,
    admin_token_create,
)


def _bootstrap_admin_space(conn, space_id):
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'Admin Space')", (space_id,))
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name, role) VALUES "
        "(%s, 'admin1', 'Admin One', 'space_admin'), (%s, 'member1', 'Member One', 'member'), "
        "(%s, 'member2', 'Member Two', 'member')",
        (space_id, space_id, space_id),
    )
    # scope1 owned by member1 (NOT the admin) and private -- lets the visibility
    # test prove a space_admin can retarget a scope it does not own / cannot read.
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, 'scope1', 'Scope One', 'member1', 'private')",
        (space_id,),
    )
    conn.commit()


def _cleanup_admin_space(conn, space_id):
    cur = conn.cursor()
    for table in ("audit_log", "api_tokens", "scope_grants", "scopes", "config", "principals"):
        cur.execute(f"DELETE FROM {table} WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


@pytest.fixture
def admin_space(conn, request):
    space_id = ("ad-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap_admin_space(conn, space_id)
    yield space_id
    _cleanup_admin_space(conn, space_id)


def _ctx(space_id, principal="admin1", role="readwrite"):
    return AuthContext("t1", space_id, principal, "pytest-client", role)


# ---- admin_member_add -------------------------------------------------------

async def test_member_add_happy_path(pool, admin_space):
    result = await admin_member_add(pool, _ctx(admin_space), {"id": "newbie", "display_name": "New B"})
    assert result["v"] == 1
    assert result["principal"]["id"] == "newbie"
    assert result["principal"]["role"] == "member"  # default
    assert result["principal"]["archived"] is False


async def test_member_add_can_mint_another_admin(pool, admin_space):
    result = await admin_member_add(
        pool, _ctx(admin_space), {"id": "admin2", "display_name": "Admin Two", "role": "space_admin"},
    )
    assert result["principal"]["role"] == "space_admin"


async def test_member_add_duplicate_is_validation(pool, admin_space):
    with pytest.raises(ToolError) as exc:
        await admin_member_add(pool, _ctx(admin_space), {"id": "member1", "display_name": "Dup"})
    assert exc.value.code == "VALIDATION"


async def test_member_add_by_non_admin_is_role(pool, admin_space):
    with pytest.raises(ToolError) as exc:
        await admin_member_add(pool, _ctx(admin_space, principal="member1"),
                               {"id": "x", "display_name": "X"})
    assert exc.value.code == "ROLE"


async def test_member_add_bad_id_is_validation(pool, admin_space):
    with pytest.raises(ToolError) as exc:
        await admin_member_add(pool, _ctx(admin_space), {"id": "Bad Id!", "display_name": "X"})
    assert exc.value.code == "VALIDATION"


# ---- admin_token_create (security-critical) --------------------------------

async def test_token_create_returns_plaintext_once_and_stores_only_hash(pool, admin_space, conn):
    result = await admin_token_create(
        pool, _ctx(admin_space),
        {"principal": "member1", "client_name": "laptop", "role": "readonly"},
    )
    assert result["v"] == 1
    raw = result["token"]
    assert isinstance(raw, str) and len(raw) >= 40  # 256-bit url-safe
    assert (result["principal"], result["client_name"], result["role"]) == (
        "member1", "laptop", "readonly")

    cur = conn.cursor()
    # Only the SHA-256 is at rest; the plaintext appears in NO column.
    cur.execute("SELECT token_hash FROM api_tokens WHERE space_id = %s AND principal = 'member1'",
                (admin_space,))
    (stored_hash,) = cur.fetchone()
    assert stored_hash == hash_token(raw)
    assert stored_hash != raw

    # The audit row records the mint but NEVER the token or its hash.
    cur.execute("SELECT action, detail::text FROM audit_log WHERE space_id = %s", (admin_space,))
    action, detail_text = cur.fetchone()
    assert action == "admin_token_create"
    assert raw not in detail_text and stored_hash not in detail_text


async def test_token_create_is_resolvable_as_a_real_bearer(pool, admin_space):
    from engraphy.server.auth import resolve_token
    result = await admin_token_create(
        pool, _ctx(admin_space),
        {"principal": "member1", "client_name": "phone", "role": "readwrite"},
    )
    async with pool.connection() as c:
        resolved = await resolve_token(c, result["token"])
    assert (resolved.space_id, resolved.principal, resolved.role) == (admin_space, "member1", "readwrite")


async def test_token_create_unknown_principal_is_validation(pool, admin_space):
    with pytest.raises(ToolError) as exc:
        await admin_token_create(pool, _ctx(admin_space),
                                 {"principal": "ghost", "client_name": "c", "role": "readonly"})
    assert exc.value.code == "VALIDATION"


async def test_token_create_duplicate_client_is_validation(pool, admin_space):
    args = {"principal": "member1", "client_name": "dup", "role": "readonly"}
    await admin_token_create(pool, _ctx(admin_space), args)
    with pytest.raises(ToolError) as exc:
        await admin_token_create(pool, _ctx(admin_space), args)
    assert exc.value.code == "VALIDATION"


async def test_token_create_bad_role_is_validation(pool, admin_space):
    with pytest.raises(ToolError) as exc:
        await admin_token_create(pool, _ctx(admin_space),
                                 {"principal": "member1", "client_name": "c", "role": "superuser"})
    assert exc.value.code == "VALIDATION"


async def test_token_create_by_non_admin_is_role(pool, admin_space):
    with pytest.raises(ToolError) as exc:
        await admin_token_create(pool, _ctx(admin_space, principal="member1"),
                                 {"principal": "member1", "client_name": "c", "role": "readonly"})
    assert exc.value.code == "ROLE"


# ---- admin_scope_visibility -------------------------------------------------

async def test_scope_visibility_happy_path_on_unowned_scope(pool, admin_space, conn):
    # scope1 is owned by member1 and private -- admin1 does not own it, but per
    # decision (b) 0016's scopes_admin_read lets a space_admin SELECT any scope's
    # metadata, so the UPDATE ... RETURNING both targets and reads it back.
    result = await admin_scope_visibility(pool, _ctx(admin_space),
                                          {"scope_id": "scope1", "visibility": "team-read"})
    assert result["scope"]["id"] == "scope1"
    assert result["scope"]["visibility"] == "team-read"
    cur = conn.cursor()
    cur.execute("SELECT visibility FROM scopes WHERE space_id = %s AND id = 'scope1'", (admin_space,))
    assert cur.fetchone()[0] == "team-read"


async def test_scope_visibility_unknown_scope_is_not_found(pool, admin_space):
    with pytest.raises(ToolError) as exc:
        await admin_scope_visibility(pool, _ctx(admin_space),
                                     {"scope_id": "nope", "visibility": "team-read"})
    assert exc.value.code == "NOT_FOUND"


async def test_scope_visibility_bad_value_is_validation(pool, admin_space):
    with pytest.raises(ToolError) as exc:
        await admin_scope_visibility(pool, _ctx(admin_space),
                                     {"scope_id": "scope1", "visibility": "public"})
    assert exc.value.code == "VALIDATION"


async def test_scope_visibility_by_non_admin_is_role(pool, admin_space):
    with pytest.raises(ToolError) as exc:
        await admin_scope_visibility(pool, _ctx(admin_space, principal="member1"),
                                     {"scope_id": "scope1", "visibility": "team-read"})
    assert exc.value.code == "ROLE"


# ---- admin_grant ------------------------------------------------------------

async def test_grant_happy_path(pool, admin_space, conn):
    result = await admin_grant(pool, _ctx(admin_space),
                               {"scope_id": "scope1", "principal": "admin1", "level": "read"})
    assert result["grant"] == {"scope_id": "scope1", "principal": "admin1", "level": "read"}
    cur = conn.cursor()
    cur.execute("SELECT level FROM scope_grants WHERE space_id = %s AND scope_id = 'scope1' "
                "AND principal = 'admin1'", (admin_space,))
    assert cur.fetchone()[0] == "read"


async def test_grant_duplicate_is_validation(pool, admin_space):
    args = {"scope_id": "scope1", "principal": "admin1", "level": "read"}
    await admin_grant(pool, _ctx(admin_space), args)
    with pytest.raises(ToolError) as exc:
        await admin_grant(pool, _ctx(admin_space), args)
    assert exc.value.code == "VALIDATION"


async def test_grant_unknown_principal_is_validation(pool, admin_space):
    with pytest.raises(ToolError) as exc:
        await admin_grant(pool, _ctx(admin_space),
                          {"scope_id": "scope1", "principal": "ghost", "level": "read"})
    assert exc.value.code == "VALIDATION"


async def test_grant_bad_level_is_validation(pool, admin_space):
    with pytest.raises(ToolError) as exc:
        await admin_grant(pool, _ctx(admin_space),
                          {"scope_id": "scope1", "principal": "admin1", "level": "admin"})
    assert exc.value.code == "VALIDATION"


async def test_grant_by_non_admin_is_role(pool, admin_space):
    with pytest.raises(ToolError) as exc:
        await admin_grant(pool, _ctx(admin_space, principal="member1"),
                          {"scope_id": "scope1", "principal": "member1", "level": "write"})
    assert exc.value.code == "ROLE"


# ---- RLS backstop (migration 0016) ------------------------------------------

async def test_rls_denies_member_writing_principals_directly(pool, admin_space):
    """Even bypassing the app gate, a non-space-admin's own RLS transaction
    cannot INSERT a principal -- 0016's principals_write requires the caller be
    a space_admin. This is the crown-jewel backstop under the app gate."""
    import psycopg
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with transaction(pool, admin_space, "member1") as c:
            await c.execute(
                "INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'sneak', 'S')",
                (admin_space,),
            )


async def test_rls_allows_space_admin_writing_principals_directly(pool, admin_space):
    async with transaction(pool, admin_space, "admin1") as c:
        await c.execute(
            "INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'legit', 'L')",
            (admin_space,),
        )
    # committed on context exit; visible to a fresh superuser read is covered by
    # the happy-path tests -- here we only assert the INSERT was permitted.


# ---- tool_registry registration gating (space_admin_tools) ------------------

async def test_admin_tools_listed_by_default(pool, admin_space):
    names = {e["name"] for e in await tool_registry.list_tools_for_space(pool, admin_space)}
    assert {"admin_member_add", "admin_token_create", "admin_scope_visibility", "admin_grant"} <= names


async def test_admin_tools_absent_when_flag_false(pool, admin_space, conn):
    cur = conn.cursor()
    cur.execute("INSERT INTO config (space_id, key, value) VALUES (%s, 'space_admin_tools', 'false')",
                (admin_space,))
    conn.commit()

    names = {e["name"] for e in await tool_registry.list_tools_for_space(pool, admin_space)}
    assert not (names & {"admin_member_add", "admin_token_create", "admin_scope_visibility", "admin_grant"})
    # ...and unresolvable, indistinguishable from a nonexistent tool.
    assert await tool_registry.resolve_dispatch(pool, admin_space, "admin_token_create", {}) is None
    # core tools still resolve.
    assert await tool_registry.resolve_dispatch(pool, admin_space, "search", {}) is not None


async def test_admin_tool_resolves_when_enabled(pool, admin_space):
    resolved = await tool_registry.resolve_dispatch(pool, admin_space, "admin_grant", {})
    assert resolved is not None
    assert resolved[0] == "admin_grant"


# ---- decision (b): scopes_admin_read widening + its bound --------------------

async def test_space_admin_scope_list_sees_unowned_private_scope(pool, admin_space):
    """(b): a space_admin reads scope METADATA across the space -- scope1 is
    member1's private scope, yet appears in admin1's scope_list."""
    from engraphy.core.scopes import scope_list as core_scope_list
    result = await core_scope_list(pool, admin_space, "admin1")
    ids = {s["id"] for s in result["scopes"]}
    assert "scope1" in ids


async def test_member_scope_list_excludes_others_private_scope(pool, admin_space):
    """The bound on (b): the widening is space_admin-only. member2 (a plain
    member, not the owner) never sees member1's private scope1 -- existence is
    information stays intact for non-admins."""
    from engraphy.core.scopes import scope_list as core_scope_list
    result = await core_scope_list(pool, admin_space, "member2")
    ids = {s["id"] for s in result["scopes"]}
    assert "scope1" not in ids
