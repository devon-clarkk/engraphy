"""engraphy.server.tools.write.write — the `write` MCP tool dispatcher: wire-name
mapping (scope/type/session_id -> scope_id/node_type/source_session), the
pre-transaction embed step, and exception -> ToolError translation. dedup.write()
itself is exhaustively tested in test_dedup.py; these tests cover the tool
layer's own responsibilities, not the pipeline.
"""
import math

import pytest

from engraphy.core import embedding
from engraphy.core.dedup import write as dedup_write
from engraphy.server.auth import AuthContext, ToolError
from engraphy.server.tools.write import link, resolve_duplicate, supersede, update, write
from engraphy.tests.test_dedup import _seed_node, _unit_vector_at_angle, write_space  # noqa: F401
from engraphy.tests import bandvalues as bv


def _ctx(space_id, principal="p1", role="readwrite"):
    return AuthContext("t1", space_id, principal, "pytest-client", role)


async def test_write_tool_insert_happy_path(pool, write_space):
    result = await write(pool, _ctx(write_space), {
        "scope": "scope1", "type": "widget", "title": "A brand new title",
        "body": "A brand new body.", "attrs": {},
    })
    assert result["v"] == 1
    assert result["outcome"] == "inserted"
    node = result["node"]
    assert node["type"] == "widget"
    assert node["scope"] == "scope1"
    assert node["title"] == "A brand new title"
    assert node["body"] == "A brand new body."
    assert node["status"] == "active"
    assert node["author"] == "p1"
    assert "id" in node and "created_at" in node
    assert result["resonance"] == []  # sole node in the space -- nothing resonant


async def test_write_tool_defaults_attrs_and_links_when_omitted(pool, write_space):
    result = await write(pool, _ctx(write_space), {
        "scope": "scope1", "type": "widget", "title": "Title", "body": "B",
    })
    assert result["outcome"] == "inserted"
    assert result["node"]["attrs"] == {}


async def test_write_tool_passes_session_id_through_to_source_session(pool, write_space, conn):
    result = await write(pool, _ctx(write_space), {
        "scope": "scope1", "type": "widget", "title": "Title", "body": "B",
        "session_id": "sess-tool-1",
    })
    cur = conn.cursor()
    cur.execute("SELECT source_session FROM nodes WHERE id = %s", (result["node"]["id"],))
    assert cur.fetchone()[0] == "sess-tool-1"


async def test_write_tool_action_param_reaches_audit_log(pool, write_space, conn):
    """app.py's alias dispatch passes resolve_alias_call's audit identity
    through as `action`; a direct tool call never sets it and gets "write"."""
    result = await write(pool, _ctx(write_space), {
        "scope": "scope1", "type": "widget", "title": "Title", "body": "B",
    }, action="write via log_error")
    cur = conn.cursor()
    cur.execute(
        "SELECT action FROM audit_log WHERE space_id = %s AND detail->>'node_id' = %s",
        (write_space, result["node"]["id"]),
    )
    assert cur.fetchone()[0] == "write via log_error"


async def test_write_tool_scope_unknown_translates_to_tool_error(pool, write_space):
    with pytest.raises(ToolError) as exc_info:
        await write(pool, _ctx(write_space), {
            "scope": "nope", "type": "widget", "title": "Title", "body": "B",
        })
    err = exc_info.value
    assert err.code == "SCOPE_UNKNOWN"
    assert str(err) == "ENGRAPHY_SCOPE_UNKNOWN: scope 'nope' does not exist or is not writable"


async def test_write_tool_reserved_attrs_addenda_translates_to_validation_error(pool, write_space):
    with pytest.raises(ToolError) as exc_info:
        await write(pool, _ctx(write_space), {
            "scope": "scope1", "type": "widget", "title": "Title", "body": "B",
            "attrs": {"addenda": [{"body": "spoofed"}]},
        })
    assert exc_info.value.code == "VALIDATION"


async def test_write_tool_malformed_link_translates_to_validation_error(pool, write_space):
    with pytest.raises(ToolError) as exc_info:
        await write(pool, _ctx(write_space), {
            "scope": "scope1", "type": "widget", "title": "Title", "body": "B",
            "links": [{"type": "relates_to"}],  # neither endpoint named
        })
    assert exc_info.value.code == "VALIDATION"


async def test_write_tool_edge_rule_violation_translates_to_edge_rule_error(pool, write_space, conn):
    """The edges_validate trigger's CheckViolation ('edge_rules: no rule for
    type=...') is the one case that must NOT collapse to VALIDATION -- write_space
    only registers (relates_to, widget, widget) and (relates_to, error, error),
    so a widget->error relates_to link has no matching rule."""
    peer = _seed_node(conn, write_space, "error", "Peer error", "Peer body.", {}, _unit_vector_at_angle(0))

    with pytest.raises(ToolError) as exc_info:
        await write(pool, _ctx(write_space), {
            "scope": "scope1", "type": "widget", "title": "Title", "body": "B",
            "links": [{"type": "relates_to", "dst_id": str(peer)}],
        })
    assert exc_info.value.code == "EDGE_RULE"


async def test_write_tool_readonly_role_is_not_gated_here(pool, write_space):
    """require_write is the transport layer's job (E2-plan.md s.6), not this
    dispatcher's -- a readonly ctx still succeeds when called directly."""
    result = await write(pool, _ctx(write_space, role="readonly"), {
        "scope": "scope1", "type": "widget", "title": "Title", "body": "B",
    })
    assert result["outcome"] == "inserted"


async def test_resolve_duplicate_tool_distinct_happy_path(pool, write_space, conn):
    _seed_node(conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0))
    parked = await dedup_write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Similar-ish body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",
    )
    assert parked["outcome"] == "needs_confirmation"

    result = await resolve_duplicate(pool, _ctx(write_space), {
        "pending_id": parked["pending_id"], "resolution": "distinct",
    })
    assert result["outcome"] == "inserted"
    assert result["relates_edge_added"] is True


async def test_resolve_duplicate_tool_merge_happy_path(pool, write_space, conn):
    candidate_id = _seed_node(
        conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0)
    )
    parked = await dedup_write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Existing body.", {},  # non-novel -> the explicit merge absorbs
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",
    )
    assert parked["outcome"] == "needs_confirmation"

    result = await resolve_duplicate(pool, _ctx(write_space), {
        "pending_id": parked["pending_id"], "resolution": "merge", "merge_into": str(candidate_id),
    })
    assert result["outcome"] == "merged"
    assert result["canonical"]["id"] == str(candidate_id)


async def test_resolve_duplicate_tool_invalid_resolution_translates_to_validation_error(pool, write_space):
    with pytest.raises(ToolError) as exc_info:
        await resolve_duplicate(pool, _ctx(write_space), {
            "pending_id": "00000000-0000-4000-8000-000000000000", "resolution": "bogus",
        })
    assert exc_info.value.code == "VALIDATION"


async def test_resolve_duplicate_tool_unknown_pending_id_translates_to_not_found(pool, write_space):
    with pytest.raises(ToolError) as exc_info:
        await resolve_duplicate(pool, _ctx(write_space), {
            "pending_id": "00000000-0000-4000-8000-000000000000", "resolution": "distinct",
        })
    assert exc_info.value.code == "NOT_FOUND"


async def test_update_tool_happy_path(pool, write_space, conn):
    nid = _seed_node(conn, write_space, "widget", "Old title", "Old body.", {}, _unit_vector_at_angle(0))
    result = await update(pool, _ctx(write_space), {"id": str(nid), "title": "New title"})
    assert result["outcome"] == "updated"
    assert result["node"]["title"] == "New title"
    assert result["node"]["body"] == "Old body."


async def test_update_tool_unknown_id_translates_to_not_found(pool, write_space):
    with pytest.raises(ToolError) as exc_info:
        await update(pool, _ctx(write_space), {
            "id": "00000000-0000-4000-8000-000000000000", "title": "New title",
        })
    assert exc_info.value.code == "NOT_FOUND"


async def test_update_tool_reserved_attrs_addenda_translates_to_validation_error(pool, write_space, conn):
    nid = _seed_node(conn, write_space, "widget", "Old title", "Old body.", {}, _unit_vector_at_angle(0))
    with pytest.raises(ToolError) as exc_info:
        await update(pool, _ctx(write_space), {
            "id": str(nid), "attrs": {"addenda": [{"body": "spoofed"}]},
        })
    assert exc_info.value.code == "VALIDATION"


async def test_link_tool_happy_path(pool, write_space, conn):
    a = _seed_node(conn, write_space, "widget", "Node A", "Body A.", {}, _unit_vector_at_angle(0))
    b = _seed_node(conn, write_space, "widget", "Node B", "Body B.", {}, _unit_vector_at_angle(1))

    result = await link(pool, _ctx(write_space), {
        "edges": [{"type": "relates_to", "src_id": str(a), "dst_id": str(b)}],
    })
    assert result == {"v": 1, "attached": 1, "skipped": 0}


async def test_link_tool_unknown_endpoint_translates_to_not_found(pool, write_space, conn):
    a = _seed_node(conn, write_space, "widget", "Node A", "Body A.", {}, _unit_vector_at_angle(0))
    with pytest.raises(ToolError) as exc_info:
        await link(pool, _ctx(write_space), {
            "edges": [{"type": "relates_to", "src_id": str(a), "dst_id": "00000000-0000-4000-8000-000000000000"}],
        })
    assert exc_info.value.code == "NOT_FOUND"


async def test_link_tool_missing_endpoint_translates_to_validation_error(pool, write_space, conn):
    a = _seed_node(conn, write_space, "widget", "Node A", "Body A.", {}, _unit_vector_at_angle(0))
    with pytest.raises(ToolError) as exc_info:
        await link(pool, _ctx(write_space), {
            "edges": [{"type": "relates_to", "src_id": str(a)}],
        })
    assert exc_info.value.code == "VALIDATION"


async def test_link_tool_no_matching_edge_rule_translates_to_edge_rule_error(pool, write_space, conn):
    a = _seed_node(conn, write_space, "widget", "Node A", "Body A.", {}, _unit_vector_at_angle(0))
    b = _seed_node(conn, write_space, "error", "Node B", "Body B.", {}, _unit_vector_at_angle(1))
    with pytest.raises(ToolError) as exc_info:
        await link(pool, _ctx(write_space), {
            "edges": [{"type": "relates_to", "src_id": str(a), "dst_id": str(b)}],
        })
    assert exc_info.value.code == "EDGE_RULE"


async def test_supersede_tool_happy_path(pool, write_space, conn):
    old_id = _seed_node(conn, write_space, "widget", "Old node", "Old body.", {}, _unit_vector_at_angle(0))
    result = await supersede(pool, _ctx(write_space), {
        "old_id": str(old_id), "scope": "scope1", "type": "widget",
        "title": "New node", "body": "New body, a revision of the old one.",
    })
    assert result["outcome"] == "inserted"
    assert result["superseded"] == str(old_id)

    cur = conn.cursor()
    cur.execute("SELECT status FROM nodes WHERE id = %s", (old_id,))
    assert cur.fetchone()[0] == "superseded"


async def test_supersede_tool_cross_type_translates_to_validation_error(pool, write_space, conn):
    old_id = _seed_node(conn, write_space, "widget", "Old node", "Old body.", {}, _unit_vector_at_angle(0))
    with pytest.raises(ToolError) as exc_info:
        await supersede(pool, _ctx(write_space), {
            "old_id": str(old_id), "scope": "scope1", "type": "error",
            "title": "New node", "body": "New body.",
        })
    assert exc_info.value.code == "VALIDATION"


async def test_supersede_tool_unknown_old_id_translates_to_not_found(pool, write_space):
    with pytest.raises(ToolError) as exc_info:
        await supersede(pool, _ctx(write_space), {
            "old_id": "00000000-0000-4000-8000-000000000000", "scope": "scope1", "type": "widget",
            "title": "New node", "body": "New body.",
        })
    assert exc_info.value.code == "NOT_FOUND"


async def test_supersede_tool_unresolved_band_translates_to_supersede_conflict(pool, write_space, conn):
    """QUESTIONS.md 'supersede-nonclean-band': a third node pushes the
    replacement into MERGE/PENDING against something other than old_id ->
    dedup.SupersedeUnresolvedBandError -> ENGRAPHY_SUPERSEDE_CONFLICT here.
    Real embedding is used through the tool, so the third node is seeded with
    the SAME title+body text the supersede call will send -- byte-identical
    text embeds identically (the pinned model is deterministic), guaranteeing
    a >= 0.95 MERGE band against it, deterministically."""
    old_id = _seed_node(conn, write_space, "widget", "Old node", "Old body.", {}, _unit_vector_at_angle(0))
    third_title, third_body = "Third node", "Third node body, unrelated to the old one."
    third_vec = embedding.embed_document(third_title + "\n" + third_body)
    _seed_node(conn, write_space, "widget", third_title, third_body, {}, third_vec)

    with pytest.raises(ToolError) as exc_info:
        await supersede(pool, _ctx(write_space), {
            "old_id": str(old_id), "scope": "scope1", "type": "widget",
            "title": third_title, "body": third_body,
        })
    assert exc_info.value.code == "SUPERSEDE_CONFLICT"
