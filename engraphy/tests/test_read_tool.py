"""engraphy.server.tools.read — the get/search/traverse/briefing MCP tool
dispatchers. Core functions are exhaustively tested in test_get.py/
test_search.py/test_traverse.py/test_briefing.py; these tests cover only the
tool layer's own responsibilities (arg mapping, dispatch).
"""
import pytest
from psycopg.types.json import Jsonb

from engraphy.server.auth import AuthContext, ToolError
from engraphy.server.tools.read import briefing, get, search, traverse
from engraphy.tests.test_dedup import _seed_node, _unit_vector_at_angle, write_space  # noqa: F401


def _ctx(space_id, principal="p1", role="readwrite", no_scope_all=False):
    return AuthContext("t1", space_id, principal, "pytest-client", role, no_scope_all)


async def test_get_tool_found_and_missing_ids(pool, write_space, conn):
    nid = _seed_node(
        conn, write_space, "widget", "Coffee maker", "Descale monthly.", {}, _unit_vector_at_angle(0)
    )
    missing_id = "00000000-0000-4000-8000-000000000000"

    result = await get(pool, _ctx(write_space), {"ids": [str(nid), missing_id]})

    assert result["v"] == 1
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["id"] == str(nid)
    assert result["missing"] == [missing_id]


async def test_search_tool_defaults_and_happy_path(pool, write_space, conn):
    nid = _seed_node(
        conn, write_space, "widget", "Coffee machine descaling",
        "Descale the office coffee machine monthly or it fails.", {}, _unit_vector_at_angle(0),
    )
    result = await search(pool, _ctx(write_space), {"scope": "scope1", "query": "descaling"})
    assert result["v"] == 1
    assert result["detail"] == "full"
    # synthetic seed vectors won't hit the vector leg meaningfully, but the
    # lexical leg is plain full-text search over the real title/body text.
    ids = [r["node"]["id"] for r in result["results"]]
    assert str(nid) in ids


async def test_search_tool_rejects_invalid_detail(pool, write_space):
    with pytest.raises(ToolError) as exc_info:
        await search(pool, _ctx(write_space), {"scope": "scope1", "query": "x", "detail": "bogus"})
    assert exc_info.value.code == "VALIDATION"


async def test_traverse_tool_missing_direction_translates_to_validation_error(pool, write_space, conn):
    nid = _seed_node(conn, write_space, "widget", "Node", "Body.", {}, _unit_vector_at_angle(0))
    with pytest.raises(ToolError) as exc_info:
        await traverse(pool, _ctx(write_space), {"start_id": str(nid)})
    assert exc_info.value.code == "VALIDATION"


async def test_traverse_tool_happy_path_defaults_to_summary(pool, write_space, conn):
    nid = _seed_node(conn, write_space, "widget", "Node", "Body.", {}, _unit_vector_at_angle(0))
    result = await traverse(pool, _ctx(write_space), {"start_id": str(nid), "direction": "both"})
    assert result["v"] == 1
    assert result["detail"] == "summary"
    assert result["nodes"][0]["id"] == str(nid)
    assert "body" not in result["nodes"][0]


async def test_traverse_tool_rejects_invalid_detail(pool, write_space, conn):
    nid = _seed_node(conn, write_space, "widget", "Node", "Body.", {}, _unit_vector_at_angle(0))
    with pytest.raises(ToolError) as exc_info:
        await traverse(pool, _ctx(write_space), {"start_id": str(nid), "direction": "both", "detail": "bogus"})
    assert exc_info.value.code == "VALIDATION"


async def test_briefing_tool_resolves_pack_config_from_config_table(pool, write_space, conn):
    """packs.py::apply() persists the applied pack's briefing: fragment under
    config['pack.briefing']; this dispatcher reads it back and threads it
    into core.briefing.briefing()'s config parameter."""
    _seed_node(conn, write_space, "widget", "A standing widget", "Body.", {}, _unit_vector_at_angle(0))
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO config (space_id, key, value) VALUES (%s, 'pack.briefing', %s)",
        (write_space, Jsonb({"sections": [{"name": "widgets", "type": "widget", "status": "active"}]})),
    )
    conn.commit()

    result = await briefing(pool, _ctx(write_space), {"scope": "scope1"})
    assert result["v"] == 1
    assert [s["name"] for s in result["sections"]] == ["widgets"]
    assert len(result["sections"][0]["nodes"]) == 1


async def test_briefing_tool_no_pack_applied_returns_empty_sections(pool, write_space):
    """No config['pack.briefing'] row (write_space never applied a pack) ->
    an empty briefing, not an error."""
    result = await briefing(pool, _ctx(write_space), {"scope": "scope1"})
    assert result["v"] == 1
    assert result["sections"] == []


# ---- the per-token scope restriction, end to end through the dispatcher ----
#
# LIVE: these four need Postgres with migration 0023 applied. The guard's own
# logic is proven without a database in test_scope_guard.py; what these add is
# the half only a real engine can show -- that a restricted context reaching the
# real resolver, over real visibility SQL, comes back ENGRAPHY_SCOPE instead of
# rows, and that an unrestricted one still gets the rows it always did.


async def test_search_refuses_scope_all_for_a_restricted_token(pool, write_space, conn):
    """The control itself. Same space, same principal, same query -- only the
    credential differs, which is the entire point of putting the restriction on
    the token rather than on the principal's grants."""
    _seed_node(conn, write_space, "widget", "Descaling", "Descale monthly.", {},
               _unit_vector_at_angle(0))
    with pytest.raises(ToolError) as exc_info:
        await search(pool, _ctx(write_space, no_scope_all=True),
                     {"scope": "all", "query": "descaling"})
    assert exc_info.value.code == "SCOPE"
    assert str(exc_info.value).startswith("ENGRAPHY_SCOPE: ")


async def test_search_still_allows_scope_all_for_an_unrestricted_token(pool, write_space, conn):
    """The regression half. Every token minted before migration 0023 is
    unrestricted, so a guard that refused them all would be an outage."""
    nid = _seed_node(conn, write_space, "widget", "Descaling", "Descale monthly.", {},
                     _unit_vector_at_angle(0))
    result = await search(pool, _ctx(write_space), {"scope": "all", "query": "descaling"})
    assert str(nid) in [r["node"]["id"] for r in result["results"]]


async def test_search_allows_a_named_scope_for_a_restricted_token(pool, write_space, conn):
    """A restricted session can still do its job: it reads the scope it names.
    What it cannot do is ask for every scope in one call."""
    nid = _seed_node(conn, write_space, "widget", "Descaling", "Descale monthly.", {},
                     _unit_vector_at_angle(0))
    result = await search(pool, _ctx(write_space, no_scope_all=True),
                          {"scope": "scope1", "query": "descaling"})
    assert str(nid) in [r["node"]["id"] for r in result["results"]]


async def test_briefing_refuses_scope_all_for_a_restricted_token(pool, write_space):
    """The second scope-taking tool. Searching is the obvious exfiltration path;
    a briefing over every scope is the same read wearing a different tool name,
    which is why the guard lives in the shared resolver and not in search."""
    with pytest.raises(ToolError) as exc_info:
        await briefing(pool, _ctx(write_space, no_scope_all=True), {"scope": "all"})
    assert exc_info.value.code == "SCOPE"
