"""engraphy/server/db.py's transaction() wrapper — visibility-and-rls-plan.md
§Session GUC protocol and §Traps:

- trap 6 (pool-bleed): transaction-local set_config only; a session-level SET
  on a pooled connection would be a cross-user identity leak. 500 interleaved
  iterations across two simulated principals sharing one small (max_size=2)
  pool, zero bleed.
- The CI grep rule: `set_config` + `engraphy.` appears nowhere outside
  engraphy/server/db.py and this test file (the wrapper is the ONLY code that
  touches these GUCs). The namespace is `engraphy.*`; a deployment provisioned
  before migration 0024 used `engram.*`, and 0024 renames it (COMPATIBILITY.md).
"""

import pathlib
import re

import pytest
from psycopg_pool import AsyncConnectionPool

from conftest import APP_ROLE, DATABASE_URL
from engraphy.server.db import transaction

REPO_ROOT = pathlib.Path(__file__).parents[2]


def test_set_config_engraphy_guc_appears_only_in_db_py_and_tests():
    pattern = re.compile(r"set_config.*engraphy\.")
    hits = []
    for path in REPO_ROOT.rglob("*.py"):
        if ".git" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append((path.relative_to(REPO_ROOT), lineno))

    non_wrapper_non_test = [
        (p, n) for p, n in hits
        if p != pathlib.Path("engraphy/server/db.py") and "tests" not in p.parts
    ]
    assert non_wrapper_non_test == [], (
        f"set_config(...engraphy...) found outside the wrapper and tests: {non_wrapper_non_test}"
    )
    wrapper_hits = [p for p, _ in hits if p == pathlib.Path("engraphy/server/db.py")]
    assert len(wrapper_hits) == 1, f"expected exactly one hit in db.py, got {wrapper_hits}"


@pytest.fixture
async def pool():
    p = AsyncConnectionPool(DATABASE_URL, min_size=2, max_size=2, open=False)
    await p.open()
    yield p
    await p.close()


@pytest.fixture
def bleed_setup(conn):
    space_id = "pooltest"
    conn.cursor().execute("INSERT INTO spaces (id, display_name) VALUES (%s, %s)", (space_id, "Pool Test"))
    conn.cursor().execute(
        "INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'pa', 'A'), (%s, 'pb', 'B')",
        (space_id, space_id),
    )
    conn.cursor().execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, 'scope-a', 'A', 'pa', 'private'), (%s, 'scope-b', 'B', 'pb', 'private')",
        (space_id, space_id),
    )
    conn.commit()  # must be visible to the pool's separate connections
    yield space_id
    with conn.cursor() as cur:
        cur.execute("DELETE FROM scopes WHERE space_id = %s", (space_id,))
        cur.execute("DELETE FROM principals WHERE space_id = %s", (space_id,))
        cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


async def test_pool_bleed(pool, bleed_setup):
    space_id = bleed_setup
    principals = [("pa", "scope-a"), ("pb", "scope-b")]
    for i in range(500):
        principal, expected_scope = principals[i % 2]
        async with transaction(pool, space_id, principal) as db_conn:
            cur = db_conn.cursor()
            # SET LOCAL, not SET: this is a pooled connection, and a plain SET
            # ROLE would persist past this transaction onto the next borrower
            # -- exactly the class of bleed this test exists to catch.
            await cur.execute(f"SET LOCAL ROLE {APP_ROLE}")
            await cur.execute("SELECT engraphy_readable_scopes()")
            got = {row[0] for row in await cur.fetchall()}
        assert got == {expected_scope}, f"iteration {i}: principal={principal} got {got}"
