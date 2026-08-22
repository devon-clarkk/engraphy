"""Per-space usage metrics: a pre-aggregated day-bucketed time-series that also
rolls up to counters, and the read side of the `stats` MCP tool.

Storage is `metrics_rollup` (migration 0021): one row per (space_id, principal,
metric, bucket_date) carrying an integer `count`, upserted with
`ON CONFLICT DO UPDATE SET count = count + excluded.count`. See that migration's
header for the storage rationale, the RLS shape, and the hot-row contention
ceiling.

================================================================================
METRIC DEFINITIONS  (the frozen label contract the VS Code extension mirrors)
================================================================================

Each metric is a string key stored in `metrics_rollup.metric`. Increments happen
at the CORE chokepoint every relevant flow funnels through, in a best-effort
transaction OUTSIDE the counted operation's own transaction (`bump_safe`), so a
metrics failure can never roll back or fail the operation it measures.

- ``questions_asked`` — one per ``search`` call (any outcome). Traverse/briefing
  are intentionally NOT counted: search is the primary "asked the memory a
  question" signal, and counting graph walks or session-start briefings would
  blur it. (A future refinement could count traverse separately.)

- ``answered`` — one per ``search`` call that returned >= 1 result. So
  ``answered <= questions_asked`` always, and (questions_asked - answered) is the
  miss count.

- ``memory_reused`` — the number of stored facts returned across searches: each
  ``search`` adds ``len(results)`` (0..25). This is a REUSE PROXY, not distinct-
  node reuse: the same node surfaced by two searches counts twice, and a single
  search returning five nodes adds five. Distinct-node reuse (dedup a node across
  its recall events) is a documented FUTURE refinement, not the MVP definition.

- ``facts_stored`` — one per write that created a NEW node: the ``inserted``
  outcome of ``write`` (including inbox ``promote``, which funnels through
  ``write``), and the ``inserted`` outcome of a ``resolve_duplicate`` that
  resolved ``distinct`` into a fresh insert. NOTE (undercount, by the partition
  choice below): the ``merged_linked`` outcome ALSO inserts a new member node but
  is counted under ``duplicates_prevented``, not here — so ``facts_stored`` is
  "novel inserts", not "every node ever created". ``supersede`` is NOT counted at
  all (it replaces/corrects an existing fact rather than storing a novel one, and
  it enters the pipeline via a different code path).

- ``duplicates_prevented`` — one per WRITE-TIME dedup event: the high-band
  auto-merge outcomes ``merged`` and ``merged_linked`` (similarity >= t_high),
  PLUS the mid-band ``needs_confirmation`` pending ([t_low, t_high)). Counted at
  WRITE time and never decremented: a pending later resolved ``distinct`` (i.e.
  it turned out NOT to be a duplicate) STILL counts here, because the metric
  measures "the engine caught and acted on a potential duplicate", which it did.
  A ``resolve_duplicate`` does NOT add to this metric (the originating pending
  already counted the event); it only adds to ``facts_stored`` when it inserts.
  Import-mode writes (``review_queued``) are not on the MCP surface and are not
  counted.

- ``promotes`` — one per successful inbox ``promote`` action (any resulting write
  outcome). A promote also drives ``facts_stored``/``duplicates_prevented`` via
  the write it performs; ``promotes`` counts the promote ACTION on top of that.

Counters ("totals") are SUM(count) over the requested day range — identical to
summing the ``series`` this module returns (``totals[m] == sum(series[i][m])``).
There is no separate lifetime total; a large ``range_days`` approximates one.

================================================================================
TWO AUDIENCES  (`stats(group_by=...)`)
================================================================================

`stats` surfaces two grains of the SAME rollup, selected by ``group_by``:

- ``group_by="space"`` (default) — the whole space/org: SUM across ALL principals
  in the space. An AGGREGATE only; no individual is ever broken out, so this is
  not sensitive within the space (any member may read it). ``principal`` in the
  response is ``null``.

- ``group_by="user"`` — the CALLING principal alone (``principal`` in the
  response is the caller's id). There is deliberately no argument to request
  ANOTHER principal's numbers, so one individual's per-user figures are exposed
  to nobody but themselves — the "don't leak an individual's numbers to a
  non-admin" boundary holds by CONSTRUCTION, not by a role check. A privileged
  cross-user breakdown (every principal's own line, for a dashboard) would be a
  separate future ``group_by`` value gated on the ``space_admin`` role; it is
  intentionally NOT built here (and would be the large payload ``group_by``
  exists to avoid).

Both grains read through the same space-scoped RLS SELECT policy, so ``stats``
can only ever touch the caller's own space.
"""
import datetime
import logging

from engraphy.server.db import transaction

logger = logging.getLogger(__name__)

# Metric keys (this list IS the frozen order the `stats` series/totals emit).
QUESTIONS_ASKED = "questions_asked"
ANSWERED = "answered"
MEMORY_REUSED = "memory_reused"
FACTS_STORED = "facts_stored"
DUPLICATES_PREVENTED = "duplicates_prevented"
PROMOTES = "promotes"

#: Every metric surfaced by `stats`, in stable order. Zero-filled per day.
METRICS = (
    QUESTIONS_ASKED,
    ANSWERED,
    MEMORY_REUSED,
    FACTS_STORED,
    DUPLICATES_PREVENTED,
    PROMOTES,
)

_DEFAULT_RANGE_DAYS = 30
# Generous ceiling: a caller wanting a lifetime-ish total passes a large range.
# Bounds the zero-fill work and the index scan; not a privacy or cost gate.
_MAX_RANGE_DAYS = 3650

# The write() / resolve_duplicate() outcome -> the metric it increments. A write
# is exactly one of these; anything absent (e.g. import-mode "review_queued") is
# deliberately uncounted. See the module docstring for the full rationale.
_WRITE_OUTCOME_METRIC = {
    "inserted": FACTS_STORED,
    "merged": DUPLICATES_PREVENTED,
    "merged_linked": DUPLICATES_PREVENTED,
    "needs_confirmation": DUPLICATES_PREVENTED,
}


def write_outcome_metric(outcome: str) -> str | None:
    """The metric a fresh ``write`` outcome increments, or None if uncounted."""
    return _WRITE_OUTCOME_METRIC.get(outcome)


async def bump_many(cur, space_id: str, principal: str, counts: dict, bucket_date=None) -> None:
    """Upsert increments for several metrics inside an ALREADY-OPEN transaction
    (the caller's `cur`). One `INSERT ... ON CONFLICT DO UPDATE` per metric with a
    non-zero count; zero/absent counts are skipped so no empty rows accrue.

    bucket_date defaults to the current UTC calendar date, computed in SQL
    (``(now() AT TIME ZONE 'UTC')::date``) so the bucket matches the DB clock and
    needs no Python clock. Tests pass an explicit date to backfill history and
    exercise multi-day zero-fill without a superuser.

    Raises on any DB error — this is the strict primitive. Production callers use
    `bump_safe`, which wraps this in its own transaction and swallows failures so
    a metrics problem never fails the operation being measured.
    """
    for metric, count in counts.items():
        if not count:
            continue
        if bucket_date is None:
            await cur.execute(
                "INSERT INTO metrics_rollup (space_id, principal, metric, bucket_date, count) "
                "VALUES (%s, %s, %s, (now() AT TIME ZONE 'UTC')::date, %s) "
                "ON CONFLICT (space_id, principal, metric, bucket_date) "
                "DO UPDATE SET count = metrics_rollup.count + excluded.count",
                (space_id, principal, metric, count),
            )
        else:
            await cur.execute(
                "INSERT INTO metrics_rollup (space_id, principal, metric, bucket_date, count) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (space_id, principal, metric, bucket_date) "
                "DO UPDATE SET count = metrics_rollup.count + excluded.count",
                (space_id, principal, metric, bucket_date, count),
            )


async def bump_safe(pool, space_id: str, principal: str, counts: dict) -> None:
    """Best-effort metrics bump in its OWN short transaction, run after the
    counted operation has already committed. Swallows every error (logging at
    WARNING): recording a usage counter must never roll back or fail the write /
    search / promote it measures, and a misconfigured metrics table (e.g. a
    missing GRANT on a freshly-migrated DB) must degrade to "counters stay flat",
    never to "the product stops working".

    The trade-off this makes explicit: the bump is NOT atomic with the operation,
    so a crash in the narrow window between the operation's commit and this bump
    loses one increment. That is acceptable for approximate usage counters and is
    the same reason the bump lives outside the operation's advisory-lock critical
    section (see migration 0021's contention note)."""
    counts = {m: c for m, c in counts.items() if c}
    if not counts:
        return
    try:
        async with transaction(pool, space_id, principal) as conn:
            await bump_many(conn.cursor(), space_id, principal, counts)
    except Exception:  # noqa: BLE001 -- observability side-effect, never fatal
        logger.warning(
            "metrics bump failed (non-fatal) for space=%s principal=%s metrics=%s",
            space_id, principal, sorted(counts), exc_info=True,
        )


GROUP_BY_VALUES = ("space", "user")


async def stats(
    pool, space_id: str, principal: str,
    range_days: int = _DEFAULT_RANGE_DAYS, group_by: str = "space",
) -> dict:
    """The read-only `stats` envelope: totals + a zero-filled daily series over
    the last ``range_days`` UTC days (inclusive of today), at the ``group_by``
    grain (see the module docstring's "TWO AUDIENCES").

    group_by="space" (default): SUM across every principal in the space
    (``principal`` in the response is None). group_by="user": the CALLING
    principal only (``principal`` is the caller's id). RLS scopes the read to the
    caller's space either way, so `stats` can only ever return the caller's own
    space. ``range_days`` is clamped to [1, _MAX_RANGE_DAYS]; ``group_by`` is
    validated (wire + dispatcher guard it too, defense in depth).

    Zero-fill: EVERY day in [today-(range_days-1) .. today] appears in ``series``
    with all metrics present (0 where absent), so the extension can chart the
    array directly with no gap handling. ``totals[m] == sum(series[i][m])``.
    """
    if group_by not in GROUP_BY_VALUES:
        raise ValueError(f"group_by must be one of {'|'.join(GROUP_BY_VALUES)}")
    range_days = max(1, min(int(range_days), _MAX_RANGE_DAYS))

    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()
        # Anchor the window on the DB clock in UTC, same expression the bump uses
        # for bucket_date, so "today" here and the bucket a concurrent bump lands
        # in agree. generated_at is the full timestamp for a dashboard "as of".
        await cur.execute("SELECT now(), (now() AT TIME ZONE 'UTC')::date")
        generated_at, today = await cur.fetchone()
        start = today - datetime.timedelta(days=range_days - 1)
        # RLS already limits to this space; the explicit space_id filter drives
        # the (space_id, metric, bucket_date) trend index. The space grain SUMs
        # across principals (aggregate, no individual attribution); the user
        # grain adds a principal filter so only the caller's own rows are summed
        # -- there is no way to name another principal, so no individual's numbers
        # leak to anyone but themselves.
        sql = ("SELECT bucket_date, metric, SUM(count) FROM metrics_rollup "
               "WHERE space_id = %s AND bucket_date BETWEEN %s AND %s")
        params = [space_id, start, today]
        result_principal = None
        if group_by == "user":
            sql += " AND principal = %s"
            params.append(principal)
            result_principal = principal
        sql += " GROUP BY bucket_date, metric"
        await cur.execute(sql, params)
        rows = await cur.fetchall()

    by_date: dict[datetime.date, dict[str, int]] = {}
    for bucket_date, metric, total in rows:
        by_date.setdefault(bucket_date, {})[metric] = int(total)

    series = []
    totals = {m: 0 for m in METRICS}
    for offset in range(range_days):
        day = start + datetime.timedelta(days=offset)
        day_counts = by_date.get(day, {})
        entry = {"date": day.isoformat()}
        for metric in METRICS:
            value = int(day_counts.get(metric, 0))
            entry[metric] = value
            totals[metric] += value
        series.append(entry)

    return {
        "v": 1,
        "space": space_id,
        "group_by": group_by,
        "principal": result_principal,   # the caller's id for the user grain; null for space
        "range_days": range_days,
        "generated_at": generated_at.isoformat(),
        "totals": totals,
        "series": series,
    }
