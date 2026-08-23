"""Fixtures for the harness's DB-backed tests (design/09).

Reuses the engine's own connection/pool fixtures rather than building a second
set: the harness must be exercised against the same RLS-live app role the server
uses, or the tests would prove something about a privileged path that no
deployment runs.

Unlike the engine's tests, these cannot be rollback-scoped. `dedup.write` opens
and commits its own transactions through the pool, so a benchmark ingest is
committed by construction. Each test therefore provisions a uniquely-named space
and drops it afterwards.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from engraphy.tests.conftest import DATABASE_URL
from engraphy.tests.conftest import app_conn, conn, pool  # noqa: F401 -- fixture re-export

# The session-scoped autouse fixture that creates the `engram_app` role and
# (re-)applies its table GRANTs. It MUST be re-exported here, not just inherited
# in spirit: `pytest bench/tests` run on its own never collects the engine's
# conftest, so without this the `pool` fixture connects as a role that either
# does not exist or holds no grants on this database. Every run during
# development passed only because earlier engine test sessions had already
# provisioned the role -- a warm-environment illusion that would have failed the
# first time anyone ran the bench suite against a fresh database.
from engraphy.tests.conftest import _ensure_app_role  # noqa: F401 -- session autouse fixture

from bench.core.space import provision_run_space, space_id_for


@pytest.fixture
def run_id() -> str:
    """A unique run id per test, so committed rows can never collide across
    tests or across reruns."""
    return f"t{uuid.uuid4().hex[:12]}"


@pytest.fixture
def bench_space(run_id):
    """Provision a run space + scopes on a superuser connection, yield a factory,
    and drop the whole space afterwards.

    Teardown is by space id, which is the reason design/09 puts the run id in
    the space name: one DELETE per table removes everything a run created,
    with no risk of catching another run's rows.
    """
    created: list[str] = []

    def _provision(haystack_ids, **kw):
        with psycopg.connect(DATABASE_URL, autocommit=True) as c:
            rs = provision_run_space(c, run_id=run_id, haystack_ids=list(haystack_ids), **kw)
        created.append(rs.space_id)
        return rs

    yield _provision

    sid = space_id_for(run_id)
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        cur = c.cursor()
        # Child-first, so FKs never block the teardown.
        for table in (
            "edges", "dedup_log", "pending_writes", "inbox", "audit_log",
            "nodes", "scope_grants", "scopes", "edge_rules", "edge_types",
            "node_types", "config", "api_tokens", "principals",
        ):
            cur.execute(f"DELETE FROM {table} WHERE space_id = %s", (sid,))
        cur.execute("DELETE FROM spaces WHERE id = %s", (sid,))
