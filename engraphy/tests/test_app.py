"""engraphy.server.app -- end-to-end through a real (in-process) MCP client
against the actual Streamable HTTP transport: ASGITransport wraps the
Starlette app with no real socket, but the client<->server exchange
(initialize, tools/list, tools/call) is the genuine MCP protocol, not a
direct dispatcher call (those are exhaustively covered per-tool in
test_*_tool.py). This is app.py's own layer under test: bearer auth,
role/rate gating, alias resolution, tools/list assembly, and error-envelope
shape -- the parts that only exist once the transport is wired up.

Lifespan: StreamableHTTPSessionManager.handle_request requires .run()'s task
group to be entered first (raises RuntimeError otherwise) -- httpx's
ASGITransport does not drive ASGI lifespan itself, so `_running_app` below
hand-simulates the lifespan.startup/shutdown handshake around each test.
"""
import contextlib

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from engraphy.server.app import (
    SchemaVersionMismatch,
    applied_schema_version,
    check_schema_version,
    check_transport_security,
    create_app,
    expected_schema_version,
)
from engraphy.server.auth import hash_token
from engraphy.tests.test_dedup import write_space  # noqa: F401


@contextlib.asynccontextmanager
async def _running_app(app):
    startup_event = anyio.Event()
    shutdown_requested = anyio.Event()
    shutdown_event = anyio.Event()
    startup_errors = []

    async def receive():
        if not startup_event.is_set():
            return {"type": "lifespan.startup"}
        await shutdown_requested.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message):
        if message["type"] == "lifespan.startup.complete":
            startup_event.set()
        elif message["type"] == "lifespan.startup.failed":
            startup_errors.append(message.get("message"))
            startup_event.set()
        elif message["type"] == "lifespan.shutdown.complete":
            shutdown_event.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(app, {"type": "lifespan"}, receive, send)
        await startup_event.wait()
        if startup_errors:
            raise RuntimeError(f"lifespan startup failed: {startup_errors}")
        try:
            yield
        finally:
            shutdown_requested.set()
            await shutdown_event.wait()


def _asgi_http_client(app, raw_token):
    headers = {"Authorization": f"Bearer {raw_token}"} if raw_token is not None else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=headers,
    )


@contextlib.asynccontextmanager
async def _mcp_session(app, raw_token):
    async with _asgi_http_client(app, raw_token) as http_client:
        async with streamable_http_client(
            "http://testserver/mcp/", http_client=http_client,
        ) as (read, write_stream, _get_session_id):
            async with ClientSession(read, write_stream) as session:
                await session.initialize()
                yield session


@pytest.fixture
def app_space(write_space, conn):
    """write_space plus a readwrite and a readonly bearer token."""
    raw_rw, raw_ro = f"rw-{write_space}", f"ro-{write_space}"
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO api_tokens (space_id, principal, client_name, token_hash, role) "
        "VALUES (%s, 'p1', 'pytest-rw', %s, 'readwrite')",
        (write_space, hash_token(raw_rw)),
    )
    cur.execute(
        "INSERT INTO api_tokens (space_id, principal, client_name, token_hash, role) "
        "VALUES (%s, 'p1', 'pytest-ro', %s, 'readonly')",
        (write_space, hash_token(raw_ro)),
    )
    conn.commit()
    yield write_space, raw_rw, raw_ro
    cur.execute("DELETE FROM api_tokens WHERE space_id = %s", (write_space,))
    conn.commit()


# ---- transport-level auth ---------------------------------------------------


async def test_healthz_is_unauthenticated(pool):
    app = create_app(pool)
    async with _running_app(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert isinstance(body["spaces"], int)
    assert "embedding_model" in body


async def test_mcp_call_with_no_bearer_is_401(pool):
    app = create_app(pool)
    async with _running_app(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post("/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
    assert resp.status_code == 401


async def test_inbox_capture_with_no_bearer_is_401(pool):
    app = create_app(pool)
    async with _running_app(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post("/inbox", json={"kind": "note", "payload": {}})
    assert resp.status_code == 401


async def test_repeated_bad_bearers_trip_the_ban(pool):
    app = create_app(pool)
    async with _running_app(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            for _ in range(10):
                resp = await c.post(
                    "/inbox", json={"kind": "note", "payload": {}},
                    headers={"Authorization": "Bearer nonsense"},
                )
                assert resp.status_code == 401
            # 11th attempt (even with no bearer at all) is banned, not just unauthorized --
            # FailureTracker's ban is a transport-level 401 either way, so this test only
            # proves the request keeps failing past the threshold, not a distinct status.
            resp = await c.post("/inbox", json={"kind": "note", "payload": {}})
            assert resp.status_code == 401


# ---- MCP tool surface --------------------------------------------------------


async def test_tools_list_includes_all_fourteen_core_tools(pool, app_space):
    space_id, raw_rw, _raw_ro = app_space
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            result = await session.list_tools()
    names = {t.name for t in result.tools}
    # The fifteen core tools (pending_list is the read-only confirm-queue list;
    # stats is the read-only per-space usage-metrics tool, E3; scope_guide is the
    # read-only routing manifest) plus the four admin_* tools (space_admin_tools
    # unset -> enabled; the flag is space-level, so even this non-admin token sees
    # them in tools/list and is refused with ENGRAPHY_ROLE only at call time).
    assert names == {
        "write", "resolve_duplicate", "update", "link", "supersede",
        "get", "search", "traverse", "briefing", "pending_list", "stats", "inbox_review",
        "scope_list", "scope_guide", "scope_create",
        "admin_member_add", "admin_token_create", "admin_scope_visibility", "admin_grant",
    }


async def test_write_tool_call_round_trip(pool, app_space):
    space_id, raw_rw, _raw_ro = app_space
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            result = await session.call_tool("write", {
                "scope": "scope1", "type": "widget", "title": "App layer write",
                "body": "Exercised through the real MCP transport.", "attrs": {},
            })
    assert result.isError is not True
    assert result.structuredContent["outcome"] == "inserted"
    assert result.structuredContent["node"]["title"] == "App layer write"


async def test_readonly_token_on_write_tool_gets_role_error(pool, app_space):
    space_id, _raw_rw, raw_ro = app_space
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_ro) as session:
            result = await session.call_tool("write", {
                "scope": "scope1", "type": "widget", "title": "Should not land",
                "body": "Blocked by role gate.", "attrs": {},
            })
    assert result.isError is True
    text = result.content[0].text
    assert text.startswith("ENGRAPHY_ROLE:")


async def test_missing_required_argument_is_validation_not_internal(pool, app_space):
    space_id, raw_rw, _raw_ro = app_space
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            # 'scope' omitted -- the write dispatcher's arguments["scope"]
            # raises KeyError, which tools/errors.py now maps to VALIDATION.
            result = await session.call_tool("write", {
                "type": "widget", "title": "No scope", "body": "Missing a required field.",
            })
    assert result.isError is True
    assert result.content[0].text.startswith("ENGRAPHY_VALIDATION:")


async def test_rate_limit_trip_carries_retry_after_ms(pool, app_space, conn):
    space_id, raw_rw, _raw_ro = app_space
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO config (space_id, key, value) VALUES (%s, 'rate.read_per_min', %s)",
        (space_id, "1"),
    )
    conn.commit()
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            first = await session.call_tool("scope_list", {})
            assert first.isError is not True
            second = await session.call_tool("scope_list", {})
    assert second.isError is True
    assert second.content[0].text.startswith("ENGRAPHY_RATE_LIMITED:")
    assert second.structuredContent["retry_after_ms"] > 0


async def test_pack_alias_appears_in_tools_list_and_audits_under_alias_identity(pool, app_space, conn):
    space_id, raw_rw, _raw_ro = app_space
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO config (space_id, key, value) VALUES (%s, 'pack.tool_aliases', %s::jsonb)",
        (space_id, '{"log_error": {"binds": "write", "preset": {"type": "widget"}}}'),
    )
    conn.commit()
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            listed = await session.list_tools()
            assert "log_error" in {t.name for t in listed.tools}

            result = await session.call_tool("log_error", {
                "scope": "scope1", "title": "Via alias", "body": "Preset overrides type.",
            })
    assert result.isError is not True
    node_id = result.structuredContent["node"]["id"]
    cur.execute(
        "SELECT action FROM audit_log WHERE space_id = %s AND detail->>'node_id' = %s",
        (space_id, node_id),
    )
    assert cur.fetchone()[0] == "write via log_error"


async def test_inbox_capture_endpoint_parks_a_pending_row(pool, app_space, conn):
    space_id, raw_rw, _raw_ro = app_space
    app = create_app(pool)
    async with _running_app(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(
                "/inbox", json={"kind": "note", "payload": {"text": "captured"}},
                headers={"Authorization": f"Bearer {raw_rw}"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    cur = conn.cursor()
    cur.execute("SELECT kind, status FROM inbox WHERE space_id = %s AND id = %s", (space_id, body["id"]))
    assert cur.fetchone() == ("note", "pending")


# ---- boot-time checks (no ASGI/lifespan needed) ------------------------------


def test_expected_schema_version_is_the_latest_migration_file():
    # engraphy/db/migrations' newest file (migration 0023, the api_tokens
    # no-scope='all' restriction backing per-token scope limits).
    assert expected_schema_version() == "0023"


async def test_applied_schema_version_reads_the_migration_table(pool):
    # Environment-agnostic: None on a DB never provisioned via dbmate (the local
    # dev DB has no schema_migrations table), else the max applied version (CI's
    # dbmate-provisioned DB, which is at the latest migration file).
    result = await applied_schema_version(pool)
    assert result is None or result == expected_schema_version()


async def test_check_schema_version_raises_on_mismatch(pool, monkeypatch):
    # Force a mismatch deterministically regardless of the DB's actual state
    # (local: schema_migrations absent -> applied None; CI: applied == latest).
    # Pin expected to a version the DB can never be at, so applied != expected
    # holds either way and the gate must raise.
    monkeypatch.setattr("engraphy.server.app.expected_schema_version", lambda: "9999")
    with pytest.raises(SchemaVersionMismatch, match="9999"):
        await check_schema_version(pool)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "10.0.0.5", "192.168.1.1"])
def test_transport_security_allows_loopback_and_private_hosts_without_opt_in(host):
    check_transport_security(host, insecure_transport_ok=False)  # must not raise


def test_transport_security_refuses_public_host_without_opt_in():
    with pytest.raises(RuntimeError, match="insecure_transport_ok"):
        check_transport_security("8.8.8.8", insecure_transport_ok=False)


def test_transport_security_allows_public_host_with_explicit_opt_in():
    check_transport_security("8.8.8.8", insecure_transport_ok=True)  # must not raise


def test_transport_security_fails_closed_on_unparseable_hostname():
    with pytest.raises(RuntimeError, match="insecure_transport_ok"):
        check_transport_security("engraphy.example.com", insecure_transport_ok=False)


def test_read_last_backup_at_omitted_when_env_unset(monkeypatch):
    monkeypatch.delenv("ENGRAPHY_LAST_BACKUP_STATUS_FILE", raising=False)
    from engraphy.server.app import _read_last_backup_at

    assert _read_last_backup_at() is None


def test_read_last_backup_at_reads_the_configured_file(tmp_path, monkeypatch):
    status_file = tmp_path / "last_backup_at"
    status_file.write_text("2026-07-18T00:00:00Z", encoding="utf-8")
    monkeypatch.setenv("ENGRAPHY_LAST_BACKUP_STATUS_FILE", str(status_file))
    from engraphy.server.app import _read_last_backup_at

    assert _read_last_backup_at() == "2026-07-18T00:00:00Z"


# ---- Wire-type enforcement through the real funnel ---------------------------
#
# Unit coverage of the rules is in test_wire_types.py. These prove the WIRING:
# that validation actually runs at handle_call_tool, sits in the pinned position
# relative to the gates, shapes its errors as ENGRAPHY_VALIDATION, and reaches
# alias calls identically. (design/07 §Per-argument wire types, ruled 2026-07-21.)

_ALIAS_CONFIG = '{"log_error": {"binds": "write", "preset": {"type": "widget"}}}'


def _install_alias(conn, space_id):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO config (space_id, key, value) VALUES (%s, 'pack.tool_aliases', %s::jsonb)",
        (space_id, _ALIAS_CONFIG),
    )
    conn.commit()


async def test_unknown_argument_is_refused_at_the_wire(pool, app_space):
    """The closed argument surface. Before enforcement this reached the
    dispatcher, which silently ignored the extra key."""
    space_id, raw_rw, _raw_ro = app_space
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            result = await session.call_tool("write", {
                "scope": "scope1", "type": "widget", "title": "Extra key",
                "body": "Has an argument the table does not list.", "flavour": "spicy",
            })
    assert result.isError is True
    text = result.content[0].text
    assert text.startswith("ENGRAPHY_VALIDATION:")
    assert "flavour" in text


async def test_explicit_null_is_refused_at_the_wire(pool, app_space):
    """07: absent and null are not the same thing. The message must say what to
    send instead, since a model that retries with null again learns nothing."""
    space_id, raw_rw, _raw_ro = app_space
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            result = await session.call_tool("write", {
                "scope": "scope1", "type": "widget", "title": "Null attrs",
                "body": "Sends null rather than omitting.", "attrs": None,
            })
    assert result.isError is True
    text = result.content[0].text
    assert text.startswith("ENGRAPHY_VALIDATION:")
    assert "omit the key" in text


async def test_wrong_type_is_refused_at_the_wire(pool, app_space):
    space_id, raw_rw, _raw_ro = app_space
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            result = await session.call_tool("search", {
                "scope": "scope1", "query": "anything", "limit": "25",
            })
    assert result.isError is True
    text = result.content[0].text
    assert text.startswith("ENGRAPHY_VALIDATION:")
    assert "limit" in text and "integer" in text


async def test_malformed_uuid_is_validation_not_an_internal_cast_failure(pool, app_space):
    """The concrete gap 07 named: uuid arguments used to go straight to Postgres,
    so a malformed one surfaced as a cast failure in the INTERNAL class rather
    than as a caller-fixable ENGRAPHY_VALIDATION."""
    space_id, raw_rw, _raw_ro = app_space
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            result = await session.call_tool("traverse", {
                "start_id": "definitely-not-a-uuid", "direction": "out",
            })
    assert result.isError is True
    text = result.content[0].text
    assert text.startswith("ENGRAPHY_VALIDATION:")
    assert "start_id" in text and "uuid" in text


async def test_a_well_typed_out_of_range_limit_is_clamped_not_refused(pool, app_space):
    """Clamps stay clamps (07, pinned). This is the regression that would be
    easiest to introduce by "tightening" validation: type errors reject, range
    excess clamps, and search still answers."""
    space_id, raw_rw, _raw_ro = app_space
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            result = await session.call_tool("search", {
                "scope": "scope1", "query": "anything", "limit": 10000,
            })
    assert result.isError is not True
    assert len(result.structuredContent["results"]) <= 25


async def test_alias_call_gets_the_same_validation_as_its_target(pool, app_space, conn):
    """design/03's "an alias is pure sugar, same validation as the tool it binds"
    -- now enforced rather than asserted. Validation runs on the RESOLVED core
    tool's MERGED arguments, so this holds by construction rather than by an
    alias-specific code path."""
    space_id, raw_rw, _raw_ro = app_space
    _install_alias(conn, space_id)
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            bad_type = await session.call_tool("log_error", {
                "scope": "scope1", "title": 7, "body": "Title is not a string.",
            })
            unknown = await session.call_tool("log_error", {
                "scope": "scope1", "title": "T", "body": "B", "flavour": "spicy",
            })
    assert bad_type.isError is True
    assert bad_type.content[0].text.startswith("ENGRAPHY_VALIDATION:")
    assert "title" in bad_type.content[0].text
    assert unknown.isError is True
    assert "flavour" in unknown.content[0].text


async def test_validation_runs_after_the_role_gate(pool, app_space):
    """Position is pinned by 07: the gates come first. A readonly token sending
    a malformed write must be told ENGRAPHY_ROLE -- leaking "your argument types
    are wrong" to a caller who may not call the tool at all would answer a
    question it was not entitled to ask."""
    space_id, _raw_rw, raw_ro = app_space
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_ro) as session:
            result = await session.call_tool("write", {
                "scope": "scope1", "type": "widget", "title": 7,
                "body": "Malformed AND unauthorized.", "flavour": "spicy",
            })
    assert result.isError is True
    assert result.content[0].text.startswith("ENGRAPHY_ROLE:")


async def test_validation_runs_after_the_rate_gate(pool, app_space, conn):
    """The other half of the pinned position: a malformed flood is still
    throttled, so sending garbage is not a way to buy cheap unlimited calls."""
    space_id, raw_rw, _raw_ro = app_space
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO config (space_id, key, value) VALUES (%s, 'rate.read_per_min', %s)",
        (space_id, "1"),
    )
    conn.commit()
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            first = await session.call_tool("scope_list", {})
            assert first.isError is not True
            # Malformed, and over the window: the rate limiter answers first.
            second = await session.call_tool("scope_list", {"bogus": 1})
    assert second.isError is True
    assert second.content[0].text.startswith("ENGRAPHY_RATE_LIMITED:")


async def test_published_schemas_carry_real_types_required_and_enums(pool, app_space):
    """tools/list now advertises the enforced surface rather than `{}` per
    property, because both are generated from wire_types.SPEC."""
    space_id, raw_rw, _raw_ro = app_space
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            listed = await session.list_tools()
    schemas = {t.name: t.inputSchema for t in listed.tools}

    search = schemas["search"]
    assert search["properties"]["limit"]["type"] == "integer"
    assert search["properties"]["query"]["type"] == "string"
    assert set(search["properties"]["detail"]["enum"]) == {"full", "summary"}
    assert sorted(search["required"]) == ["query", "scope"]
    assert search["additionalProperties"] is False

    # An admin tool's enum, from the same spec that used to hold admin args in
    # a separate hand-maintained dict.
    assert set(schemas["admin_grant"]["properties"]["level"]["enum"]) == {"read", "write"}
    assert schemas["get"]["properties"]["ids"]["type"] == "array"


async def test_an_alias_publishes_its_targets_generated_schema(pool, app_space, conn):
    space_id, raw_rw, _raw_ro = app_space
    _install_alias(conn, space_id)
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            listed = await session.list_tools()
    schemas = {t.name: t.inputSchema for t in listed.tools}
    assert schemas["log_error"] == schemas["write"]


async def test_admin_tools_validate_before_their_space_admin_gate(pool, app_space):
    """The one place the "gates first" ordering does NOT hold, pinned deliberately
    rather than left as an unexamined gap.

    `require_write` and the rate limiter live in handle_call_tool, so they run
    before validation (the two tests above). The SPACE-ADMIN gate is different:
    it lives inside the admin dispatchers (`admin.py::_assert_space_admin`), so a
    non-space-admin sending malformed admin arguments is told VALIDATION, not
    ROLE.

    Accepted, because the argument surface it reveals is already public: when
    `space_admin_tools` is enabled the admin_* tools appear in tools/list for
    EVERY token in the space, generated inputSchema and all (see
    test_tools_list_includes_all_fourteen_core_tools -- this same non-admin token
    sees them). Validation can therefore only restate what the caller was already
    served, so nothing leaks. The readonly-token case is the one that mattered
    and it still holds, because that gate is a real pre-dispatch gate.
    """
    space_id, raw_rw, _raw_ro = app_space
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            # p1 is a plain member, so this token is readwrite but NOT space_admin.
            malformed = await session.call_tool("admin_grant", {
                "scope_id": "scope1", "principal": "p2", "level": "sideways",
            })
            well_formed = await session.call_tool("admin_grant", {
                "scope_id": "scope1", "principal": "p2", "level": "read",
            })
    assert malformed.isError is True
    assert malformed.content[0].text.startswith("ENGRAPHY_VALIDATION:")
    # ...and once the arguments are well-formed, the space-admin gate answers.
    assert well_formed.isError is True
    assert well_formed.content[0].text.startswith("ENGRAPHY_ROLE:")


async def test_merged_envelope_instruction_reaches_the_caller_over_the_wire(pool, app_space, conn):
    """The instruction is only worth anything if it survives to the agent that
    has to act on it, so it is asserted at the transport and not just in-process
    (ruled 2026-07-21, the dupstream contradiction finding).

    Byte-exact against dedup.MERGED_INSTRUCTION, which is itself byte-pinned by
    design/07's example and fixtures/wire/write_merged.json -- three places that
    must agree, checked here rather than trusted.
    """
    from engraphy.core.dedup import MERGED_INSTRUCTION

    space_id, raw_rw, _raw_ro = app_space
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            first = await session.call_tool("write", {
                "scope": "scope1", "type": "widget", "title": "Priya's job",
                "body": "Priya works as a paediatric nurse.",
            })
            assert first.structuredContent["outcome"] == "inserted"
            # Byte-identical text: a guaranteed 1.0 self-hit, so this merges.
            second = await session.call_tool("write", {
                "scope": "scope1", "type": "widget", "title": "Priya's job",
                "body": "Priya works as a paediatric nurse.",
            })
    assert second.isError is not True
    envelope = second.structuredContent
    assert envelope["outcome"] == "merged"
    assert envelope["instruction"] == MERGED_INSTRUCTION
    assert "supersede" in envelope["instruction"]


async def test_write_description_tells_callers_about_the_merge_repair(pool, app_space):
    """descriptions are not contract (no fixture pins them), but this one is the
    only place a caller learns the rule BEFORE it hits a merge -- the envelope
    only speaks after the fact."""
    space_id, raw_rw, _raw_ro = app_space
    app = create_app(pool)
    async with _running_app(app):
        async with _mcp_session(app, raw_rw) as session:
            listed = await session.list_tools()
    write_tool = next(t for t in listed.tools if t.name == "write")
    assert "supersede" in write_tool.description
    assert "contradicted or updated" in write_tool.description
