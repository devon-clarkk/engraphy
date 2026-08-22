"""Per-space usage metrics (engraphy.core.metrics + migration 0021 + the `stats`
tool). Covers, against a live Postgres under the RLS-enforcing app role:

- bump_many upserts + accumulates on the right (metric, day) row;
- bump_safe is best-effort (a DB failure never propagates);
- stats shape: zero-filled contiguous daily series, totals == sum(series),
  range clamp;
- RLS: a filter-free read under one space's identity sees zero of another
  space's rows, and a write may only land the caller's own principal row;
- the counted CORE chokepoints increment the correct metric: search
  (questions_asked/answered/memory_reused), write (facts_stored /
  duplicates_prevented across the insert / merge / pending bands),
  resolve_duplicate (facts_stored on a distinct->insert), inbox promote
  (promotes, on top of the write's own outcome metric).
"""
import datetime
import math

import psycopg
import pytest
from psycopg.types.json import Jsonb

from engraphy.core import metrics
from engraphy.core.dedup import BandThresholds, resolve_duplicate, write
from engraphy.core.inbox import capture, promote
from engraphy.core.search import search
from engraphy.server.db import transaction

# ---- fixtures / helpers -----------------------------------------------------


def _unit_vector_at_angle(theta: float) -> list[float]:
    """A 384-dim unit vector at angle theta from e1, so its cosine with
    _unit_vector_at_angle(0) is exactly cos(theta) -- a controlled similarity
    independent of any embedding model (mirrors test_dedup)."""
    vec = [0.0] * 384
    vec[0] = math.cos(theta)
    vec[1] = math.sin(theta)
    return vec


def _bootstrap_metrics_space(conn, space_id, principal="p1"):
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, %s)", (space_id, "Metrics Space"))
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name) VALUES (%s, %s, 'P')",
        (space_id, principal),
    )
    cur.execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES "
        "(%s, 'widget', 'w', %s)",
        (space_id, Jsonb({"attrs": {"closed": False}})),
    )
    # relates_to + same_topic: resolve_duplicate(distinct) adds relates_to to the
    # nearest candidate, and the merge-link path adds same_topic -- both must be
    # declared or those write paths would raise inside the pipeline.
    cur.execute(
        "INSERT INTO edge_types (space_id, name, description, bidirectional) VALUES "
        "(%s, 'relates_to', 'assoc', true), (%s, 'same_topic', 'same topic', true)",
        (space_id, space_id),
    )
    cur.execute(
        "INSERT INTO edge_rules (space_id, type, src_type, dst_type) VALUES "
        "(%s, 'relates_to', 'widget', 'widget'), (%s, 'same_topic', 'widget', 'widget')",
        (space_id, space_id),
    )
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, 'scope1', 'S', %s, 'private')",
        (space_id, principal),
    )
    conn.commit()


def _cleanup_metrics_space(conn, space_id):
    cur = conn.cursor()
    for t in ("metrics_rollup", "inbox", "config", "audit_log", "dedup_log",
              "pending_writes", "edges", "nodes", "scope_grants", "scopes",
              "edge_rules", "edge_types", "node_types", "principals"):
        cur.execute(f"DELETE FROM {t} WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


@pytest.fixture
def metrics_space(conn, request):
    space_id = ("me-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap_metrics_space(conn, space_id)
    yield space_id
    _cleanup_metrics_space(conn, space_id)


def _seed_node(conn, space_id, title, body, embedding_vector, node_type="widget"):
    cur = conn.cursor()
    lit = "[" + ",".join(str(x) for x in embedding_vector) + "]"
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
        "embedding_model, source_client, author_principal) "
        "VALUES (%s, %s, 'scope1', %s, %s, %s, %s::vector, 'test-model', 'pytest', 'p1') "
        "RETURNING id",
        (space_id, node_type, title, body, Jsonb({}), lit),
    )
    (nid,) = cur.fetchone()
    conn.commit()
    return nid


def _db_today(conn) -> datetime.date:
    """The DB's own UTC calendar date -- the same anchor bump/stats use, so
    backfilled buckets line up regardless of the host's local timezone."""
    cur = conn.cursor()
    cur.execute("SELECT (now() AT TIME ZONE 'UTC')::date")
    return cur.fetchone()[0]


def _read_rollup(conn, space_id):
    """Superuser (BYPASSRLS) read of a space's raw rollup rows as
    {(metric, bucket_date): count}."""
    cur = conn.cursor()
    cur.execute(
        "SELECT metric, bucket_date, count FROM metrics_rollup WHERE space_id = %s",
        (space_id,),
    )
    return {(m, d): c for m, d, c in cur.fetchall()}


# ---- bump_many / bump_safe --------------------------------------------------


async def test_bump_many_upserts_and_accumulates(pool, metrics_space, conn):
    day = _db_today(conn)
    async with transaction(pool, metrics_space, "p1") as c:
        await metrics.bump_many(c.cursor(), metrics_space, "p1",
                                {metrics.QUESTIONS_ASKED: 1, metrics.ANSWERED: 1}, bucket_date=day)
    async with transaction(pool, metrics_space, "p1") as c:
        await metrics.bump_many(c.cursor(), metrics_space, "p1",
                                {metrics.QUESTIONS_ASKED: 2}, bucket_date=day)

    rows = _read_rollup(conn, metrics_space)
    assert rows[(metrics.QUESTIONS_ASKED, day)] == 3   # 1 + 2 accumulated on ON CONFLICT
    assert rows[(metrics.ANSWERED, day)] == 1
    # A zero count writes no row (skipped), so answered has exactly one row.
    assert len(rows) == 2


async def test_bump_many_skips_zero_counts(pool, metrics_space, conn):
    day = _db_today(conn)
    async with transaction(pool, metrics_space, "p1") as c:
        await metrics.bump_many(c.cursor(), metrics_space, "p1",
                                {metrics.ANSWERED: 0, metrics.MEMORY_REUSED: 0}, bucket_date=day)
    assert _read_rollup(conn, metrics_space) == {}


async def test_bump_safe_swallows_db_failure(pool, conn):
    """A space_id absent from `spaces` violates the FK -- bump_safe must swallow
    it (best-effort observability never fails the caller) and write nothing."""
    # Must not raise:
    await metrics.bump_safe(pool, "no-such-space-xyz", "p1", {metrics.QUESTIONS_ASKED: 1})
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM metrics_rollup WHERE space_id = %s", ("no-such-space-xyz",))
    assert cur.fetchone()[0] == 0


# ---- stats shape / zero-fill / clamp ---------------------------------------


async def test_stats_shape_zero_fill_and_totals(pool, metrics_space, conn):
    today = _db_today(conn)
    two_ago = today - datetime.timedelta(days=2)
    # today: 3 questions, 2 answered; two days ago: 5 facts_stored; yesterday: nothing.
    async with transaction(pool, metrics_space, "p1") as c:
        cur = c.cursor()
        await metrics.bump_many(cur, metrics_space, "p1",
                                {metrics.QUESTIONS_ASKED: 3, metrics.ANSWERED: 2}, bucket_date=today)
        await metrics.bump_many(cur, metrics_space, "p1",
                                {metrics.FACTS_STORED: 5}, bucket_date=two_ago)

    result = await metrics.stats(pool, metrics_space, "p1", range_days=3)

    assert result["v"] == 1
    assert result["space"] == metrics_space
    assert result["range_days"] == 3
    assert "generated_at" in result
    # Contiguous, ascending, ends today, exactly range_days entries (zero-filled).
    assert len(result["series"]) == 3
    dates = [e["date"] for e in result["series"]]
    assert dates == [two_ago.isoformat(), (today - datetime.timedelta(days=1)).isoformat(), today.isoformat()]
    # Every metric present on every day.
    for entry in result["series"]:
        assert set(entry) == {"date", *metrics.METRICS}
    # Values landed on the right day; the empty middle day is all zeros.
    assert result["series"][0][metrics.FACTS_STORED] == 5
    assert result["series"][1] == {"date": (today - datetime.timedelta(days=1)).isoformat(),
                                   **{m: 0 for m in metrics.METRICS}}
    assert result["series"][2][metrics.QUESTIONS_ASKED] == 3
    assert result["series"][2][metrics.ANSWERED] == 2
    # totals == column-sum of the series, for every metric.
    for m in metrics.METRICS:
        assert result["totals"][m] == sum(e[m] for e in result["series"])
    assert result["totals"][metrics.QUESTIONS_ASKED] == 3
    assert result["totals"][metrics.FACTS_STORED] == 5


async def test_stats_excludes_rows_outside_range(pool, metrics_space, conn):
    today = _db_today(conn)
    old = today - datetime.timedelta(days=10)
    async with transaction(pool, metrics_space, "p1") as c:
        await metrics.bump_many(c.cursor(), metrics_space, "p1",
                                {metrics.PROMOTES: 7}, bucket_date=old)
    result = await metrics.stats(pool, metrics_space, "p1", range_days=3)
    assert result["totals"][metrics.PROMOTES] == 0        # 10 days ago is outside a 3-day window
    assert len(result["series"]) == 3


async def test_stats_range_days_clamped(pool, metrics_space):
    lo = await metrics.stats(pool, metrics_space, "p1", range_days=0)
    assert lo["range_days"] == 1 and len(lo["series"]) == 1
    hi = await metrics.stats(pool, metrics_space, "p1", range_days=10_000)
    assert hi["range_days"] == metrics._MAX_RANGE_DAYS and len(hi["series"]) == metrics._MAX_RANGE_DAYS


# ---- group_by: space (all principals) vs user (caller only) -----------------


async def _seed_two_principals(pool, space_id, day):
    """p1 gets questions_asked=3, p2 gets questions_asked=5, both today."""
    async with transaction(pool, space_id, "p1") as c:
        await metrics.bump_many(c.cursor(), space_id, "p1", {metrics.QUESTIONS_ASKED: 3}, bucket_date=day)
    async with transaction(pool, space_id, "p2") as c:
        await metrics.bump_many(c.cursor(), space_id, "p2", {metrics.QUESTIONS_ASKED: 5}, bucket_date=day)


async def test_stats_group_by_space_aggregates_all_principals(pool, metrics_space, conn):
    await _seed_two_principals(pool, metrics_space, _db_today(conn))
    result = await metrics.stats(pool, metrics_space, "p1", range_days=1, group_by="space")
    assert result["group_by"] == "space"
    assert result["principal"] is None                        # aggregate: no individual attribution
    assert result["totals"][metrics.QUESTIONS_ASKED] == 8     # p1(3) + p2(5), summed across principals


async def test_stats_group_by_user_is_caller_only(pool, metrics_space, conn):
    await _seed_two_principals(pool, metrics_space, _db_today(conn))
    result = await metrics.stats(pool, metrics_space, "p1", range_days=1, group_by="user")
    assert result["group_by"] == "user"
    assert result["principal"] == "p1"                        # the field the extension branches on
    assert result["totals"][metrics.QUESTIONS_ASKED] == 3     # p1 only; p2's 5 is NEVER surfaced
    # And a different caller sees only their own line, proving no cross-user leak.
    as_p2 = await metrics.stats(pool, metrics_space, "p2", range_days=1, group_by="user")
    assert as_p2["principal"] == "p2"
    assert as_p2["totals"][metrics.QUESTIONS_ASKED] == 5


async def test_stats_default_group_by_is_space(pool, metrics_space, conn):
    await _seed_two_principals(pool, metrics_space, _db_today(conn))
    result = await metrics.stats(pool, metrics_space, "p1", range_days=1)  # no group_by
    assert result["group_by"] == "space"
    assert result["principal"] is None
    assert result["totals"][metrics.QUESTIONS_ASKED] == 8


async def test_stats_bad_group_by_rejected(pool, metrics_space):
    with pytest.raises(ValueError):
        await metrics.stats(pool, metrics_space, "p1", group_by="everyone")


# ---- RLS --------------------------------------------------------------------


async def test_rls_filter_free_read_sees_only_own_space(pool, conn):
    """The discriminating RLS test: two spaces' rows inserted as superuser, then
    a FILTER-FREE select under space A's identity returns zero of B's rows -- so
    it is RLS, not a WHERE clause, doing the scoping."""
    a, b = "me-rls-a", "me-rls-b"
    _bootstrap_metrics_space(conn, a)
    _bootstrap_metrics_space(conn, b)
    day = _db_today(conn)
    try:
        cur = conn.cursor()
        for space in (a, b):
            cur.execute(
                "INSERT INTO metrics_rollup (space_id, principal, metric, bucket_date, count) "
                "VALUES (%s, 'p1', %s, %s, 1)",
                (space, metrics.QUESTIONS_ASKED, day),
            )
        conn.commit()

        async with transaction(pool, a, "p1") as c:
            cur = await c.execute("SELECT space_id FROM metrics_rollup")  # NO where clause
            seen = {r[0] for r in await cur.fetchall()}
        assert seen == {a}, f"filter-free read under A saw {seen}"
    finally:
        _cleanup_metrics_space(conn, a)
        _cleanup_metrics_space(conn, b)


async def test_rls_write_only_own_principal(pool, metrics_space, conn):
    """metrics_rollup_write is principal-scoped: an INSERT for a principal other
    than the session's is refused by the WITH CHECK policy (fail-closed)."""
    day = _db_today(conn)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with transaction(pool, metrics_space, "p1") as c:
            await c.execute(
                "INSERT INTO metrics_rollup (space_id, principal, metric, bucket_date, count) "
                "VALUES (%s, 'someone-else', %s, %s, 1)",
                (metrics_space, metrics.QUESTIONS_ASKED, day),
            )
    assert _read_rollup(conn, metrics_space) == {}


async def test_stats_only_returns_caller_space(pool, conn):
    a, b = "me-scope-a", "me-scope-b"
    _bootstrap_metrics_space(conn, a)
    _bootstrap_metrics_space(conn, b)
    try:
        async with transaction(pool, a, "p1") as c:
            await metrics.bump_many(c.cursor(), a, "p1", {metrics.QUESTIONS_ASKED: 2})
        async with transaction(pool, b, "p1") as c:
            await metrics.bump_many(c.cursor(), b, "p1", {metrics.QUESTIONS_ASKED: 9})

        result = await metrics.stats(pool, a, "p1", range_days=7)
        assert result["space"] == a
        assert result["totals"][metrics.QUESTIONS_ASKED] == 2  # B's 9 never leak in
    finally:
        _cleanup_metrics_space(conn, a)
        _cleanup_metrics_space(conn, b)


# ---- counted chokepoints (integration through the real core paths) ----------


async def test_write_insert_increments_facts_stored(pool, metrics_space, conn):
    result = await write(pool, metrics_space, "p1", "widget", "scope1",
                         "A brand new fact", "A brand new fact body.", {},
                         _unit_vector_at_angle(0), "pytest")
    assert result["outcome"] == "inserted"
    rows = _read_rollup(conn, metrics_space)
    assert sum(c for (m, _), c in rows.items() if m == metrics.FACTS_STORED) == 1
    assert sum(c for (m, _), c in rows.items() if m == metrics.DUPLICATES_PREVENTED) == 0


async def test_write_merge_increments_duplicates_prevented(pool, metrics_space, conn):
    _seed_node(conn, metrics_space, "Existing", "Descale the machine monthly.", _unit_vector_at_angle(0))
    result = await write(pool, metrics_space, "p1", "widget", "scope1", "Dup",
                         "Descale the machine monthly.", {}, _unit_vector_at_angle(0), "pytest",
                         thresholds=BandThresholds(t_high=0.95, t_low=0.80))
    assert result["outcome"] in ("merged", "merged_linked")   # >= t_high auto-merge band
    rows = _read_rollup(conn, metrics_space)
    assert sum(c for (m, _), c in rows.items() if m == metrics.DUPLICATES_PREVENTED) == 1
    assert sum(c for (m, _), c in rows.items() if m == metrics.FACTS_STORED) == 0


async def test_write_pending_increments_duplicates_prevented(pool, metrics_space, conn):
    _seed_node(conn, metrics_space, "Existing", "Existing body.", _unit_vector_at_angle(0))
    result = await write(pool, metrics_space, "p1", "widget", "scope1", "Near", "Near body.", {},
                         _unit_vector_at_angle(math.acos(0.87)), "pytest",  # 0.87 -> pending band
                         thresholds=BandThresholds(t_high=0.95, t_low=0.80))
    assert result["outcome"] == "needs_confirmation"
    rows = _read_rollup(conn, metrics_space)
    assert sum(c for (m, _), c in rows.items() if m == metrics.DUPLICATES_PREVENTED) == 1


async def test_resolve_distinct_increments_facts_stored(pool, metrics_space, conn):
    _seed_node(conn, metrics_space, "Existing", "Existing body.", _unit_vector_at_angle(0))
    parked = await write(pool, metrics_space, "p1", "widget", "scope1", "Near", "A distinct new body.", {},
                         _unit_vector_at_angle(math.acos(0.87)), "pytest",
                         thresholds=BandThresholds(t_high=0.95, t_low=0.80))
    assert parked["outcome"] == "needs_confirmation"       # duplicates_prevented += 1 here
    resolved = await resolve_duplicate(pool, metrics_space, "p1", parked["pending_id"], "distinct")
    assert resolved["outcome"] == "inserted"               # facts_stored += 1 here

    rows = _read_rollup(conn, metrics_space)
    assert sum(c for (m, _), c in rows.items() if m == metrics.FACTS_STORED) == 1
    # The originating pending stays counted as a caught duplicate (never decremented).
    assert sum(c for (m, _), c in rows.items() if m == metrics.DUPLICATES_PREVENTED) == 1


async def test_promote_increments_promotes_and_facts_stored(pool, metrics_space, conn):
    item = await capture(pool, metrics_space, "p1", "note", {"raw": "captured"}, "scope1")
    result = await promote(pool, metrics_space, "p1", item["id"], "widget", "scope1",
                           "Promoted", "A promoted fact.", {}, "pytest",
                           embedding_vector=_unit_vector_at_angle(0))
    assert result["outcome"] == "inserted"
    rows = _read_rollup(conn, metrics_space)
    # The promote action AND the fact it stored both count (documented dual signal).
    assert sum(c for (m, _), c in rows.items() if m == metrics.PROMOTES) == 1
    assert sum(c for (m, _), c in rows.items() if m == metrics.FACTS_STORED) == 1


async def test_search_increments_questions_answered_reused(pool, metrics_space, conn):
    """Real-model search: a hit bumps questions_asked + answered + memory_reused
    (by result count); a miss bumps only questions_asked. The miss runs against
    the empty space FIRST -- `search` applies no vector floor, so once any node
    exists in scope the vector leg always returns it, and a genuine zero-result
    read is only observable before anything is seeded."""
    from engraphy.core import embedding as emb

    miss = await search(pool, metrics_space, "p1", "scope1", "quarterly tax filing deadlines", "pytest")
    assert len(miss["results"]) == 0                     # empty space -> questions_asked only

    vec = emb.embed_document("Descale the office coffee machine monthly.")
    _seed_node(conn, metrics_space, "Coffee descaling", "Descale the office coffee machine monthly.", vec)

    hit = await search(pool, metrics_space, "p1", "scope1", "how do I descale the coffee machine", "pytest")
    assert len(hit["results"]) >= 1

    stats = await metrics.stats(pool, metrics_space, "p1", range_days=1)
    t = stats["totals"]
    assert t[metrics.QUESTIONS_ASKED] == 2               # both calls
    assert t[metrics.ANSWERED] == 1                      # only the hit
    assert t[metrics.MEMORY_REUSED] == len(hit["results"])  # facts returned by the hit
