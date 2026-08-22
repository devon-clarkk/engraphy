"""Cross-space isolation fuzz -- the design/03 exit-gate property: a token for
space A reaches ZERO space-B rows through every tool, no matter what space-B ids
it crafts into the arguments. Spaces are the hard wall (design/06: "no query,
token, edge, or dedup comparison crosses a space, ever"); this test tries to
breach it across the full tool surface and asserts it cannot.

The breach model: the attacker holds a legitimate, fully-privileged (readwrite +
space_admin) space-A token and has somehow learned space-B's ids (node ids,
scope ids, pending/inbox ids). No tool takes a space argument, so the only lever
is smuggling a B id into a tool call. Every such attempt must resolve as
not-found / unwritable / empty -- never touch or reveal a B row. The RLS GUC
(engram.space_id), pinned from the token by db.transaction(), is the backstop
behind every tool.

Structure: one two-space fixture, then a breach test per tool, direct RLS
assertions on every RLS-covered table, a dedup-candidate-surface check, a reverse
(B->A) spot check, and positive controls proving A's own data IS reachable (so
the negatives aren't passing vacuously).
"""
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from engraphy.server.auth import AuthContext, ToolError
from engraphy.server.db import transaction
from engraphy.server.tools.admin import admin_grant, admin_scope_visibility
from engraphy.server.tools.inbox import inbox_review
from engraphy.server.tools.read import get, search, traverse
from engraphy.server.tools.write import link, resolve_duplicate, supersede, update, write

# A valid unit vector, NOT the zero vector: cosine distance (`<=>`, the operator
# the dedup-candidate query uses) against the zero vector is NaN, which makes
# `ORDER BY embedding <=> ...` nondeterministic under the HNSW index -- the ANN
# positive-control query could then return zero rows and flake. A real unit
# vector keeps every distance well-defined.
_UNIT_EMB = "[1," + ",".join(["0"] * 383) + "]"
_RLS_TABLES = ("nodes", "edges", "scopes", "scope_grants", "principals",
               "pending_writes", "inbox", "dedup_log", "metrics_rollup")


def _seed_space(cur, space_id, principal):
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, %s)", (space_id, space_id))
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name, role) VALUES (%s, %s, %s, 'space_admin')",
        (space_id, principal, principal),
    )
    cur.execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES (%s, 'widget', 'w', %s)",
        (space_id, Jsonb({"attrs": {"closed": False}})),
    )
    cur.execute(
        "INSERT INTO edge_types (space_id, name, description, bidirectional) "
        "VALUES (%s, 'relates_to', 'assoc', true)", (space_id,),
    )
    cur.execute(
        "INSERT INTO edge_rules (space_id, type, src_type, dst_type) "
        "VALUES (%s, 'relates_to', 'widget', 'widget')", (space_id,),
    )
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, %s, %s, %s, 'private')",
        (space_id, f"{space_id}-scope", "S", principal),
    )


def _seed_node(cur, space_id, principal, title, body):
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
        "embedding_model, source_client, author_principal) VALUES "
        "(%s, 'widget', %s, %s, %s, %s, %s, 'test-model', 'seed', %s) RETURNING id",
        (space_id, f"{space_id}-scope", title, body, Jsonb({}), _UNIT_EMB, principal),
    )
    return str(cur.fetchone()[0])


_ALL_TABLES = ("audit_log", "inbox", "pending_writes", "dedup_log", "edges", "nodes",
               "scope_grants", "scopes", "edge_rules", "edge_types", "node_types",
               "api_tokens", "config", "principals")


def _purge(cur, sid):
    """Child-first delete of everything in a space, then the space itself --
    used both as a defensive pre-clean (leftovers from an aborted run) and at
    teardown."""
    for table in _ALL_TABLES:
        cur.execute(f"DELETE FROM {table} WHERE space_id = %s", (sid,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (sid,))


@pytest.fixture
def two_spaces(conn, request):
    """Space A (attacker) and space B (target, holding a distinctive 'secret'
    node), fully seeded and committed so the async pool sees them."""
    # Sanitize to the scopes/spaces id charset (^[a-z0-9][a-z0-9-]{1,62}$):
    # parametrized names carry '[4]' etc. that the CHECK constraint rejects.
    import re
    tag = re.sub(r"[^a-z0-9]+", "-", request.node.name.lower()).strip("-")[:40].strip("-")
    a, b = f"isoa-{tag}"[:60], f"isob-{tag}"[:60]
    cur = conn.cursor()
    _purge(cur, a)
    _purge(cur, b)
    conn.commit()
    _seed_space(cur, a, "pa")
    _seed_space(cur, b, "pb")
    a_node = _seed_node(cur, a, "pa", "alpha note", "space A content")
    b_node1 = _seed_node(cur, b, "pb", "beta secret", "space B SECRET content")
    b_node2 = _seed_node(cur, b, "pb", "beta second", "space B other content")
    cur.execute(
        "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, 'relates_to')",
        (b, b_node1, b_node2),
    )
    cur.execute(
        "INSERT INTO pending_writes (space_id, author_principal, payload, expires_at) "
        "VALUES (%s, 'pb', %s, now() + interval '1 hour') RETURNING id",
        (b, Jsonb({"scope_id": f"{b}-scope", "type": "widget", "title": "x", "body": "y"})),
    )
    b_pending = str(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO inbox (space_id, scope_id, status, kind, payload) "
        "VALUES (%s, %s, 'pending', 'note', %s) RETURNING id",
        (b, f"{b}-scope", Jsonb({"title": "x", "body": "y"})),
    )
    b_inbox = str(cur.fetchone()[0])
    conn.commit()

    yield {
        "a": a, "b": b, "a_scope": f"{a}-scope", "b_scope": f"{b}-scope",
        "a_node": a_node, "b_node1": b_node1, "b_node2": b_node2,
        "b_pending": b_pending, "b_inbox": b_inbox,
    }

    _purge(cur, a)
    _purge(cur, b)
    conn.commit()


def _ctx_a(env):
    return AuthContext("tok-a", env["a"], "pa", "attacker-client", "readwrite")


# B's node CONTENT -- the actual secret. Distinct from B's ids: an id the
# attacker supplied as an argument echoing back (e.g. get()'s `missing` list)
# reveals nothing new, but any of B's node BODY/TITLE text appearing IS a breach.
_B_CONTENT = ("SECRET", "beta secret", "beta second", "space B")


def _assert_no_b_content(result):
    blob = repr(result)
    for s in _B_CONTENT:
        assert s not in blob, f"B content {s!r} leaked into {blob!r}"


def _assert_no_b_ids(result, env):
    """For calls where the attacker supplied NO B id -- any B id surfacing is a
    leak (unlike get/traverse where a supplied id legitimately echoes back)."""
    blob = repr(result)
    for key in ("b_node1", "b_node2", "b_pending", "b_inbox"):
        assert env[key] not in blob, f"{key} leaked into {blob!r}"


# ---- read-path breach vectors -----------------------------------------------

async def test_get_cross_space_ids_are_missing_not_returned(pool, two_spaces):
    env = two_spaces
    result = await get(pool, _ctx_a(env), {"ids": [env["b_node1"], env["b_node2"]]})
    assert result["nodes"] == []
    assert set(result["missing"]) == {env["b_node1"], env["b_node2"]}
    _assert_no_b_content(result)


async def test_traverse_from_cross_space_node_never_enters_b(pool, two_spaces):
    env = two_spaces
    try:
        result = await traverse(pool, _ctx_a(env), {"start_id": env["b_node1"], "direction": "both"})
    except ToolError as exc:
        assert exc.code in ("NOT_FOUND", "VALIDATION")
        return
    _assert_no_b_content(result)
    assert env["b_node2"] not in repr(result)


async def test_search_all_scope_never_surfaces_b_nodes(pool, two_spaces):
    env = two_spaces
    result = await search(pool, _ctx_a(env), {"scope": "all", "query": "space B SECRET content"})
    _assert_no_b_content(result)
    _assert_no_b_ids(result, env)


async def test_search_into_b_scope_cannot_read_it(pool, two_spaces):
    env = two_spaces
    try:
        result = await search(pool, _ctx_a(env), {"scope": env["b_scope"], "query": "secret"})
    except ToolError as exc:
        assert exc.code in ("NOT_FOUND", "SCOPE_UNKNOWN", "VALIDATION")
        return
    _assert_no_b_content(result)
    _assert_no_b_ids(result, env)


# ---- write-path breach vectors ----------------------------------------------

async def test_write_into_b_scope_is_scope_unknown(pool, two_spaces):
    env = two_spaces
    with pytest.raises(ToolError) as exc:
        await write(pool, _ctx_a(env), {
            "scope": env["b_scope"], "type": "widget", "title": "intrusion attempt",
            "body": "trying to write into space B", "attrs": {},
        })
    assert exc.value.code == "SCOPE_UNKNOWN"


async def test_update_cross_space_node_is_not_found(pool, two_spaces):
    env = two_spaces
    with pytest.raises(ToolError) as exc:
        await update(pool, _ctx_a(env), {"id": env["b_node1"], "title": "hijacked title"})
    assert exc.value.code == "NOT_FOUND"
    # and the row is untouched
    with psycopg.connect(_pg_url(), autocommit=True) as c:
        cur = c.cursor()
        cur.execute("SELECT title FROM nodes WHERE id = %s", (env["b_node1"],))
        assert cur.fetchone()[0] == "beta secret"


async def test_link_across_space_endpoints_is_not_found(pool, two_spaces):
    env = two_spaces
    with pytest.raises(ToolError) as exc:
        await link(pool, _ctx_a(env), {"edges": [
            {"type": "relates_to", "src_id": env["b_node1"], "dst_id": env["b_node2"]},
        ]})
    assert exc.value.code == "NOT_FOUND"


async def test_supersede_cross_space_node_is_not_found(pool, two_spaces):
    env = two_spaces
    with pytest.raises(ToolError) as exc:
        await supersede(pool, _ctx_a(env), {
            "old_id": env["b_node1"], "scope": env["a_scope"], "type": "widget",
            "title": "replacement", "body": "replacing a B node from A", "attrs": {},
        })
    assert exc.value.code == "NOT_FOUND"


async def test_resolve_duplicate_cross_space_pending_is_not_found(pool, two_spaces):
    env = two_spaces
    with pytest.raises(ToolError) as exc:
        await resolve_duplicate(pool, _ctx_a(env),
                                {"pending_id": env["b_pending"], "resolution": "distinct"})
    assert exc.value.code == "NOT_FOUND"


async def test_inbox_promote_cross_space_item_is_not_found(pool, two_spaces):
    env = two_spaces
    with pytest.raises(ToolError) as exc:
        await inbox_review(pool, _ctx_a(env), {"action": "promote", "id": env["b_inbox"]})
    assert exc.value.code in ("NOT_FOUND", "VALIDATION")


async def test_inbox_list_never_shows_b_items(pool, two_spaces):
    env = two_spaces
    result = await inbox_review(pool, _ctx_a(env), {"action": "list"})
    _assert_no_b_content(result)
    _assert_no_b_ids(result, env)


# ---- admin-tool breach vectors ----------------------------------------------

async def test_admin_scope_visibility_on_b_scope_is_not_found(pool, two_spaces):
    env = two_spaces
    with pytest.raises(ToolError) as exc:
        await admin_scope_visibility(pool, _ctx_a(env),
                                     {"scope_id": env["b_scope"], "visibility": "team-read"})
    assert exc.value.code == "NOT_FOUND"
    with psycopg.connect(_pg_url(), autocommit=True) as c:
        cur = c.cursor()
        cur.execute("SELECT visibility FROM scopes WHERE space_id = %s AND id = %s",
                    (env["b"], env["b_scope"]))
        assert cur.fetchone()[0] == "private"  # untouched


async def test_admin_grant_on_b_scope_is_rejected(pool, two_spaces):
    env = two_spaces
    with pytest.raises(ToolError) as exc:
        await admin_grant(pool, _ctx_a(env),
                          {"scope_id": env["b_scope"], "principal": "pa", "level": "read"})
    assert exc.value.code in ("VALIDATION", "NOT_FOUND")
    with psycopg.connect(_pg_url(), autocommit=True) as c:
        cur = c.cursor()
        cur.execute("SELECT count(*) FROM scope_grants WHERE space_id = %s", (env["b"],))
        assert cur.fetchone()[0] == 0  # no grant leaked into B


# ---- direct RLS backstop ----------------------------------------------------

async def test_rls_a_transaction_sees_zero_b_rows_in_every_table(pool, two_spaces):
    env = two_spaces
    async with transaction(pool, env["a"], "pa") as c:
        for table in _RLS_TABLES:
            cur = await c.execute(f"SELECT count(*) FROM {table} WHERE space_id = %s", (env["b"],))
            (count,) = await cur.fetchone()
            assert count == 0, f"space A saw {count} rows of B in {table}"


async def test_dedup_candidate_ann_query_cannot_reach_b(pool, two_spaces):
    """The dedup write path's candidate query is an RLS-gated nearest-neighbour
    SELECT on nodes. Even a byte-identical embedding in B is invisible: a vector
    search under A's identity returns only A rows."""
    env = two_spaces
    async with transaction(pool, env["a"], "pa") as c:
        cur = await c.execute(
            "SELECT id FROM nodes WHERE type = 'widget' ORDER BY embedding <=> %s LIMIT 10",
            (_UNIT_EMB,),
        )
        ids = {str(r[0]) for r in await cur.fetchall()}
    assert env["b_node1"] not in ids and env["b_node2"] not in ids
    assert env["a_node"] in ids  # positive control: A's own node IS a candidate


# ---- reverse direction spot check -------------------------------------------

async def test_reverse_b_token_cannot_get_a_node(pool, two_spaces):
    env = two_spaces
    ctx_b = AuthContext("tok-b", env["b"], "pb", "b-client", "readwrite")
    result = await get(pool, ctx_b, {"ids": [env["a_node"]]})
    assert result["nodes"] == []
    assert result["missing"] == [env["a_node"]]


# ---- positive controls (the wall is not just "everything fails") ------------

async def test_positive_control_a_can_get_its_own_node(pool, two_spaces):
    env = two_spaces
    result = await get(pool, _ctx_a(env), {"ids": [env["a_node"]]})
    assert [n["id"] for n in result["nodes"]] == [env["a_node"]]
    assert result["missing"] == []


async def test_positive_control_a_can_write_its_own_scope(pool, two_spaces):
    env = two_spaces
    result = await write(pool, _ctx_a(env), {
        "scope": env["a_scope"], "type": "widget", "title": "legit A write",
        "body": "this belongs in space A", "attrs": {},
    })
    assert result["outcome"] in ("inserted", "merged", "needs_confirmation")


def _pg_url():
    from engraphy.tests.conftest import DATABASE_URL
    return DATABASE_URL


# A crafted-random-id sweep: fabricated UUIDs never resolve to anything, in any
# space -- a belt-and-braces fuzz over get with ids that don't exist at all.
@pytest.mark.parametrize("_i", range(5))
async def test_fuzz_random_uuids_never_resolve(pool, two_spaces, _i):
    env = two_spaces
    ids = [str(uuid.uuid4()) for _ in range(4)]
    result = await get(pool, _ctx_a(env), {"ids": ids})
    assert result["nodes"] == []
    assert set(result["missing"]) == set(ids)
