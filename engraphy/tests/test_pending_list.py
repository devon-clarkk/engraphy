"""engraphy.core.pending.pending_list -- the read-only confirm-queue list.

The tool SELECTs the caller's own pending_writes rows (the write/dedup PENDING
band) and shapes them for a client's confirm queue. These tests prove it:
returns the caller's rows in the expected shape, is read-only, is RLS-scoped so
one caller can never see another space's (or another principal's) pendings, and
handles the empty case + limit/offset.

Seeding goes through a superuser autocommit connection (committed, so the
separate app-role `pool` connection can see the rows); the READ under test goes
through that app-role pool (NOBYPASSRLS) inside `transaction()`, which sets the
`engraphy.space_id` / `engraphy.principal` GUCs the `pending_writes_read` policy
enforces -- so what these tests exercise is the real RLS path, not a superuser
bypass. Space ids are derived per-test (like test_dedup.py's write_space) so the
fixed-id delete-on-setup can never collide with a concurrently-running test.
"""
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from conftest import DATABASE_URL
from engraphy.core import pending as pending_mod
from engraphy.core.pending import pending_list
from engraphy.server.tool_registry import resolve_dispatch

# Principals are free-text author_principal values (no principals rows needed):
# PA / PA2 are two principals in space A; PB is a principal in space B.
PA = "pl-pa"
PA2 = "pl-pa2"
PB = "pl-pb"


class _Env:
    def __init__(self, conn, space_a, space_b):
        self.conn = conn
        self.space_a = space_a
        self.space_b = space_b


def _payload(title, body, candidate_title="Coffee machine", similarity=0.96):
    """A payload shaped exactly like dedup.py::_do_pending parks: incoming
    title/body plus a nearest-first candidates snapshot of {id,title,body,
    similarity}. embedding/links/extra_search are parked too but the list must
    ignore them (queue preview, not the payload)."""
    return {
        "type": "widget",
        "scope_id": "scope1",
        "title": title,
        "body": body,
        "attrs": {},
        "source_client": "pytest",
        "source_session": None,
        "embedding": [0.0] * 384,
        "candidates": [
            {"id": str(uuid.uuid4()), "title": candidate_title,
             "body": "the office coffee machine", "similarity": similarity},
        ],
        "links": [],
        "extra_search": "",
    }


def _seed(cur, space_id, principal, payload, *, age_seconds=0, ttl_hours=24):
    """Insert one pending_writes row with an explicit created_at (so newest-first
    ordering is deterministic in tests) and return its id."""
    cur.execute(
        "INSERT INTO pending_writes (space_id, author_principal, payload, expires_at, created_at) "
        "VALUES (%s, %s, %s, now() + make_interval(hours => %s), now() - make_interval(secs => %s)) "
        "RETURNING id",
        (space_id, principal, Jsonb(payload), ttl_hours, age_seconds),
    )
    return str(cur.fetchone()[0])


@pytest.fixture
def env(request):
    """Two per-test spaces (committed, so the app-role pool sees them), torn
    down after. Ids are derived from the test name -- unique per test, so the
    setup-delete can never race a concurrently-running test."""
    stem = request.node.name.replace("_", "-")
    space_a = ("pl-a-" + stem)[:60]
    space_b = ("pl-b-" + stem)[:60]
    conn = psycopg.connect(DATABASE_URL, autocommit=True)
    cur = conn.cursor()
    for space in (space_a, space_b):
        cur.execute("DELETE FROM pending_writes WHERE space_id = %s", (space,))
        cur.execute("DELETE FROM spaces WHERE id = %s", (space,))
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'A'), (%s, 'B')",
                (space_a, space_b))
    try:
        yield _Env(conn, space_a, space_b)
    finally:
        for space in (space_a, space_b):
            cur.execute("DELETE FROM pending_writes WHERE space_id = %s", (space,))
            cur.execute("DELETE FROM spaces WHERE id = %s", (space,))
        conn.close()


async def test_returns_callers_rows_with_expected_shape(pool, env):
    cur = env.conn.cursor()
    pid_new = _seed(cur, env.space_a, PA, _payload("Coffee machine is broken", "Down again on 3."),
                    age_seconds=1)
    pid_old = _seed(cur, env.space_a, PA, _payload("Printer jam", "Tray 2 keeps jamming."),
                    age_seconds=100)

    result = await pending_list(pool, env.space_a, PA)

    assert result["v"] == 1
    items = result["pending"]
    assert [it["id"] for it in items] == [pid_new, pid_old]  # newest-first

    top = items[0]
    assert set(top) == {"id", "payload_preview", "candidates", "expires_at", "created_at"}
    # payload_preview is a single string: incoming title + capped body snippet.
    assert top["payload_preview"] == "Coffee machine is broken — Down again on 3."
    assert top["expires_at"] is not None and top["created_at"] is not None
    # candidates carry at least id + title (the extension's requirement); similarity rides along.
    cand = top["candidates"][0]
    assert set(cand) >= {"id", "title"}
    assert cand["title"] == "Coffee machine"
    assert cand["similarity"] == 0.96


async def test_empty_when_caller_has_no_pendings(pool, env):
    # space_a exists but nothing was seeded for PA.
    result = await pending_list(pool, env.space_a, PA)
    assert result == {"v": 1, "pending": []}


async def test_rls_scopes_out_other_space_and_other_principal(pool, env):
    cur = env.conn.cursor()
    _seed(cur, env.space_a, PA, _payload("Only PA in space A should see this", "secret-ish"))

    # Same principal, same space: sees it.
    assert len((await pending_list(pool, env.space_a, PA))["pending"]) == 1

    # Different space + different principal: blind.
    assert (await pending_list(pool, env.space_b, PB))["pending"] == []

    # SAME space, DIFFERENT principal: blind (author scoping -- a parked write
    # is visible only to its author, it may quote private content).
    assert (await pending_list(pool, env.space_a, PA2))["pending"] == []

    # DIFFERENT space, SAME principal id: blind. Isolates the space leg of the
    # policy on its own -- a bug that dropped the space clause would be invisible
    # to the cases above, where the principal always also differs.
    assert (await pending_list(pool, env.space_b, PA))["pending"] == []


async def test_is_read_only(pool, env):
    cur = env.conn.cursor()
    _seed(cur, env.space_a, PA, _payload("row one", "b1"))
    _seed(cur, env.space_a, PA, _payload("row two", "b2"))

    cur.execute("SELECT count(*) FROM pending_writes WHERE space_id IN (%s, %s)",
                (env.space_a, env.space_b))
    (before,) = cur.fetchone()

    await pending_list(pool, env.space_a, PA)
    await pending_list(pool, env.space_a, PA, limit=1)

    cur.execute("SELECT count(*) FROM pending_writes WHERE space_id IN (%s, %s)",
                (env.space_a, env.space_b))
    (after,) = cur.fetchone()
    assert after == before  # no rows inserted, consumed, or deleted by a read


async def test_limit_and_offset_paginate_newest_first(pool, env):
    cur = env.conn.cursor()
    ids = [
        _seed(cur, env.space_a, PA, _payload(f"row {i}", f"body {i}"), age_seconds=i)
        for i in range(3)
    ]  # ids[0] newest (age 0), ids[2] oldest (age 2)

    page1 = await pending_list(pool, env.space_a, PA, limit=2)
    assert [it["id"] for it in page1["pending"]] == [ids[0], ids[1]]

    page2 = await pending_list(pool, env.space_a, PA, limit=2, offset=2)
    assert [it["id"] for it in page2["pending"]] == [ids[2]]


async def test_body_preview_is_capped(pool, env):
    cur = env.conn.cursor()
    _seed(cur, env.space_a, PA, _payload("long one", "x" * 500))

    preview = (await pending_list(pool, env.space_a, PA))["pending"][0]["payload_preview"]
    assert preview.startswith("long one — ")
    assert preview.endswith("…")
    # title + " — " + capped body + the ellipsis; comfortably shorter than the raw 500.
    assert len(preview) <= len("long one — ") + pending_mod._BODY_PREVIEW_CHARS + 1


async def test_dispatchable_through_the_registry(pool, env):
    """End-to-end wiring: the tool resolves through the same registry path
    app.py uses, and its dispatcher returns the envelope."""
    cur = env.conn.cursor()
    _seed(cur, env.space_a, PA, _payload("wired", "through the registry"))

    resolved = await resolve_dispatch(pool, env.space_a, "pending_list", {"limit": 5})
    assert resolved is not None
    core_name, dispatcher, merged_args, _action = resolved
    assert core_name == "pending_list"

    class _Ctx:
        space_id = env.space_a
        principal = PA
        client_name = "pytest"

    envelope = await dispatcher(pool, _Ctx(), merged_args)
    assert envelope["v"] == 1
    assert len(envelope["pending"]) == 1
    assert envelope["pending"][0]["payload_preview"] == "wired — through the registry"
