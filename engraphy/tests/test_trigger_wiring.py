"""nodes_validate_attrs / edges_validate / nodes_touch trigger wiring —
design/implementation/attr-spec-interpreter-plan.md §Test plan, row
`test_trigger_wiring.py` ("trigger rejects with ERRCODE 23514 and the joined
message; valid rows insert"), plus design/01 §Trigger enforcement's
edges_validate and nodes_touch. Raw SQL against a live Postgres.
"""

import psycopg
import pytest
from psycopg.types.json import Jsonb

from conftest import DATABASE_URL, bootstrap_space, insert_node


def test_valid_node_inserts(conn):
    ids = bootstrap_space(conn)
    node_id = insert_node(conn, ids["space_id"], ids["scope_id"], attrs={"status": "open"})
    assert node_id is not None


def test_invalid_attrs_rejected_with_23514_and_joined_message(conn):
    ids = bootstrap_space(conn)
    with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
        insert_node(conn, ids["space_id"], ids["scope_id"], attrs={"status": "nope"})
    assert exc_info.value.sqlstate == "23514"
    assert "attrs.status must be one of open|closed" in str(exc_info.value)


def test_unknown_node_type_rejected(conn):
    # The trigger's explicit "spec IS NULL" check fires before the (space_id, type)
    # FK constraint would -- a clearer CheckViolation, not the raw FK violation.
    # The FK is still the backstop if the trigger is ever dropped (design/01: "two
    # independent layers"); this doesn't weaken that, it just wins the race.
    ids = bootstrap_space(conn)
    with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
        insert_node(conn, ids["space_id"], ids["scope_id"], node_type="ghost")
    assert "unknown node type ghost" in str(exc_info.value)


def test_nodes_touch_sets_updated_at():
    # now() is transaction-start time, not statement time -- exercised across
    # two real transactions (two connections) to match how the GUC-wrapper
    # protocol actually runs each tool call as its own transaction; a
    # single-transaction test would trivially see created_at == updated_at
    # forever, regardless of whether the trigger fires.
    with psycopg.connect(DATABASE_URL) as c1:
        ids = bootstrap_space(c1)
        node_id = insert_node(c1, ids["space_id"], ids["scope_id"])
        c1.commit()
        cur = c1.cursor()
        cur.execute("SELECT created_at, updated_at FROM nodes WHERE id = %s", (node_id,))
        created_at, updated_at = cur.fetchone()
        assert created_at == updated_at  # first insert: identical

        with psycopg.connect(DATABASE_URL) as c2:
            c2.cursor().execute("SELECT pg_sleep(0.01)")
            c2.cursor().execute(
                "UPDATE nodes SET title = %s WHERE id = %s", ("A different title", node_id)
            )
            c2.commit()

        cur.execute("SELECT updated_at FROM nodes WHERE id = %s", (node_id,))
        (new_updated_at,) = cur.fetchone()
        assert new_updated_at > updated_at

        cur.execute("DELETE FROM nodes WHERE id = %s", (node_id,))
        cur.execute("DELETE FROM scopes WHERE space_id = %s", (ids["space_id"],))
        cur.execute("DELETE FROM edge_rules WHERE space_id = %s", (ids["space_id"],))
        cur.execute("DELETE FROM edge_types WHERE space_id = %s", (ids["space_id"],))
        cur.execute("DELETE FROM node_types WHERE space_id = %s", (ids["space_id"],))
        cur.execute("DELETE FROM principals WHERE space_id = %s", (ids["space_id"],))
        cur.execute("DELETE FROM spaces WHERE id = %s", (ids["space_id"],))
        c1.commit()


def test_nodes_touch_canonical_id_same_space_and_type(conn):
    ids = bootstrap_space(conn)
    canonical_id = insert_node(conn, ids["space_id"], ids["scope_id"])
    merged_id = insert_node(conn, ids["space_id"], ids["scope_id"])
    cur = conn.cursor()
    cur.execute(
        "UPDATE nodes SET status = 'merged', canonical_id = %s WHERE id = %s",
        (canonical_id, merged_id),
    )  # same space, same type: must succeed


def test_nodes_touch_rejects_cross_type_canonical(conn):
    ids = bootstrap_space(conn)
    cur = conn.cursor()
    other_spec = Jsonb({"attrs": {"closed": True}})
    cur.execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES (%s, %s, %s, %s)",
        (ids["space_id"], "gadget", "Another type.", other_spec),
    )
    canonical_id = insert_node(conn, ids["space_id"], ids["scope_id"], node_type="gadget", attrs={})
    merged_id = insert_node(conn, ids["space_id"], ids["scope_id"])  # type=widget
    with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
        cur.execute(
            "UPDATE nodes SET status = 'merged', canonical_id = %s WHERE id = %s",
            (canonical_id, merged_id),
        )
    assert "canonical_id target must be same space and type" in str(exc_info.value)


def test_edges_validate_accepts_rule_match(conn):
    ids = bootstrap_space(conn)
    a = insert_node(conn, ids["space_id"], ids["scope_id"])
    b = insert_node(conn, ids["space_id"], ids["scope_id"])
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, %s)",
        (ids["space_id"], a, b, ids["edge_type"]),
    )


def test_edges_validate_rejects_missing_rule(conn):
    ids = bootstrap_space(conn)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO edge_types (space_id, name, description) VALUES (%s, %s, %s)",
        (ids["space_id"], "unruled", "No edge_rules row for this one."),
    )
    a = insert_node(conn, ids["space_id"], ids["scope_id"])
    b = insert_node(conn, ids["space_id"], ids["scope_id"])
    with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
        cur.execute(
            "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, %s)",
            (ids["space_id"], a, b, "unruled"),
        )
    assert "edge_rules: no rule for" in str(exc_info.value)


def test_edges_validate_rejects_cross_space(conn):
    ids1 = bootstrap_space(conn, space_id="t1", principal_id="p1")
    ids2 = bootstrap_space(conn, space_id="t2", principal_id="p1")
    a = insert_node(conn, ids1["space_id"], ids1["scope_id"])
    b = insert_node(conn, ids2["space_id"], ids2["scope_id"])
    cur = conn.cursor()
    with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
        cur.execute(
            "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, %s)",
            (ids1["space_id"], a, b, ids1["edge_type"]),
        )
    assert "cross-space edge is not permitted" in str(exc_info.value)


def test_nodes_touch_skips_recall_only_update():
    """Migration 0012: a recall-only update (recall_count / last_recalled_at)
    must NOT move updated_at -- a recall is a read, not an edit
    (QUESTIONS.md recall-stats-vs-nodes-touch, Devon option a). Two connections,
    same reason as test_nodes_touch_sets_updated_at: now() is transaction-start."""
    with psycopg.connect(DATABASE_URL) as c1:
        ids = bootstrap_space(c1)
        node_id = insert_node(c1, ids["space_id"], ids["scope_id"])
        c1.commit()
        cur = c1.cursor()
        cur.execute("SELECT updated_at, recall_count FROM nodes WHERE id = %s", (node_id,))
        updated_at, recall_count = cur.fetchone()
        assert recall_count == 0

        with psycopg.connect(DATABASE_URL) as c2:
            c2.cursor().execute("SELECT pg_sleep(0.01)")
            c2.cursor().execute(
                "UPDATE nodes SET recall_count = recall_count + 1, last_recalled_at = now() "
                "WHERE id = %s",
                (node_id,),
            )
            c2.commit()

        cur.execute(
            "SELECT updated_at, recall_count, last_recalled_at FROM nodes WHERE id = %s", (node_id,)
        )
        new_updated_at, new_recall_count, last_recalled_at = cur.fetchone()
        assert new_recall_count == 1, "recall_count bumped"
        assert last_recalled_at is not None, "last_recalled_at set"
        assert new_updated_at == updated_at, "updated_at UNTOUCHED by a recall-only update"

        cur.execute("DELETE FROM nodes WHERE id = %s", (node_id,))
        cur.execute("DELETE FROM scopes WHERE space_id = %s", (ids["space_id"],))
        cur.execute("DELETE FROM edge_rules WHERE space_id = %s", (ids["space_id"],))
        cur.execute("DELETE FROM edge_types WHERE space_id = %s", (ids["space_id"],))
        cur.execute("DELETE FROM node_types WHERE space_id = %s", (ids["space_id"],))
        cur.execute("DELETE FROM principals WHERE space_id = %s", (ids["space_id"],))
        cur.execute("DELETE FROM spaces WHERE id = %s", (ids["space_id"],))
        c1.commit()
