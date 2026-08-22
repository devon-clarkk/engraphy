"""engraphy.server.auth -- token resolution, role gating, rate limiting (design/03,
error codes design/07).

Pure-logic tests (hashing, classification, RateLimiter, FailureTracker) need no DB
and use an injected clock. DB-backed tests (resolve_token, read_rate_limits) seed
api_tokens on the superuser connection and resolve on the app-role pool -- api_tokens
is instance-level (not RLS), so resolution runs on a plain connection with no GUCs.
"""

import pytest

from engraphy.server.auth import (
    AuthContext,
    FailureTracker,
    RateLimiter,
    ToolError,
    Unauthorized,
    classify_kind,
    hash_token,
    mint_token,
    read_rate_limits,
    require_write,
    resolve_token,
)

# ---- pure logic (no DB) ---------------------------------------------------


def test_hash_token_is_deterministic_sha256_hex():
    h = hash_token("secret-bearer")
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    assert h == hash_token("secret-bearer") and h != hash_token("other")


@pytest.mark.parametrize("tool,args,kind", [
    ("search", None, "read"), ("get", None, "read"), ("briefing", None, "read"),
    ("traverse", None, "read"), ("scope_list", None, "read"),
    ("write", None, "write"), ("link", None, "write"), ("update", None, "write"),
    ("supersede", None, "write"), ("resolve_duplicate", None, "write"),
    ("scope_create", None, "write"), ("admin_token_create", None, "write"),
    ("inbox_review", {"action": "list"}, "read"),
    ("inbox_review", {"action": "promote"}, "write"),
    ("inbox_review", {"action": "discard"}, "write"),
])
def test_classify_kind(tool, args, kind):
    assert classify_kind(tool, args) == kind


def test_require_write_blocks_readonly_on_writes_only():
    ro = AuthContext("t", "s", "p", "c", "readonly")
    rw = AuthContext("t", "s", "p", "c", "readwrite")
    require_write(ro, "search")                       # read: fine
    require_write(rw, "write")                        # readwrite: fine
    require_write(ro, "inbox_review", {"action": "list"})
    for tool, args in [("write", None), ("inbox_review", {"action": "promote"})]:
        with pytest.raises(ToolError) as exc:
            require_write(ro, tool, args)
        assert exc.value.code == "ROLE"
        assert str(exc.value).startswith("ENGRAPHY_ROLE: ")


def test_rate_limiter_windows_and_buckets():
    clock = {"t": 1000.0}
    rl = RateLimiter(now=lambda: clock["t"])
    for _ in range(30):
        rl.check("tok", "write")
    with pytest.raises(ToolError) as exc:
        rl.check("tok", "write")
    assert exc.value.code == "RATE_LIMITED" and exc.value.extra["retry_after_ms"] > 0
    for _ in range(60):                               # reads are a separate bucket
        rl.check("tok", "read")
    with pytest.raises(ToolError):
        rl.check("tok", "read")
    clock["t"] += 61.0                                # window passes -> forgiven
    rl.check("tok", "write")
    rl.check("tok", "read")


def test_rate_limiter_honors_custom_limits():
    rl = RateLimiter(now=lambda: 0.0)
    rl.check("x", "read", read_limit=1)
    with pytest.raises(ToolError) as exc:
        rl.check("x", "read", read_limit=1)
    assert exc.value.code == "RATE_LIMITED"


def test_failure_tracker_bans_per_source_and_expires():
    clock = {"t": 0.0}
    ft = FailureTracker(threshold=3, window_seconds=10.0, now=lambda: clock["t"])
    assert not ft.is_banned("1.2.3.4")
    for _ in range(3):
        ft.record_failure("1.2.3.4")
    assert ft.is_banned("1.2.3.4")
    assert not ft.is_banned("5.6.7.8")                # per-source
    clock["t"] += 11.0
    assert not ft.is_banned("1.2.3.4")                # window passed


# ---- DB-backed (needs live Postgres; api_tokens is not RLS-covered) --------


def _bootstrap_token_space(conn, space_id):
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'Auth Space')", (space_id,))
    cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p1', 'P1')",
                (space_id,))
    conn.commit()


def _insert_token(conn, space_id, raw, *, principal="p1", client="dev-laptop",
                  role="readwrite", revoked=False, no_scope_all=False):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO api_tokens "
        "(space_id, principal, client_name, token_hash, role, revoked, no_scope_all) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (space_id, principal, client, hash_token(raw), role, revoked, no_scope_all),
    )
    tid = cur.fetchone()[0]
    conn.commit()
    return str(tid)


@pytest.fixture
def token_space(conn, request):
    space_id = ("au-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap_token_space(conn, space_id)
    yield space_id
    cur = conn.cursor()
    cur.execute("DELETE FROM api_tokens WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM config WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM principals WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


async def test_resolve_token_returns_context(pool, token_space, conn):
    tid = _insert_token(conn, token_space, "raw-secret-A", role="readonly")
    async with pool.connection() as c:
        ctx = await resolve_token(c, "raw-secret-A")
    assert ctx.token_id == tid
    assert ctx.space_id == token_space
    assert (ctx.principal, ctx.client_name, ctx.role) == ("p1", "dev-laptop", "readonly")
    assert ctx.no_scope_all is False   # the stored default, not the dataclass's


async def test_resolve_token_carries_the_scope_restriction(pool, token_space, conn):
    """LIVE (needs migration 0023). The link this whole change hangs on: a flag
    stored on the row has to arrive on the AuthContext, because that context is
    the entire identity the tool layer ever sees. A restriction that does not
    survive resolve_token is a column nothing reads."""
    _insert_token(conn, token_space, "raw-secret-R", client="scheduler", no_scope_all=True)
    async with pool.connection() as c:
        ctx = await resolve_token(c, "raw-secret-R")
    assert ctx.no_scope_all is True


async def test_mint_token_round_trips_the_scope_restriction(pool, token_space):
    """LIVE (needs migration 0023). Mint through the ONE mint path, resolve back
    through the one resolve path, and check both ends agree -- including that the
    default mint is still unrestricted, so nothing minted by an un-updated caller
    silently acquires a restriction."""
    async with pool.connection() as c:
        raw_r, meta_r = await mint_token(c, token_space, "p1", "scheduler", "readwrite",
                                         no_scope_all=True)
        raw_u, meta_u = await mint_token(c, token_space, "p1", "workstation", "readwrite")
        assert meta_r["no_scope_all"] is True and meta_u["no_scope_all"] is False
        assert (await resolve_token(c, raw_r)).no_scope_all is True
        assert (await resolve_token(c, raw_u)).no_scope_all is False


async def test_resolve_token_stamps_last_used_at(pool, token_space, conn):
    _insert_token(conn, token_space, "raw-secret-B")
    async with pool.connection() as c:
        await resolve_token(c, "raw-secret-B")
    cur = conn.cursor()
    cur.execute("SELECT last_used_at FROM api_tokens WHERE space_id = %s", (token_space,))
    assert cur.fetchone()[0] is not None


async def test_resolve_token_unknown_is_unauthorized(pool, token_space, conn):
    _insert_token(conn, token_space, "raw-secret-C")
    async with pool.connection() as c:
        with pytest.raises(Unauthorized):
            await resolve_token(c, "not-a-real-token")


async def test_resolve_token_revoked_is_unauthorized(pool, token_space, conn):
    _insert_token(conn, token_space, "raw-secret-D", revoked=True)
    async with pool.connection() as c:
        with pytest.raises(Unauthorized):
            await resolve_token(c, "raw-secret-D")


async def test_resolve_token_empty_is_unauthorized(pool):
    async with pool.connection() as c:
        with pytest.raises(Unauthorized):
            await resolve_token(c, "")


async def test_read_rate_limits_defaults_and_overrides(pool, token_space, conn):
    async with pool.connection() as c:
        assert await read_rate_limits(c, token_space) == (60, 30)
    cur = conn.cursor()
    cur.execute("INSERT INTO config (space_id, key, value) VALUES "
                "(%s, 'rate.read_per_min', %s), (%s, 'rate.write_per_min', %s)",
                (token_space, "120", token_space, "45"))
    conn.commit()
    async with pool.connection() as c:
        assert await read_rate_limits(c, token_space) == (120, 45)
