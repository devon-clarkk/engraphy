"""Real, unmocked subprocess round-trip for migrate.py/verify_restore.py's
`pg_dump`/`pg_restore`/`dbmate` calls -- test_migrate.py and
test_verify_restore.py monkeypatch every one of these (deliberately, for
speed and because this dev environment has no native pg_dump/pg_restore --
see their module docstrings), which means the literal subprocess argv this
code ships has never been exercised for real anywhere else in this suite.

Skipped when pg_dump/pg_restore/dbmate aren't on PATH (true of local Windows
dev environments per this repo's tooling notes); runs for real on CI
(ubuntu-latest, which ships pg_dump/pg_restore, and the workflow already
installs dbmate) against the same pgvector/pg16 service container every
other test uses. This is deliberately the one place client-vs-server
Postgres VERSION mismatches would surface (e.g. an older `pg_dump` against a
newer server's custom-format dump) -- exactly the kind of bug that only
shows up by actually running the binaries, never by mocking them.
"""
import pathlib
import shutil
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from conftest import DATABASE_URL
from engraphy.admin import migrate, verify_restore

MIGRATIONS_DIR = pathlib.Path(__file__).parents[2] / "engraphy" / "db" / "migrations"

pytestmark = pytest.mark.skipif(
    not (shutil.which("pg_dump") and shutil.which("pg_restore") and shutil.which("dbmate")),
    reason="pg_dump/pg_restore/dbmate not all on PATH -- this environment can't run the real "
           "subprocess round-trip (see module docstring); CI always can.",
)


@pytest.fixture
def scratch_db():
    """A fresh, uniquely-named DB with pgvector installed and the real
    migrations applied via a real `dbmate up` -- not the fixture-driven
    schema this suite's other tests get from the shared test DB."""
    name = f"engraphy_shell_it_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        cur = c.cursor()
        cur.execute(f'CREATE DATABASE "{name}"')
    conninfo = DATABASE_URL.rsplit("/", 1)[0] + f"/{name}?sslmode=disable"
    try:
        with psycopg.connect(conninfo, autocommit=True) as c:
            c.cursor().execute("CREATE EXTENSION IF NOT EXISTS vector")
        output = migrate.dbmate_up(conninfo, MIGRATIONS_DIR)
        expected = migrate.expected_schema_version(MIGRATIONS_DIR)
        assert expected in output or output == "", f"dbmate up against a fresh scratch DB failed oddly: {output!r}"
        yield conninfo
    finally:
        with psycopg.connect(DATABASE_URL, autocommit=True) as c:
            c.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _seed_one_node(conninfo: str) -> str:
    sentinel_id = str(uuid.uuid4())
    with psycopg.connect(conninfo, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO spaces (id, display_name) VALUES ('shellit', 'Shell IT')")
        cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES ('shellit', 'p1', 'P1')")
        cur.execute(
            "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES "
            "('shellit', 'note', 'a note', %s)", (Jsonb({"attrs": {"closed": False}}),))
        cur.execute(
            "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
            "VALUES ('shellit', 'scope1', 'Scope', 'p1', 'private')")
        cur.execute(
            "INSERT INTO nodes (id, space_id, type, scope_id, title, body, attrs, embedding, "
            "embedding_model, source_client, author_principal) VALUES "
            "(%s, 'shellit', 'note', 'scope1', 'sentinel title', 'sentinel body', '{}', "
             "'[" + ",".join(["0"] * 384) + "]', 'test', 'pytest', 'p1')",
            (sentinel_id,),
        )
    return sentinel_id


def test_real_pre_dump_and_verify_restore_round_trip(scratch_db, tmp_path):
    """The exact sequence engraphy-admin migrate/verify-restore run: dbmate_up
    (already done by the scratch_db fixture) -> seed data -> pre_dump (real
    pg_dump) -> verify_restore.run (real pg_restore + the full assertion
    suite) against a SEPARATE scratch DB it creates and drops itself."""
    conninfo = scratch_db
    sentinel_id = _seed_one_node(conninfo)

    dump_path = migrate.pre_dump(conninfo, tmp_path)
    assert dump_path.exists()
    assert dump_path.stat().st_size > 0

    log = verify_restore.run(
        dump_path, conninfo, migrations_dir=MIGRATIONS_DIR, sentinel_id=sentinel_id)

    expected = migrate.expected_schema_version(MIGRATIONS_DIR)
    assert any(f"schema_migrations at '{expected}'" in line for line in log)
    assert any("1 space(s) restored" in line for line in log)
    assert any("constraint probe ok: spaces.id pattern CHECK" in line for line in log)
    assert any("constraint probe ok: nodes.body length CHECK" in line for line in log)
    assert any("constraint probe ok: nodes.status/canonical_id CHECK" in line for line in log)
    # The seeded space is a pre-convention one (hand-inserted, no
    # `sentinel.node_id` in config), so resolution falls through to the
    # operator-supplied --sentinel-id -- and the log names which source it used.
    assert any(f"sentinel {sentinel_id!r} retrieved from --sentinel-id" in line for line in log)


def test_real_smoke_test_after_real_dbmate_up(scratch_db):
    """migrate.smoke_test's own DB assertions, against a DB that really went
    through dbmate_up in this same test run (not a pre-existing fixture)."""
    lines = migrate.smoke_test(scratch_db, MIGRATIONS_DIR, healthz_url=None)
    expected = migrate.expected_schema_version(MIGRATIONS_DIR)
    assert any(f"schema_migrations at '{expected}' (matches expected)" in line for line in lines)
