"""RLS traps — visibility-and-rls-plan.md §Traps and §Test plan rows not
covered by test_visibility_matrix.py or test_db_guc_protocol.py:

- FORCE audit: every RLS-covered table has relrowsecurity AND relforcerowsecurity.
- RLS probe: a filter-free `SELECT * FROM nodes` under one space's identity
  returns zero rows from another space, and zero rows from a teammate's
  private scope within one team space.
- Existence hiding: a non-existent id and an existing-but-unreadable id both
  come back empty through the same query shape (never an error, never a
  different code path) -- the structural half of "existence is information";
  full not-found translation is a tool-layer (E2) concern.
- Definer-guard (trap 2): dropping SECURITY DEFINER from
  engram_readable_scopes() must break the suite (RLS-on-scopes recursion),
  proving the test suite would actually catch a well-meaning "hardening" revert.
"""

import psycopg
import pytest
from psycopg.types.json import Jsonb

from conftest import APP_ROLE, bootstrap_space, insert_node, set_identity

_RLS_TABLES = ("nodes", "edges", "scopes", "scope_grants", "principals", "pending_writes", "inbox", "dedup_log", "metrics_rollup")


def test_force_audit(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relname = ANY(%s)",
        (list(_RLS_TABLES),),
    )
    rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    assert set(rows) == set(_RLS_TABLES)
    for table, (enabled, forced) in rows.items():
        assert enabled and forced, f"{table}: relrowsecurity={enabled} relforcerowsecurity={forced}"


def test_rls_probe_cross_space_filter_free_select(conn):
    ids_author = bootstrap_space(conn, space_id="author-probe", principal_id="author")
    ids_dad = bootstrap_space(conn, space_id="alex-probe", principal_id="alex")
    insert_node(conn, ids_author["space_id"], ids_author["scope_id"], attrs={"status": "open"})
    insert_node(conn, ids_dad["space_id"], ids_dad["scope_id"], attrs={"status": "open"})

    cur = conn.cursor()
    cur.execute(f"SET LOCAL ROLE {APP_ROLE}")
    set_identity(conn, "alex-probe", "alex")

    cur.execute("SELECT space_id FROM nodes")  # deliberately filter-free
    seen_spaces = {r[0] for r in cur.fetchall()}
    assert seen_spaces == {"alex-probe"}, f"cross-space leak: saw {seen_spaces}"

    cur.execute("SELECT id FROM scopes")
    cur.execute("SELECT space_id FROM edges")
    cur.execute("SELECT space_id FROM principals")


def test_rls_probe_teammate_private_scope_within_one_space(conn):
    space_id = "team-probe"
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, %s)", (space_id, "Team"))
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'alice', 'A'), (%s, 'bob', 'B')",
        (space_id, space_id),
    )
    cur.execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES (%s, 'widget', 'w', %s)",
        (space_id, Jsonb({"attrs": {"closed": False}})),
    )
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, 'alice-private', 'A priv', 'alice', 'private')",
        (space_id,),
    )
    insert_node(conn, space_id, "alice-private", node_type="widget", attrs={}, author_principal="alice")

    cur.execute(f"SET LOCAL ROLE {APP_ROLE}")
    set_identity(conn, space_id, "bob")

    cur.execute("SELECT * FROM nodes")  # deliberately filter-free
    assert cur.fetchall() == [], "bob saw alice's private-scope node via a filter-free SELECT"


def test_existence_hiding_nonexistent_vs_unreadable(conn):
    ids = bootstrap_space(conn, space_id="exist-probe")
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'outsider', 'O')",
        (ids["space_id"],),
    )
    real_but_private_id = insert_node(conn, ids["space_id"], ids["scope_id"], attrs={"status": "open"})

    cur.execute(f"SET LOCAL ROLE {APP_ROLE}")
    set_identity(conn, ids["space_id"], "outsider")

    cur.execute("SELECT * FROM nodes WHERE id = %s", (real_but_private_id,))
    unreadable_result = cur.fetchall()

    cur.execute("SELECT * FROM nodes WHERE id = gen_random_uuid()")
    nonexistent_result = cur.fetchall()

    assert unreadable_result == [] == nonexistent_result


def test_definer_guard_regression(conn):
    """Dropping SECURITY DEFINER must break the suite -- proves this suite
    would catch the regression, not merely that the flag is set today."""
    cur = conn.cursor()
    cur.execute("SAVEPOINT definer_guard")
    try:
        cur.execute(
            "ALTER FUNCTION engram_readable_scopes() SECURITY INVOKER"
        )
        ids = bootstrap_space(conn, space_id="definer-probe")
        cur.execute(f"SET LOCAL ROLE {APP_ROLE}")
        set_identity(conn, ids["space_id"], ids["principal_id"])
        with pytest.raises(psycopg.Error):
            # Without DEFINER, evaluating scopes' own RLS policy recurses
            # into calling the function again to evaluate scopes' policy --
            # infinite recursion.
            cur.execute("SELECT * FROM scopes")
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT definer_guard")
