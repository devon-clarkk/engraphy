"""Visibility matrix — design/06 §Testing, visibility-and-rls-plan.md §Test
plan row `visibility_matrix` (generated): every (visibility, grant, actor) x
operation combination from fixtures/visibility_matrix.py's generator, tested
against RAW SQL (psql-level truth before server truth), per the plan's build
order step 4. One Postgres connection per case: bootstrap as the superuser
(sees everything, sets up the scenario), then `SET ROLE engraphy_app` to drop
to the actual non-superuser, non-BYPASSRLS role for the check itself -- a
second connection would not see the first's uncommitted setup rows, and
committing between them would require manual cleanup per case.
"""

import pathlib
import sys
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, str(pathlib.Path(__file__).parent / "fixtures"))
from visibility_matrix import generate_cases

from conftest import APP_ROLE, DATABASE_URL, insert_node, set_identity

CASES = list(generate_cases())


def _bootstrap(super_conn, case, space_id):
    cur = super_conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, %s)", (space_id, "Matrix"))

    owner, actor = "owner-principal", "actor-principal"
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name, role) VALUES (%s, %s, 'Owner', 'member')",
        (space_id, owner),
    )
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name, role) VALUES (%s, %s, 'Actor', %s)",
        (space_id, actor, case["actor_role"]),
    )

    cur.execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES (%s, 'widget', 'w', %s)",
        (space_id, Jsonb({"attrs": {"closed": False}})),
    )
    cur.execute(
        "INSERT INTO edge_types (space_id, name, description, bidirectional) VALUES (%s, 'relates_to', 'r', true)",
        (space_id,),
    )
    cur.execute(
        "INSERT INTO edge_rules (space_id, type, src_type, dst_type) VALUES (%s, 'relates_to', 'widget', 'widget')",
        (space_id,),
    )

    setup = case["setup"]
    scope_owner = owner if setup["scope_owner_principal"] == "owner-principal" else actor
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, 'scope-x', 'X', %s, %s)",
        (space_id, scope_owner, setup["scope_visibility"]),
    )
    if setup["scope_grant"]:
        cur.execute(
            "INSERT INTO scope_grants (space_id, scope_id, principal, level) VALUES (%s, 'scope-x', %s, %s)",
            (space_id, actor, setup["scope_grant"]["level"]),
        )
    # actor's own always-owned-and-writable scope: the fixed second endpoint
    # for edge operations (visibility_matrix.py's derivation).
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, 'scope-own', 'Own', %s, 'private')",
        (space_id, actor),
    )

    node_x = insert_node(super_conn, space_id, "scope-x", node_type="widget", attrs={}, author_principal=owner)
    node_own = insert_node(super_conn, space_id, "scope-own", node_type="widget", attrs={}, author_principal=actor)

    if case["operation"] == "edge_read_both_ends":
        # Pre-created as superuser: this operation tests READING an existing
        # edge, not creating one.
        cur.execute(
            "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, 'relates_to')",
            (space_id, node_own, node_x),
        )

    return {"owner": owner, "actor": actor, "node_x": node_x, "node_own": node_own}


def _run_operation(conn, cur, case, ctx, space_id):
    op = case["operation"]
    if op in ("select", "dedup_candidate", "traverse_through"):
        # All three reduce to the same predicate against this fixed pairing
        # (visibility_matrix.py's derivation) -- same query shape is correct.
        cur.execute("SELECT 1 FROM nodes WHERE id = %s", (ctx["node_x"],))
        return cur.fetchone() is not None
    if op == "insert":
        try:
            insert_node(
                conn, space_id, "scope-x", node_type="widget", attrs={}, author_principal=ctx["actor"]
            )
            return True
        except psycopg.Error:
            return False
    if op == "update":
        try:
            cur.execute(
                "UPDATE nodes SET title = 'matrix updated title' WHERE id = %s", (ctx["node_x"],)
            )
            return cur.rowcount > 0
        except psycopg.Error:
            return False
    if op == "edge_create_one_end":
        try:
            cur.execute(
                "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, 'relates_to')",
                (space_id, ctx["node_own"], ctx["node_x"]),
            )
            return True
        except psycopg.Error:
            return False
    if op == "edge_read_both_ends":
        cur.execute(
            "SELECT 1 FROM edges WHERE src_id = %s AND dst_id = %s", (ctx["node_own"], ctx["node_x"])
        )
        return cur.fetchone() is not None
    raise ValueError(f"unhandled operation: {op}")  # pragma: no cover


@pytest.mark.parametrize("case", CASES, ids=[c["case_id"] for c in CASES])
def test_matrix_case(case):
    space_id = f"m{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DATABASE_URL, autocommit=False) as conn:
        try:
            ctx = _bootstrap(conn, case, space_id)

            cur = conn.cursor()
            cur.execute(f"SET ROLE {APP_ROLE}")
            set_identity(conn, space_id, ctx["actor"])

            allow = _run_operation(conn, cur, case, ctx, space_id)
        finally:
            conn.rollback()

    assert allow == case["expect_allow"], (
        f"{case['case_id']}: expected allow={case['expect_allow']}, got {allow}"
    )
