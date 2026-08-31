"""0025_api_tokens_live_unique_index: what it unblocks, and what it costs.

The migration replaces the table-level UNIQUE (space_id, principal, client_name)
from 0008 with a partial unique index over live rows. These tests pin the three
behaviours that matter:

  * after it, revoke-then-remint under the SAME client name succeeds, which is
    what token rotation is;
  * the property the old constraint protected is still enforced: two LIVE
    tokens for one (space, principal, client name) are still refused;
  * down is clean on a database that has never rotated, and refuses on one that
    has. The second is not a defect. A revoked row and a live row sharing a
    triple is exactly the state rotation produces, and no down migration can
    restore a constraint that data violates without deleting the revoked row,
    which is the audit record the retention rule exists to keep.

Runs against ENGRAPHY_TEST_DATABASE_URL's cluster in its own scratch database,
created and dropped here, so nothing touches the shared dev database.
"""
import contextlib

import psycopg
import pytest

from conftest import DATABASE_URL
from engraphy.admin import migrate

MIGRATION = "0025_api_tokens_live_unique_index.sql"
SCRATCH = "engraphy_mig0025_scratch"


def _scratch_url(dbname: str) -> str:
    base, _, _tail = DATABASE_URL.partition("?")
    prefix, _, _olddb = base.rpartition("/")
    return f"{prefix}/{dbname}{DATABASE_URL[len(base):]}"


def _admin_url() -> str:
    return _scratch_url("postgres")


def _down_section(sql_text: str) -> str:
    """The mirror of migrate._up_section: everything after `-- migrate:down`.

    Line endings are preserved the same way, for the same reason.
    """
    lines = sql_text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip().startswith("-- migrate:down"):
            return "".join(lines[i + 1:]).strip()
    raise AssertionError(f"{MIGRATION}: no '-- migrate:down' marker found")


def _down_sql() -> str:
    path = migrate.DEFAULT_MIGRATIONS_DIR / MIGRATION
    return _down_section(path.read_text(encoding="utf-8"))


def _up_sql() -> str:
    path = migrate.DEFAULT_MIGRATIONS_DIR / MIGRATION
    return migrate._up_section(path.read_text(encoding="utf-8"), MIGRATION)


@pytest.fixture
def migrated_db():
    """A scratch database with every shipped migration applied, plus a space and
    principal for api_tokens' two foreign keys to land on."""
    try:
        with psycopg.connect(_admin_url(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH}"')
            conn.execute(f'CREATE DATABASE "{SCRATCH}"')
    except psycopg.Error as exc:
        pytest.skip(f"cannot create scratch database (environment limitation): {exc}")

    url = _scratch_url(SCRATCH)
    try:
        migrate.apply_migrations(url, migrate.DEFAULT_MIGRATIONS_DIR)
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO spaces (id, display_name) VALUES ('rot', 'rotation probe')")
            conn.execute(
                "INSERT INTO principals (space_id, id, display_name) "
                "VALUES ('rot', 'owner', 'rotation probe')")
        yield url
    finally:
        with contextlib.suppress(psycopg.Error):
            with psycopg.connect(_admin_url(), autocommit=True) as conn:
                conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH}"')


def _mint(conn, token_hash, client_name="engraphy-agent"):
    conn.execute(
        "INSERT INTO api_tokens (space_id, principal, client_name, token_hash, role) "
        "VALUES ('rot', 'owner', %s, %s, 'readwrite')",
        (client_name, token_hash),
    )


def _revoke(conn, client_name="engraphy-agent"):
    cur = conn.execute(
        "UPDATE api_tokens SET revoked = true WHERE space_id = 'rot' AND principal = 'owner' "
        "AND client_name = %s AND revoked = false",
        (client_name,),
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# up
# ---------------------------------------------------------------------------

def test_up_leaves_the_partial_index_and_removes_the_table_constraint(migrated_db):
    """Both statements ran. Adding the index without dropping the constraint
    would leave the original key in force and change nothing, so this checks
    for the absence as well as the presence."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        constraints = [r[0] for r in conn.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'api_tokens'::regclass "
            "AND contype = 'u'").fetchall()]
        assert "api_tokens_space_id_principal_client_name_key" not in constraints

        (indexdef,) = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'api_tokens_live_identity_key'"
        ).fetchone()
        assert "UNIQUE" in indexdef
        assert "NOT revoked" in indexdef


def test_up_refuses_a_second_live_token_for_the_same_identity(migrated_db):
    """The property the old constraint protected survives verbatim: at most one
    live credential per (space, principal, client name)."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _mint(conn, "hash-live-1")
        with pytest.raises(psycopg.errors.UniqueViolation):
            _mint(conn, "hash-live-2")


def test_up_allows_remint_after_revoke(migrated_db):
    """Rotation, end to end at the schema level: mint, revoke, mint again under
    the SAME client name, and both rows survive with exactly one of them live."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _mint(conn, "hash-original")
        assert _revoke(conn) == 1
        _mint(conn, "hash-rotated")

        rows = conn.execute(
            "SELECT token_hash, revoked FROM api_tokens WHERE space_id = 'rot' "
            "AND client_name = 'engraphy-agent' ORDER BY created_at").fetchall()
        assert rows == [("hash-original", True), ("hash-rotated", False)]


def test_up_allows_repeated_rotation(migrated_db):
    """Rotation is not a one-shot: the retained rows accumulate and never block
    the next mint, because the key only counts live rows."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        for i in range(4):
            _mint(conn, f"hash-{i}")
            assert _revoke(conn) == 1
        _mint(conn, "hash-final")

        (live,) = conn.execute(
            "SELECT count(*) FROM api_tokens WHERE NOT revoked").fetchone()
        (total,) = conn.execute("SELECT count(*) FROM api_tokens").fetchone()
        assert (live, total) == (1, 5)


def test_up_key_is_per_client_name_not_per_principal(migrated_db):
    """The two per-tenant credentials the control plane mints, side by side. A
    key that had collapsed to (space, principal) would refuse the second."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _mint(conn, "hash-agent")
        _mint(conn, "hash-dashboard", client_name="engraphy-dashboard")
        (n,) = conn.execute("SELECT count(*) FROM api_tokens WHERE NOT revoked").fetchone()
        assert n == 2


# ---------------------------------------------------------------------------
# down
# ---------------------------------------------------------------------------

def test_down_is_clean_on_a_database_that_has_not_rotated(migrated_db):
    """The reversible case. The index goes, the table constraint comes back, and
    it enforces what it always did."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _mint(conn, "hash-live-1")
        conn.execute(_down_sql())

        constraints = [r[0] for r in conn.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'api_tokens'::regclass "
            "AND contype = 'u'").fetchall()]
        assert "api_tokens_space_id_principal_client_name_key" in constraints

        indexes = [r[0] for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'api_tokens'").fetchall()]
        assert "api_tokens_live_identity_key" not in indexes

        with pytest.raises(psycopg.errors.UniqueViolation):
            _mint(conn, "hash-live-2")


def test_down_then_up_is_idempotent(migrated_db):
    """Down and up again restores the rotation capability, so an operator who
    reverses and re-applies is not left in a third state."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute(_down_sql())
        conn.execute(_up_sql())
        _mint(conn, "hash-original")
        assert _revoke(conn) == 1
        _mint(conn, "hash-rotated")
        (n,) = conn.execute("SELECT count(*) FROM api_tokens").fetchone()
        assert n == 2


def test_down_refuses_on_a_database_that_has_rotated(migrated_db):
    """The documented one-way edge, pinned so it stays documented.

    After a rotation the table holds a revoked row and a live row sharing a
    triple, which is precisely what the wider constraint forbids. Down fails,
    and the alternative (deleting the revoked row to make room) would destroy
    the record that the credential ever existed. An operator going back past
    this point restores the pre-migration dump.
    """
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _mint(conn, "hash-original")
        _revoke(conn)
        _mint(conn, "hash-rotated")

        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(_down_sql())

    # And the data is untouched by the failed attempt.
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        (n,) = conn.execute("SELECT count(*) FROM api_tokens").fetchone()
        assert n == 2
