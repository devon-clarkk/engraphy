"""engraphy.admin.verify_restore -- design/04 s.Backup contract: schema version
match, per-space row counts, constraint probes, sentinel retrieval.

`_restore()` (the actual pg_restore subprocess call) is monkeypatched in
every test here to apply the real migrations via dbmate instead -- for the
assertion suite's own correctness, a scratch DB with the real schema loaded
by dbmate is functionally identical to one loaded by pg_restore from a dump
of that same schema, and this environment has no native pg_restore binary to
call. The literal `pg_restore` subprocess invocation is exercised for real by
CI (Linux, real Postgres client tools on PATH).

The two tests using `restore_via_dbmate` need a real `dbmate` on PATH (same
requirement as test_shell_integration.py's guard, and for the same reason:
`shutil.which` is how production code (migrate.py) finds it too, so this
mirrors that rather than assuming a fixed install location) -- skipped when
absent, e.g. a dev box that never put dbmate on PATH.
"""
import pathlib
import shutil
import subprocess

import psycopg
import pytest
from psycopg.types.json import Jsonb

from conftest import DATABASE_URL
from engraphy.admin import verify_restore

MIGRATIONS_DIR = pathlib.Path(__file__).parents[2] / "engraphy" / "db" / "migrations"
DBMATE = shutil.which("dbmate")


def _as_url(conninfo: str) -> str:
    """dbmate's --url wants a postgres:// URL; verify_restore._admin_conninfo
    returns whatever libpq keyword/value form psycopg.conninfo.make_conninfo
    produces, which dbmate doesn't parse. Only needed for this test's own
    real-dbmate stand-in for pg_restore."""
    parsed = psycopg.conninfo.conninfo_to_dict(conninfo)
    return (f"postgres://{parsed['user']}:{parsed['password']}@{parsed['host']}:"
            f"{parsed['port']}/{parsed['dbname']}?sslmode={parsed.get('sslmode', 'disable')}")


def _dbmate_up(conninfo: str) -> None:
    result = subprocess.run(
        [str(DBMATE), "--migrations-dir", str(MIGRATIONS_DIR), "--url", _as_url(conninfo), "up"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def restore_via_dbmate(monkeypatch):
    """Replaces the real pg_restore call with a real `dbmate up` against the
    scratch DB verify_restore.run() already created -- see module docstring."""
    if DBMATE is None:
        pytest.skip("dbmate not found on PATH -- can't stand in for pg_restore in this environment")
    def fake_restore(dump_path, scratch_conninfo):
        del dump_path
        _dbmate_up(scratch_conninfo)
    monkeypatch.setattr(verify_restore, "_restore", fake_restore)


def test_admin_conninfo_swaps_dbname_only():
    swapped = verify_restore._admin_conninfo(DATABASE_URL, "some_other_db")
    parsed = psycopg.conninfo.conninfo_to_dict(swapped)
    assert parsed["dbname"] == "some_other_db"
    original = psycopg.conninfo.conninfo_to_dict(DATABASE_URL)
    assert parsed["host"] == original["host"]
    assert parsed["port"] == original["port"]


def test_scratch_database_created_and_dropped():
    seen_names = []
    with verify_restore._scratch_database(DATABASE_URL) as (scratch_conninfo, name):
        seen_names.append(name)
        with psycopg.connect(scratch_conninfo, autocommit=True) as c:
            c.cursor().execute("SELECT 1")  # reachable while the context is open
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        cur = c.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (seen_names[0],))
        assert cur.fetchone() is None  # dropped on exit


def test_scratch_database_dropped_even_on_exception():
    name_holder = []
    with pytest.raises(RuntimeError):
        with verify_restore._scratch_database(DATABASE_URL) as (_conninfo, name):
            name_holder.append(name)
            raise RuntimeError("boom")
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        cur = c.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name_holder[0],))
        assert cur.fetchone() is None


def test_run_full_suite_against_empty_schema_reports_zero_spaces_and_raises(restore_via_dbmate, tmp_path):
    """A freshly-migrated, unpopulated DB has zero spaces -- run() must treat
    that as a corrupt/empty-looking dump and refuse, not silently report success."""
    dump_path = tmp_path / "fake.pgdump"
    dump_path.write_bytes(b"not a real dump -- _restore is monkeypatched")
    with pytest.raises(verify_restore.VerifyRestoreError, match="zero spaces"):
        verify_restore.run(dump_path, DATABASE_URL, migrations_dir=MIGRATIONS_DIR)


def test_run_full_suite_against_populated_schema_passes_with_sentinel(restore_via_dbmate, tmp_path, monkeypatch):
    """End-to-end: restore (faked via dbmate) -> schema check -> row counts ->
    constraint probes -> sentinel retrieval, all against a real scratch DB
    populated with one space/type/scope/node, exercising the full run()
    sequence exactly as the CLI verb calls it. The sentinel id is generated
    up front (the real operator workflow: a canary node minted ahead of
    time, whose known id is passed to --sentinel-id) so it's known before
    run() ever starts."""
    dump_path = tmp_path / "fake.pgdump"
    dump_path.write_bytes(b"not a real dump -- _restore is monkeypatched")
    sentinel_id = "11111111-1111-1111-1111-111111111111"
    real_restore = verify_restore._restore

    def populate_after_restore(dp, scratch_conninfo):
        real_restore(dp, scratch_conninfo)  # already monkeypatched to dbmate up
        with psycopg.connect(scratch_conninfo, autocommit=True) as c:
            cur = c.cursor()
            cur.execute("INSERT INTO spaces (id, display_name) VALUES ('vr-space', 'VR Space')")
            cur.execute(
                "INSERT INTO principals (space_id, id, display_name) VALUES ('vr-space', 'p1', 'P1')")
            cur.execute(
                "INSERT INTO node_types (space_id, name, description, attr_spec) "
                "VALUES ('vr-space', 'note', 'a note', %s)", (Jsonb({"attrs": {"closed": False}}),))
            cur.execute(
                "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
                "VALUES ('vr-space', 'scope1', 'Scope', 'p1', 'private')")
            cur.execute(
                "INSERT INTO nodes (id, space_id, type, scope_id, title, body, attrs, embedding, "
                "embedding_model, source_client, author_principal) VALUES "
                "(%s, 'vr-space', 'note', 'scope1', 'sentinel title', 'sentinel body', '{}', "
                 "'[" + ",".join(["0"] * 384) + "]', 'test', 'pytest', 'p1')", (sentinel_id,))

    monkeypatch.setattr(verify_restore, "_restore", populate_after_restore)

    log = verify_restore.run(
        dump_path, DATABASE_URL, migrations_dir=MIGRATIONS_DIR, sentinel_id=sentinel_id)
    assert any("restoring into scratch database" in line for line in log)
    assert any("schema_migrations at" in line and "matches expected" in line for line in log)
    assert any("1 space(s) restored" in line for line in log)
    assert any("constraint probe ok: spaces.id pattern CHECK" in line for line in log)
    assert any("constraint probe ok: nodes.body length CHECK" in line for line in log)
    assert any("constraint probe ok: nodes.status/canonical_id CHECK" in line for line in log)
    # Resolution is now per space (design/04's sentinel convention): this space
    # has no `sentinel.node_id` in config -- it is a pre-convention space, seeded
    # by hand above -- so the operator-supplied --sentinel-id is what gets used,
    # and the log says which source it came from.
    assert any(f"sentinel {sentinel_id!r} retrieved from --sentinel-id" in line for line in log)
    assert any("dropped" in line for line in log)


def test_sentinel_retrieval_skipped_when_not_given():
    # _sentinel_check is a pure unit against any live connection; DATABASE_URL
    # itself is fine as the target since it never mutates. With no id from
    # either source, every space is SKIPPED rather than failed (pre-convention
    # spaces keep working) -- and the skip is logged per space so it can never
    # be mistaken for a pass.
    # Deliberately tolerant about WHICH spaces the shared dev DB happens to
    # hold: with no id supplied, a pre-convention space must skip, and a space
    # that has `sentinel.node_id` in config resolves from config. What must
    # never happen is a line claiming retrieval it did not do, or a raise.
    lines = verify_restore._sentinel_check(DATABASE_URL, None)
    assert all(("sentinel retrieval skipped" in line) or ("from config" in line)
               for line in lines), lines


def test_sentinel_retrieval_raises_when_id_not_found():
    with pytest.raises(verify_restore.VerifyRestoreError, match="not found"):
        verify_restore._sentinel_check(DATABASE_URL, "00000000-0000-0000-0000-000000000000")


def test_run_raises_on_missing_dump_file(tmp_path):
    with pytest.raises(verify_restore.VerifyRestoreError, match="not found"):
        verify_restore.run(tmp_path / "nope.pgdump", DATABASE_URL, migrations_dir=MIGRATIONS_DIR)
