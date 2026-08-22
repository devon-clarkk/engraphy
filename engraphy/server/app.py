"""FastMCP app: /mcp (Streamable HTTP), POST /inbox, GET /healthz.

Boot order: load config -> schema version gate (refuse on mismatch, name both versions)
-> load embedding model (warm the cache) -> serve. Per-space pack fragments
(tool_aliases, tool_descriptions, the briefing config) are deliberately NOT
preloaded at boot: tool_registry.py reads them fresh from `config` on every
`tools/list` call and every alias resolution, so a `pack apply`/`pack upgrade`
takes effect on a running server without a restart -- see QUESTIONS.md's
"Unreviewed E2 implementation choices" section for why no in-process cache
exists for them yet.
Transport refusal: plaintext auth on a public interface exits nonzero unless
insecure_transport_ok (design/04 §Deployment shape).

Built on the MCP SDK's low-level `Server` + `StreamableHTTPSessionManager`
rather than the `FastMCP` decorator class: FastMCP registers a fixed tool
list at decoration time, but this app's tool list is per-space (a pack's
aliases add tool names; admin tools must be entirely absent, not merely
gated, when `space_admin_tools=false` -- E2-plan.md s.6/s.5.5). The low-level
`Server.list_tools()`/`call_tool()` handlers run per-request instead, which
is what per-space dynamic tool sets require. Mounted alongside plain
Starlette routes for /inbox and /healthz, so "FastMCP app" here means "the
official MCP SDK's server", not literally the `FastMCP` convenience class.

`call_tool` is registered with `validate_input=False`, and that is **permanent
design** rather than a gap awaiting an upstream fix (ruled 2026-07-21). Two
independent reasons, either sufficient: `Server`'s `_tool_cache` is a single
process-global dict list_tools/call_tool share, so two concurrent requests from
different spaces registering different tool sets would clobber each other's
cached schema if the SDK's jsonschema validation were left on; and the SDK's
validation errors are not `ENGRAPHY_<CODE>`-shaped, so enabling it would break
07's own error contract as a side effect. Do not flip it, and do not reach into
`_tool_cache`.

Wire types ARE enforced -- by us, at `handle_call_tool` below, against
`engraphy/server/wire_types.py`'s transcription of 07's per-argument table
(design/07 §Per-argument wire types). The published `tool.inputSchema` is
generated from that same spec, so the advertised surface and the enforced one
cannot drift; it is a truthful advertisement for client UIs and never a
validation source, which is what keeps the cache hazard above dormant.
Dispatchers keep their own required-argument reads as defense in depth (a
missing key raises KeyError, translated to ENGRAPHY_VALIDATION by
tools/errors.py).
"""
import contextlib
import contextvars
import ipaddress
import os
import pathlib

import psycopg
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

import engraphy
from engraphy.core import embedding
from engraphy.core.inbox import capture as core_capture
from engraphy.server import tool_registry, wire_types
from engraphy.server.auth import (
    AuthContext,
    FailureTracker,
    RateLimiter,
    ToolError,
    Unauthorized,
    classify_kind,
    read_rate_limits,
    require_write,
    resolve_token,
)
from engraphy.server.db import get_pool
from engraphy.server.tools.errors import to_tool_error

# The engine version reported by /healthz, read from the ONE place the number
# is written down: engraphy/__init__.py's __version__ (pyproject.toml derives its
# [project].version from the same attribute). This used to be a hand-maintained
# literal whose comment claimed it and pyproject were "the same source"; they
# were not, and nothing would have caught them diverging -- a release that
# bumped pyproject alone would have shipped a server reporting the old version
# on every /healthz, which is exactly the field an operator trusts to tell them
# what is deployed.
#
# Deliberately the module attribute rather than importlib.metadata.version():
# installed metadata is written at install time, so an editable install that
# predates a version bump keeps reporting the OLD number until someone
# reinstalls (observed on this repo's dev box -- dist-info said 0.0.1 while
# pyproject said 0.1.0). Reading the module means the running code and the
# reported version are the same artifact, in every configuration.
ENGINE_VERSION = engraphy.__version__

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / "db" / "migrations"

# What list_tools/call_tool read to know who's calling -- set by
# BearerAuthMiddleware before the request reaches the MCP transport, read
# back inside the SDK's per-request handlers. A ContextVar rather than
# request.state: the SDK's Server exposes request_context.request (the same
# Starlette Request the middleware saw), so request.state would also work,
# but that depends on the SDK never copying `scope` between the middleware
# and its own Request construction -- unverified SDK-internal behavior. A
# ContextVar set before the request is dispatched needs no such assumption
# (a spawned child task still inherits the value already set at spawn time).
_auth_ctx_var: contextvars.ContextVar[AuthContext | None] = contextvars.ContextVar(
    "engram_auth_ctx", default=None
)


class SchemaVersionMismatch(Exception):
    """Boot-time gate failure (design/04 s.Engine migrations): the DB's
    schema_migrations is behind the migrations shipped with this code.
    Refuses to start rather than serve against a DB an operator forgot to
    `engraphy-admin migrate` against."""


def expected_schema_version() -> str:
    """The latest migration file's version prefix -- this code's own
    expectation, derived from the migrations directory shipped alongside it
    (never hardcoded, so it can't drift from the files themselves)."""
    versions = sorted(p.name.split("_", 1)[0] for p in _MIGRATIONS_DIR.glob("*.sql"))
    return versions[-1]


async def applied_schema_version(pool) -> str | None:
    """The DB's actual state: max(version) in schema_migrations. None on a
    DB dbmate has never touched (no schema_migrations table at all -- true
    of this repo's ad-hoc local dev DB) or an empty one (max() -> NULL)."""
    async with pool.connection() as conn:
        cur = conn.cursor()
        try:
            await cur.execute("SELECT max(version) FROM schema_migrations")
        except psycopg.errors.UndefinedTable:
            return None
        (version,) = await cur.fetchone()
    return version


async def check_schema_version(pool) -> None:
    """Call before serving (main(), not create_app() -- tests build the app
    directly against whatever migration state their DB happens to be in, and
    must not be forced through this gate to do it)."""
    expected = expected_schema_version()
    applied = await applied_schema_version(pool)
    if applied != expected:
        raise SchemaVersionMismatch(
            f"schema_migrations is at {applied!r}, this code expects {expected!r} "
            "-- run `engraphy-admin migrate`"
        )


def _is_private_or_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False  # a hostname, not a literal address -- can't classify; fail closed as public
    return addr.is_loopback or addr.is_private


def check_transport_security(bind_host: str, insecure_transport_ok: bool) -> None:
    """design/04 s.Deployment shape: TLS is the deployment's job (WireGuard,
    reverse proxy, `tailscale cert`), but the process itself refuses to bind
    a public interface in plaintext unless the operator opts in explicitly.
    Loopback and RFC1918/CGNAT ranges are the local/overlay profile and need
    no opt-in; anything else -- including an unparseable hostname -- does."""
    if insecure_transport_ok or _is_private_or_loopback(bind_host):
        return
    raise RuntimeError(
        f"refusing to bind {bind_host!r} without TLS in front of it or explicit "
        "insecure_transport_ok=true (overlay-networks-only, design/04 s.Deployment shape)"
    )


def _read_last_backup_at() -> str | None:
    """/healthz.last_backup_at: an operator-maintained status file path
    (design/04 s.Backup contract), read fresh on every call so staleness is
    observable in-band. ENGRAPHY_LAST_BACKUP_STATUS_FILE follows this
    codebase's existing ENGRAPHY_* env-var convention (ENGRAPHY_TEST_DATABASE_URL,
    ENGRAPHY_BENCH_ITERS); unset or unreadable -> omit the field (07 marks it
    optional)."""
    path = os.environ.get("ENGRAPHY_LAST_BACKUP_STATUS_FILE")
    if not path:
        return None
    try:
        return pathlib.Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _error_result(exc: ToolError) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=str(exc))],
        structuredContent=dict(exc.extra) if exc.extra else None,
        isError=True,
    )


def _build_mcp_server(pool, rate_limiter: RateLimiter) -> Server:
    server = Server("engraphy")

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        ctx = _auth_ctx_var.get()
        if ctx is None:  # pragma: no cover -- BearerAuthMiddleware already gated /mcp
            return []
        entries = await tool_registry.list_tools_for_space(pool, ctx.space_id)
        return [
            types.Tool(
                name=e["name"], description=e["description"], inputSchema=e["inputSchema"],
                annotations=types.ToolAnnotations(**e["annotations"]) if e.get("annotations") else None,
            )
            for e in entries
        ]

    @server.call_tool(validate_input=False)
    async def handle_call_tool(name: str, arguments: dict):
        ctx = _auth_ctx_var.get()
        if ctx is None:  # pragma: no cover -- BearerAuthMiddleware already gated /mcp
            return _error_result(ToolError("INTERNAL", "an unexpected error occurred"))

        resolved = await tool_registry.resolve_dispatch(pool, ctx.space_id, name, arguments or {})
        if resolved is None:
            return _error_result(ToolError("VALIDATION", f"unknown tool: {name}"))
        core_name, dispatcher, merged_args, action = resolved

        try:
            # Role/rate gating happens on the RESOLVED core name (design/03:
            # an alias is "pure sugar", same validation as the tool it
            # binds) -- log_error isn't in WRITE_TOOLS, write is.
            require_write(ctx, core_name, merged_args)
            kind = classify_kind(core_name, merged_args)
            async with pool.connection() as conn:
                read_limit, write_limit = await read_rate_limits(conn, ctx.space_id)
            rate_limiter.check(ctx.token_id, kind, read_limit, write_limit)
        except ToolError as exc:
            return _error_result(exc)

        try:
            # 07 §Per-argument wire types, enforced (ruled 2026-07-21). Position
            # is pinned: AFTER the role/rate gates above, so a malformed flood is
            # still throttled rather than being cheap to send, and BEFORE
            # dispatch. Validates the RESOLVED core tool's MERGED arguments, so
            # an alias inherits its target's surface by construction and a pack
            # preset naming a bogus argument fails every call loudly.
            wire_types.validate(core_name, merged_args)
            if core_name == "write":
                return await dispatcher(pool, ctx, merged_args, action=(action or "write"))
            return await dispatcher(pool, ctx, merged_args)
        except ToolError as exc:
            return _error_result(exc)
        except Exception as exc:
            # Every dispatcher wraps its OWN core-layer call in try/except ->
            # to_tool_error, but each one reads its required wire arguments
            # (arguments["scope"], ...) BEFORE that try block -- a missing
            # key raises a bare KeyError that never passes through a
            # dispatcher's own translation. Catching broadly here, at the one
            # place every tool call funnels through, is the same "one place"
            # reasoning tools/errors.py already uses rather than re-ordering
            # five dispatcher files to move arg extraction inside their own
            # try blocks.
            return _error_result(to_tool_error(exc))

    return server


class BearerAuthMiddleware:
    """Wraps every route except /healthz (design/03: '/inbox' takes the same
    bearer auth as MCP; /healthz is unauthenticated, deployment-gated by
    network placement instead). Resolves the bearer on a plain pooled
    connection -- api_tokens is instance-level, predating any space GUC, so
    this must NOT go through db.transaction(). A missing/unknown/revoked
    bearer is a transport 401 (auth.Unauthorized), never an ENGRAPHY_ tool
    error; repeated failures from one client trip FailureTracker's ban."""

    _EXEMPT_PATHS = frozenset({"/healthz"})

    def __init__(self, app, pool, failure_tracker: FailureTracker):
        self._app = app
        self._pool = pool
        self._failure_tracker = failure_tracker

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in self._EXEMPT_PATHS:
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive)
        client_key = request.client.host if request.client else "unknown"
        if self._failure_tracker.is_banned(client_key):
            response = PlainTextResponse("too many failed auth attempts", status_code=401)
            await response(scope, receive, send)
            return

        auth_header = request.headers.get("authorization", "")
        raw_token = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
        try:
            async with self._pool.connection() as conn:
                ctx = await resolve_token(conn, raw_token)
        except Unauthorized:
            self._failure_tracker.record_failure(client_key)
            response = PlainTextResponse("unauthorized", status_code=401)
            await response(scope, receive, send)
            return

        token = _auth_ctx_var.set(ctx)
        scope.setdefault("state", {})["auth_ctx"] = ctx
        try:
            await self._app(scope, receive, send)
        finally:
            _auth_ctx_var.reset(token)


def create_app(pool, *, insecure_transport_ok: bool = False) -> Starlette:
    """Build the Starlette app. Caller owns `pool`'s lifecycle (opening it
    before serving, closing it at shutdown) -- this function only wires
    routes and middleware around it. `insecure_transport_ok` is threaded
    through for symmetry with main()'s boot check; create_app() itself binds
    no socket, so it has nothing to refuse here (see check_transport_security,
    called by main() before uvicorn.run)."""
    del insecure_transport_ok  # main()'s concern (bind-time), not app-wiring's; kept as a documented no-op here

    failure_tracker = FailureTracker()
    rate_limiter = RateLimiter()
    mcp_server = _build_mcp_server(pool, rate_limiter)
    session_manager = StreamableHTTPSessionManager(app=mcp_server, stateless=True)

    async def _inbox_capture(request: Request) -> Response:
        ctx: AuthContext = request.state.auth_ctx
        try:
            body = await request.json()
            kind = body["kind"]
            payload = body["payload"]
        except Exception:
            return JSONResponse(
                {"error": "ENGRAPHY_VALIDATION: request body must be JSON {kind, payload, scope?}"},
                status_code=400,
            )
        scope_id = body.get("scope")
        try:
            result = await core_capture(pool, ctx.space_id, ctx.principal, kind, payload, scope_id)
        except Exception as exc:
            return JSONResponse({"error": str(to_tool_error(exc))}, status_code=400)
        return JSONResponse(result)

    async def _healthz(request: Request) -> Response:
        del request
        async with pool.connection() as conn:
            cur = conn.cursor()
            await cur.execute("SELECT count(*) FROM spaces")
            (space_count,) = await cur.fetchone()
        body = {
            "status": "ok",
            "version": ENGINE_VERSION,
            "schema_version": await applied_schema_version(pool),
            "spaces": space_count,
            "embedding_model": embedding.MODEL_ID,
        }
        last_backup_at = _read_last_backup_at()
        if last_backup_at is not None:
            body["last_backup_at"] = last_backup_at
        return JSONResponse(body)

    routes = [
        Route("/healthz", _healthz, methods=["GET"]),
        Route("/inbox", _inbox_capture, methods=["POST"]),
        Mount("/mcp", app=session_manager.handle_request),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app):
        del app
        async with session_manager.run():
            yield

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(BearerAuthMiddleware, pool=pool, failure_tracker=failure_tracker)
    return app


def main() -> None:  # pragma: no cover -- process entrypoint, not exercised by tests
    import uvicorn

    conninfo = os.environ["ENGRAPHY_DATABASE_URL"]
    bind_host = os.environ.get("ENGRAPHY_BIND_HOST", "127.0.0.1")
    bind_port = int(os.environ.get("ENGRAPHY_BIND_PORT", "8000"))
    insecure_transport_ok = os.environ.get("ENGRAPHY_INSECURE_TRANSPORT_OK", "") == "true"

    check_transport_security(bind_host, insecure_transport_ok)

    pool = get_pool(conninfo)

    async def _run():
        await pool.open()
        try:
            await check_schema_version(pool)
            embedding.embed_query("warm the model cache")  # boot order: load embedding model before serving
            app = create_app(pool, insecure_transport_ok=insecure_transport_ok)
            config = uvicorn.Config(app, host=bind_host, port=bind_port, log_level="info")
            await uvicorn.Server(config).serve()
        finally:
            await pool.close()

    import anyio

    anyio.run(_run)


if __name__ == "__main__":  # pragma: no cover
    main()
