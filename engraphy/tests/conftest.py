"""Shared fixtures for live-Postgres E0 tests. Every test gets its own
rollback-scoped transaction against ENGRAPHY_TEST_DATABASE_URL (defaults to the
IMPLEMENTER.md scratch instance) -- no committed rows ever leak between tests,
so no reset step is needed between runs.
"""

import asyncio
import os
import sys

import psycopg
import pytest
from psycopg.types.json import Jsonb

if sys.platform == "win32":
    # psycopg's async mode requires a selector-based loop; Windows defaults
    # to ProactorEventLoop, which it explicitly does not support.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

DATABASE_URL = os.environ.get(
    "ENGRAPHY_TEST_DATABASE_URL",
    "postgres://postgres:engraphy@localhost:5432/engraphy_dev?sslmode=disable",
)

# design/01 §Row-level security: "The app's DB role is not BYPASSRLS." No doc
# gives literal CREATE ROLE DDL for it (DECISIONS-DELTA.md); provisioned here,
# idempotently, as test infrastructure for the RLS probe / visibility matrix --
# roles are cluster-level so this survives the scratch DB being dropped and
# recreated, but table GRANTs do not, so grants are re-applied every session.
APP_ROLE = "engraphy_app"
APP_ROLE_PASSWORD = "engraphy_app_test_only"
APP_DATABASE_URL = DATABASE_URL.replace("postgres:engraphy@", f"{APP_ROLE}:{APP_ROLE_PASSWORD}@")

_RLS_TABLES = ("nodes", "edges", "scopes", "scope_grants", "principals", "pending_writes", "inbox", "dedup_log", "metrics_rollup")
_READ_ONLY_TABLES = ("spaces", "node_types", "edge_types", "edge_rules", "config")
_UNGATED_WRITE_TABLES = ("api_tokens", "audit_log")

# The product's ONLY DELETE grant: resolve_duplicate consumes the parked row it
# resolves (QUESTIONS.md "pending-write-consumption", resolved 2026-07-16).
# Paired with migration 0011's pending_writes_delete policy and useless without
# it -- FORCE RLS turns a policy-less DELETE into a silent zero-row no-op rather
# than an error, so granting this alone would hide un-consumed parked writes
# behind a success envelope. Everything else in the schema is append-or-update-
# only (01: no hard deletes).
_DELETE_TABLES = ("pending_writes",)


@pytest.fixture(scope="session", autouse=True)
def _ensure_app_role():
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        cur = c.cursor()
        # APP_ROLE/APP_ROLE_PASSWORD are fixed module constants, not external
        # input -- safe to interpolate directly; psycopg's own placeholder
        # parser would otherwise collide with Postgres format()'s %I/%L.
        # ALTER ROLE ... PASSWORD unconditionally on the existing-role branch
        # too: roles are cluster-level (shared across every database on this
        # server, not scoped to the one this connection is in), so anything
        # else that ever runs ALTER ROLE engraphy_app PASSWORD against this
        # same cluster (e.g. manually testing deploy/provision-app-role.sql
        # against a scratch DB on the same server) silently desyncs this
        # constant from the real password, and every APP_DATABASE_URL
        # connection in this file then fails auth with no hint why. Cheap to
        # just always re-assert it every session.
        cur.execute(
            f"DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN "
            f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_ROLE_PASSWORD}' "
            f"NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE; "
            f"ELSE ALTER ROLE {APP_ROLE} PASSWORD '{APP_ROLE_PASSWORD}'; END IF; END $$;"
        )
        cur.execute(f"GRANT CONNECT ON DATABASE {c.info.dbname} TO {APP_ROLE}")
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
        cur.execute(f"GRANT SELECT, INSERT, UPDATE ON {', '.join(_RLS_TABLES)} TO {APP_ROLE}")
        cur.execute(f"GRANT DELETE ON {', '.join(_DELETE_TABLES)} TO {APP_ROLE}")
        cur.execute(f"GRANT SELECT ON {', '.join(_READ_ONLY_TABLES)} TO {APP_ROLE}")
        cur.execute(f"GRANT SELECT, INSERT, UPDATE ON {', '.join(_UNGATED_WRITE_TABLES)} TO {APP_ROLE}")
        cur.execute(f"GRANT EXECUTE ON FUNCTION engraphy_readable_scopes() TO {APP_ROLE}")
        cur.execute(f"GRANT EXECUTE ON FUNCTION engraphy_writable_scopes() TO {APP_ROLE}")
        cur.execute(f"GRANT EXECUTE ON FUNCTION engraphy_validate_attrs(jsonb, jsonb) TO {APP_ROLE}")
        # schema_migrations exists only on a dbmate-provisioned DB (CI); the
        # local dev DB has never been through dbmate, so it is absent there.
        # /healthz reads it through the app-role pool (applied_schema_version),
        # so grant SELECT when the table is present. Production provisioning of
        # the app role needs this same grant -- flag for the E3 deploy docs.
        cur.execute("SELECT to_regclass('public.schema_migrations')")
        if cur.fetchone()[0] is not None:
            cur.execute(f"GRANT SELECT ON schema_migrations TO {APP_ROLE}")


@pytest.fixture
def conn():
    """Superuser connection, one rollback-scoped transaction per test."""
    with psycopg.connect(DATABASE_URL, autocommit=False) as c:
        yield c
        c.rollback()


@pytest.fixture
async def pool():
    """Real async pool under the app role -- exercises engraphy/server/db.py's
    actual transaction() wrapper (not the raw-SQL sync fixtures above).
    Function-scoped to avoid event-loop-scope mismatches with
    pytest-asyncio's per-test loop (a session-scoped async fixture would
    outlive the loop its connections were opened on)."""
    from psycopg_pool import AsyncConnectionPool

    p = AsyncConnectionPool(APP_DATABASE_URL, open=False, min_size=2, max_size=4)
    await p.open()
    yield p
    await p.close()


@pytest.fixture
def app_conn():
    """engraphy_app connection (non-superuser, non-BYPASSRLS) -- RLS is live on
    this connection. Callers must set_identity() before querying RLS-gated
    tables; with no identity set, current_setting returns NULL and every
    policy denies (fail-closed, by design)."""
    with psycopg.connect(APP_DATABASE_URL, autocommit=False) as c:
        yield c
        c.rollback()


def set_identity(conn, space_id, principal):
    """The one-line GUC protocol from visibility-and-rls-plan.md, `true` =
    transaction-local. Production code's only caller is engraphy/server/db.py's
    transaction() wrapper (async, built in E0 alongside the RLS functions it
    protects); E0/E1's raw-SQL tests call this directly on sync connections
    since they're exercising RLS/trigger/constraint behavior at the SQL
    level, where sync vs async makes no difference to what's under test.
    Async pipeline code (E1's dedup write path onward) uses the real
    transaction() wrapper instead -- see test_dedup.py."""
    conn.cursor().execute(
        "SELECT set_config('engraphy.space_id', %s, true), set_config('engraphy.principal', %s, true)",
        (space_id, principal),
    )


def bootstrap_space(conn, space_id="t1", principal_id="p1"):
    """Minimal space + principal + a 'widget' node type + a 'relates_to' edge
    type/rule + one private scope owned by the principal. Returns a dict of
    the ids created, for tests to build on."""
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, %s)", (space_id, "Test Space"))
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name) VALUES (%s, %s, %s)",
        (space_id, principal_id, "Test Principal"),
    )
    attr_spec = {
        "attrs": {
            "required": {"status": {"enum": ["open", "closed"]}},
            "optional": {"note": {"type": "string"}},
            "closed": True,
        }
    }
    cur.execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES (%s, %s, %s, %s)",
        (space_id, "widget", "A test node type.", Jsonb(attr_spec)),
    )
    cur.execute(
        "INSERT INTO edge_types (space_id, name, description, bidirectional) VALUES (%s, %s, %s, %s)",
        (space_id, "relates_to", "Generic association.", True),
    )
    cur.execute(
        "INSERT INTO edge_rules (space_id, type, src_type, dst_type) VALUES (%s, %s, %s, %s)",
        (space_id, "relates_to", "widget", "widget"),
    )
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, %s, %s, %s, %s)",
        (space_id, "scope1", "Scope One", principal_id, "private"),
    )
    return {
        "space_id": space_id,
        "principal_id": principal_id,
        "node_type": "widget",
        "edge_type": "relates_to",
        "scope_id": "scope1",
    }


def insert_node(conn, space_id, scope_id, node_type="widget", attrs=None, **overrides):
    """Insert a node with sane defaults for every NOT NULL column not under test."""
    cur = conn.cursor()
    values = {
        "space_id": space_id,
        "type": node_type,
        "scope_id": scope_id,
        "title": "A test node title",
        "body": "A test node body.",
        "attrs": Jsonb(attrs if attrs is not None else {"status": "open"}),
        "embedding": "[" + ",".join(["0"] * 384) + "]",
        "embedding_model": "test-model",
        "source_client": "pytest",
        "author_principal": overrides.pop("author_principal", None) or "p1",
    }
    values.update(overrides)
    cols = list(values.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    cur.execute(
        f"INSERT INTO nodes ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
        [values[c] for c in cols],
    )
    return cur.fetchone()[0]
