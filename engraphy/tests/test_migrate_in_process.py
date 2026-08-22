"""engraphy.admin.migrate in-process applier -- the dbmate-free migrate-up path.

Two layers:

  * Pure unit tests for `_up_section` (marker parsing, verbatim line-ending
    preservation, transaction:false refusal) -- no database.
  * Live-DB integration tests that apply the real shipped migrations to a fresh
    scratch database and assert the resulting schema is byte-for-byte what
    `dbmate up` produces from the same files. This is the behavior-neutrality
    proof for the in-process path: same schema_version, same objects, same
    function/trigger bodies. Skipped cleanly where the environment can't create
    a scratch DB or lacks dbmate/pg_dump.
"""
import shutil
import subprocess

import psycopg
import pytest

from conftest import DATABASE_URL
from engraphy.admin import migrate

# ---------------------------------------------------------------------------
# Pure unit tests: _up_section
# ---------------------------------------------------------------------------


def test_up_section_extracts_between_markers():
    sql = "-- migrate:up\nCREATE TABLE t (id int);\n-- migrate:down\nDROP TABLE t;\n"
    assert migrate._up_section(sql, "x.sql") == "CREATE TABLE t (id int);"


def test_up_section_no_down_marker_reads_to_eof():
    sql = "-- migrate:up\nCREATE TABLE t (id int);\n"
    assert migrate._up_section(sql, "x.sql") == "CREATE TABLE t (id int);"


def test_up_section_preserves_crlf_line_endings_verbatim():
    """The whole point of matching dbmate's stored function bodies: a CRLF file
    must yield CRLF-terminated body text, not LF-normalized text."""
    sql = "-- migrate:up\r\nCREATE FUNCTION f() RETURNS int AS $$\r\nBEGIN\r\nRETURN 1;\r\nEND;\r\n$$ LANGUAGE plpgsql;\r\n-- migrate:down\r\nDROP FUNCTION f;\r\n"
    body = migrate._up_section(sql, "x.sql")
    assert "\r\n" in body
    assert body.startswith("CREATE FUNCTION f() RETURNS int AS $$\r\n")


def test_up_section_missing_up_marker_raises():
    with pytest.raises(migrate.MigrateError, match="no '-- migrate:up' marker"):
        migrate._up_section("SELECT 1;\n", "x.sql")


def test_up_section_transaction_false_is_refused():
    sql = "-- migrate:up transaction:false\nCREATE INDEX CONCURRENTLY i ON t (c);\n-- migrate:down\n"
    with pytest.raises(migrate.MigrateError, match="transaction:false"):
        migrate._up_section(sql, "0099_x.sql")


# ---------------------------------------------------------------------------
# Live-DB integration: scratch database helpers
# ---------------------------------------------------------------------------


def _scratch_url(dbname: str) -> str:
    """Same cluster/credentials as DATABASE_URL, different database name."""
    base, _, _tail = DATABASE_URL.partition("?")
    prefix, _, _olddb = base.rpartition("/")
    query = DATABASE_URL[len(base):]
    return f"{prefix}/{dbname}{query}"


def _admin_url() -> str:
    """A connection to the `postgres` maintenance DB, used only to CREATE/DROP
    the scratch databases (you cannot drop the DB you are connected to)."""
    return _scratch_url("postgres")


def _create_db(name: str) -> None:
    with psycopg.connect(_admin_url(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.execute(f'CREATE DATABASE "{name}"')


def _drop_db(name: str) -> None:
    with psycopg.connect(_admin_url(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture
def scratch_db():
    """Yield a freshly-created, empty scratch database name; drop it after.

    Skips (does not fail) if the test cluster won't let us create a database
    from this connection -- some managed/locked-down test DBs won't, and that is
    an environment limitation, not a migrate.py bug."""
    name = "engram_inproc_scratch"
    try:
        _create_db(name)
    except psycopg.Error as exc:
        pytest.skip(f"cannot create scratch database (environment limitation): {exc}")
    try:
        yield name
    finally:
        try:
            _drop_db(name)
        except psycopg.Error:
            pass


def test_apply_migrations_applies_all_and_is_idempotent(scratch_db):
    url = _scratch_url(scratch_db)
    applied = migrate.apply_migrations(url, migrate.DEFAULT_MIGRATIONS_DIR)
    expected = migrate.expected_schema_version(migrate.DEFAULT_MIGRATIONS_DIR)
    assert applied, "expected at least one migration applied to an empty DB"
    assert applied[-1] == expected

    # Ledger is at the expected version, and re-running applies nothing.
    with psycopg.connect(url, autocommit=True) as conn:
        (top,) = conn.execute("SELECT max(version) FROM public.schema_migrations").fetchone()
    assert top == expected
    assert migrate.apply_migrations(url, migrate.DEFAULT_MIGRATIONS_DIR) == []


def test_schema_migrations_table_shape_matches_dbmate(scratch_db):
    """dbmate's ledger column is an unbounded `character varying` with a
    `schema_migrations_pkey` primary key. A bounded varchar(n) here would make
    the schema diff fail, so pin the exact shape."""
    url = _scratch_url(scratch_db)
    migrate.apply_migrations(url, migrate.DEFAULT_MIGRATIONS_DIR)
    with psycopg.connect(url, autocommit=True) as conn:
        (data_type, max_len) = conn.execute(
            "SELECT data_type, character_maximum_length FROM information_schema.columns "
            "WHERE table_name = 'schema_migrations' AND column_name = 'version'"
        ).fetchone()
        (pk_name,) = conn.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'public.schema_migrations'::regclass "
            "AND contype = 'p'"
        ).fetchone()
    assert data_type == "character varying"
    assert max_len is None  # unbounded
    assert pk_name == "schema_migrations_pkey"


def _pg_dump_schema(url: str) -> list[str]:
    """Schema-only dump as a list of content lines, with pg_dump's random
    per-run \\restrict / \\unrestrict nonce meta-commands removed (they are not
    schema content)."""
    result = subprocess.run(
        ["pg_dump", "--schema-only", "--no-owner", "--no-privileges", url],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr.strip()}")
    return [
        line for line in result.stdout.splitlines()
        if not line.startswith("\\restrict") and not line.startswith("\\unrestrict")
    ]


def test_in_process_schema_is_byte_identical_to_dbmate():
    """THE proof for item 3: apply the same migrations two ways -- in-process
    and via `dbmate up` -- into two fresh DBs on the same cluster, and assert the
    schema-only pg_dumps are identical line-for-line (nonce meta-commands
    excluded). Skips if dbmate or pg_dump is unavailable, or if scratch DBs can't
    be created."""
    if shutil.which("dbmate") is None:
        pytest.skip("dbmate not on PATH -- schema-parity test needs it as the reference")
    if shutil.which("pg_dump") is None:
        pytest.skip("pg_dump not on PATH -- needed to compare schemas")

    inproc_name = "engram_parity_inproc"
    dbmate_name = "engram_parity_dbmate"
    try:
        _create_db(inproc_name)
        _create_db(dbmate_name)
    except psycopg.Error as exc:
        pytest.skip(f"cannot create scratch databases (environment limitation): {exc}")
    try:
        migrate.apply_migrations(_scratch_url(inproc_name), migrate.DEFAULT_MIGRATIONS_DIR)
        dbmate_result = subprocess.run(
            ["dbmate", "--migrations-dir", str(migrate.DEFAULT_MIGRATIONS_DIR),
             "--no-dump-schema", "--url", _scratch_url(dbmate_name), "up"],
            capture_output=True, text=True,
        )
        assert dbmate_result.returncode == 0, f"dbmate up failed: {dbmate_result.stderr}"

        inproc_schema = _pg_dump_schema(_scratch_url(inproc_name))
        dbmate_schema = _pg_dump_schema(_scratch_url(dbmate_name))
        assert inproc_schema == dbmate_schema, "in-process schema differs from dbmate's"

        # And the ledger rows themselves (schema-only dumps omit table data).
        with psycopg.connect(_scratch_url(inproc_name), autocommit=True) as c:
            inproc_versions = [r[0] for r in c.execute(
                "SELECT version FROM public.schema_migrations ORDER BY version").fetchall()]
        with psycopg.connect(_scratch_url(dbmate_name), autocommit=True) as c:
            dbmate_versions = [r[0] for r in c.execute(
                "SELECT version FROM public.schema_migrations ORDER BY version").fetchall()]
        assert inproc_versions == dbmate_versions
    finally:
        _drop_db(inproc_name)
        _drop_db(dbmate_name)
