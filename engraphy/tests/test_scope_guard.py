"""The per-token scope restriction: api_tokens.no_scope_all (migration 0023) ->
AuthContext.no_scope_all -> core.scope_set -> ENGRAPHY_SCOPE.

Deliberately DB-FREE, all of it. The guard is a property of the credential and
not of the data, so nothing here needs a space, a scope, or a row: the refusal
path is pure Python, the resolver path runs against a recording fake cursor, and
the dispatcher-wiring path substitutes the core function. That is not a
convenience -- a control whose only test needs a live Postgres is a control that
stops being checked the day the stack is down, which is the day it matters.

The live half (a real token minted --no-scope-all, resolved over the wire,
refused by a running engine) belongs in the integration suite and is NOT faked
here.

NOTE: engraphy/tests/conftest.py installs a session-scoped autouse fixture that
connects to Postgres, so pytest cannot currently collect this file without a
live database even though no test in it uses one. That is pre-existing (it
applies equally to test_wire_types.py and the other pure-logic files) and is a
conftest concern, not this module's.
"""

import pytest

from engraphy.core.scope_set import (
    ScopeForbiddenError,
    refuse_scope_all,
    resolve_scope_set,
)
from engraphy.server.auth import AuthContext
from engraphy.server.tools import read as read_tools
from engraphy.server.tools.errors import to_tool_error

# ---- the refusal itself (pure) --------------------------------------------


def test_a_restricted_token_is_refused_scope_all():
    """The control, stated at its smallest: this token, that argument, refused."""
    with pytest.raises(ScopeForbiddenError) as exc:
        refuse_scope_all("all", no_scope_all=True, tool="search")
    assert str(exc.value).startswith("ENGRAPHY_SCOPE: ")
    assert "scope='all'" in str(exc.value)
    assert "search" in str(exc.value)   # names the tool that refused


def test_an_unrestricted_token_may_still_ask_for_scope_all():
    """The other half, and the one that would catch a fail-closed-by-accident
    guard: the flag is opt-in, so every token that predates it -- which after
    migration 0023 is every token that exists -- keeps working unchanged."""
    assert refuse_scope_all("all", no_scope_all=False, tool="search") is None


@pytest.mark.parametrize("no_scope_all", [True, False])
def test_a_named_scope_is_never_refused(no_scope_all):
    """The restriction is on the BREADTH of the question, not on reading. A
    restricted session may read any scope it can name; what it may not do is ask
    for all of them in one call. Refusing a named scope here would turn an
    exfiltration guard into a read ban."""
    assert refuse_scope_all("work", no_scope_all, tool="briefing") is None


def test_the_refusal_reaches_the_wire_as_engraphy_scope():
    """tools/errors.py's mapping, which is what makes this a tool error rather
    than the ENGRAPHY_INTERNAL every unrecognised exception collapses into. The
    prefix must appear exactly once: ToolError re-adds it, and the typed
    exception already carries it (the house idiom, see _strip_code)."""
    err = to_tool_error(ScopeForbiddenError("ENGRAPHY_SCOPE: this token may not use scope='all'"))
    assert err.code == "SCOPE"
    assert str(err) == "ENGRAPHY_SCOPE: this token may not use scope='all'"


def test_the_refusal_is_not_collapsed_into_scope_unknown():
    """SCOPE and SCOPE_UNKNOWN mean different things and must not merge.
    SCOPE_UNKNOWN is not-found-shaped on purpose (existence is information, so a
    principal learns nothing about scopes it cannot reach); this refusal tells a
    bearer about its OWN credential, which leaks nothing, and collapsing it into
    not-found would tell an unattended session its scopes had vanished."""
    assert to_tool_error(ScopeForbiddenError("ENGRAPHY_SCOPE: x")).code != "SCOPE_UNKNOWN"


# ---- the shared resolver (fake cursor, still no DB) ------------------------


class _FakeCursor:
    """Enough psycopg AsyncCursor to drive core.visibility, and a record of every
    statement it was asked to run -- which is what lets a test assert that a
    refused call did NO visibility work rather than merely that it raised."""

    def __init__(self, readable=(), ambient=()):
        self._readable = [(s,) for s in readable]
        self._ambient = [(s,) for s in ambient]
        self.executed: list[str] = []
        self._rows: list[tuple] = []

    async def execute(self, sql, params=None):
        self.executed.append(sql)
        self._rows = self._ambient if "ambient" in sql else self._readable

    async def fetchall(self):
        return self._rows


async def test_the_resolver_refuses_before_it_queries_anything():
    """The guard is the resolver's first statement, so a refused call never
    reaches engraphy_readable_scopes(). Cheapness is the small reason; the real one
    is that the refusal must not depend on any visibility state being loadable."""
    cur = _FakeCursor(readable=("work", "personal", "health"))
    with pytest.raises(ScopeForbiddenError):
        await resolve_scope_set(cur, "all", no_scope_all=True, tool="search")
    assert cur.executed == [], "a refused call must issue no query at all"


async def test_the_resolver_still_expands_all_for_an_unrestricted_token():
    """scope='all' keeps meaning 'every readable scope' for everybody else --
    this change adds a refusal for marked tokens, it does not narrow the tool."""
    cur = _FakeCursor(readable=("work", "personal", "health"))
    got = await resolve_scope_set(cur, "all", no_scope_all=False, tool="search")
    assert got == {"work", "personal", "health"}


async def test_a_restricted_token_still_resolves_a_named_scope():
    """A restricted token is not a crippled one: the named-scope path is
    untouched, ambient expansion included, and still intersected with the
    readable set so an unreadable request contributes nothing."""
    cur = _FakeCursor(readable=("work", "shared"), ambient=("shared",))
    got = await resolve_scope_set(cur, "work", no_scope_all=True, tool="search")
    assert got == {"work", "shared"}

    unreadable = _FakeCursor(readable=("work",), ambient=())
    assert await resolve_scope_set(unreadable, "someone-elses", True, "search") == set()


# ---- the dispatcher wiring (substituted core fn, still no DB) --------------


def _ctx(no_scope_all):
    return AuthContext("tok", "space", "principal", "client", "readwrite", no_scope_all)


def test_auth_context_defaults_to_unrestricted():
    """Every hand-built AuthContext in the suite predates the field. The default
    must be the old behaviour, or adding the field would silently restrict them."""
    assert AuthContext("t", "s", "p", "c", "readonly").no_scope_all is False


@pytest.mark.parametrize("restricted", [True, False])
async def test_the_search_dispatcher_forwards_the_token_restriction(monkeypatch, restricted):
    """The one wiring a future scope-taking tool can forget. The guard lives in
    the shared resolver, so the ONLY way to miss it is to not pass the flag --
    which is exactly what this asserts, per tool."""
    seen = {}

    async def _recorder(*args, **kwargs):
        seen.update(kwargs)
        return {"v": 1, "results": []}

    monkeypatch.setattr(read_tools, "core_search", _recorder)
    await read_tools.search(None, _ctx(restricted), {"scope": "all", "query": "q"})
    assert seen["no_scope_all"] is restricted


@pytest.mark.parametrize("restricted", [True, False])
async def test_the_briefing_dispatcher_forwards_the_token_restriction(monkeypatch, restricted):
    """Same assertion for the second scope-taking tool. Two tools, two wirings,
    two tests: the shared resolver removes the duplicated LOGIC, not the
    duplicated obligation to hand it the flag."""
    seen = {}

    async def _recorder(*args, **kwargs):
        seen.update(kwargs)
        return {"v": 1, "sections": [], "footer": {}}

    async def _no_pack(pool, ctx):
        return {}

    monkeypatch.setattr(read_tools, "core_briefing", _recorder)
    monkeypatch.setattr(read_tools, "_resolve_pack_briefing_config", _no_pack)
    await read_tools.briefing(None, _ctx(restricted), {"scope": "all"})
    assert seen["no_scope_all"] is restricted
