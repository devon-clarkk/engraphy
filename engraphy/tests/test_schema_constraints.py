"""design/01 §Testing and validation -- rows not covered by
test_trigger_wiring.py / test_attr_spec_pg.py / test_pack_apply.py:

- "Two spaces, same type names, different attr-specs: each space enforced
  against its own spec."
- "Concurrent writers: two connections x 500 mixed inserts across two
  spaces -- zero errors, zero lost rows, zero cross-space leakage."
"""

import concurrent.futures
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from conftest import DATABASE_URL, insert_node


def test_same_type_name_different_attr_spec_per_space(conn):
    cur = conn.cursor()
    for space_id, required_key in (("space-a", "severity"), ("space-b", "urgency")):
        cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, %s)", (space_id, space_id))
        cur.execute(
            "INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p1', 'P')", (space_id,)
        )
        spec = Jsonb({"attrs": {"required": {required_key: {"type": "string"}}, "closed": True}})
        cur.execute(
            "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES (%s, 'error', 'e', %s)",
            (space_id, spec),
        )
        cur.execute(
            "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
            "VALUES (%s, 'scope1', 'S', 'p1', 'private')",
            (space_id,),
        )

    # space-a requires "severity" -- attrs={"urgency": "x"} must be rejected
    # (severity missing) even though "urgency" is exactly what space-b wants.
    cur.execute("SAVEPOINT s1")
    with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
        insert_node(conn, "space-a", "scope1", node_type="error", attrs={"urgency": "x"})
    assert "attrs.severity is required" in str(exc_info.value)
    cur.execute("ROLLBACK TO SAVEPOINT s1")

    # space-b requires "urgency" -- the same shape rejected for the opposite reason.
    cur.execute("SAVEPOINT s2")
    with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
        insert_node(conn, "space-b", "scope1", node_type="error", attrs={"severity": "x"})
    assert "attrs.urgency is required" in str(exc_info.value)
    cur.execute("ROLLBACK TO SAVEPOINT s2")

    # each space's own correct shape succeeds.
    id_a = insert_node(conn, "space-a", "scope1", node_type="error", attrs={"severity": "high"})
    id_b = insert_node(conn, "space-b", "scope1", node_type="error", attrs={"urgency": "now"})
    assert id_a and id_b


def _writer(space_id: str, n: int) -> list[str]:
    """Runs in a worker thread with its OWN connection (psycopg connections
    are not thread-safe to share). Returns the ids it successfully inserted."""
    ids = []
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        cur = c.cursor()
        for _ in range(n):
            embedding = "[" + ",".join(["0"] * 384) + "]"
            row_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO nodes (id, space_id, type, scope_id, title, body, attrs, "
                "embedding, embedding_model, source_client, author_principal) "
                "VALUES (%s, %s, 'widget', 'scope1', 'Concurrent node title', 'Concurrent node body.', "
                "'{}', %s, 'test-model', 'pytest', 'p1') RETURNING id",
                (row_id, space_id, embedding),
            )
            ids.append(str(cur.fetchone()[0]))
    return ids


def test_concurrent_writers_two_spaces_zero_leakage(conn):
    cur = conn.cursor()
    for space_id in ("conc-a", "conc-b"):
        cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, %s)", (space_id, space_id))
        cur.execute(
            "INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p1', 'P')", (space_id,)
        )
        cur.execute(
            "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES (%s, 'widget', 'w', %s)",
            (space_id, Jsonb({"attrs": {"closed": False}})),
        )
        cur.execute(
            "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
            "VALUES (%s, 'scope1', 'S', 'p1', 'private')",
            (space_id,),
        )
    conn.commit()  # must be visible to the worker threads' own connections

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(_writer, "conc-a", 500)
            fut_b = pool.submit(_writer, "conc-b", 500)
            ids_a, ids_b = fut_a.result(), fut_b.result()

        assert len(ids_a) == 500 and len(ids_b) == 500
        assert set(ids_a).isdisjoint(ids_b)  # zero lost rows / zero id collisions

        with psycopg.connect(DATABASE_URL, autocommit=True) as verify_conn:
            vcur = verify_conn.cursor()
            vcur.execute("SELECT count(*) FROM nodes WHERE space_id = 'conc-a'")
            assert vcur.fetchone()[0] == 500
            vcur.execute("SELECT count(*) FROM nodes WHERE space_id = 'conc-b'")
            assert vcur.fetchone()[0] == 500
            # zero cross-space leakage: every conc-a id's row actually has space_id=conc-a
            vcur.execute(
                "SELECT count(*) FROM nodes WHERE id = ANY(%s) AND space_id <> 'conc-a'", (ids_a,)
            )
            assert vcur.fetchone()[0] == 0
    finally:
        with psycopg.connect(DATABASE_URL, autocommit=True) as cleanup_conn:
            ccur = cleanup_conn.cursor()
            for space_id in ("conc-a", "conc-b"):
                ccur.execute("DELETE FROM nodes WHERE space_id = %s", (space_id,))
                ccur.execute("DELETE FROM scopes WHERE space_id = %s", (space_id,))
                ccur.execute("DELETE FROM node_types WHERE space_id = %s", (space_id,))
                ccur.execute("DELETE FROM principals WHERE space_id = %s", (space_id,))
                ccur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
