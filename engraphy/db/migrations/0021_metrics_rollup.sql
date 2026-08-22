-- migrate:up
-- Per-space usage metrics as a PRE-AGGREGATED time-series that also rolls up to
-- counters (the `stats` MCP tool -- design/03's tool surface, E3 value-proof).
--
-- Shape: one row per (space_id, principal, metric, bucket_date), carrying an
-- integer count, incremented via `INSERT ... ON CONFLICT DO UPDATE SET count =
-- count + excluded.count`. Day-granularity buckets in UTC keep the table small
-- (at most one row per metric per principal per day) and make trend queries a
-- bounded index range-scan rather than a scan over an unbounded raw-event log.
-- Counters are SUM(count) over the bucket range; there is deliberately NO
-- parallel lifetime-total table (a SUM over <= range_days rows per metric is
-- already trivially fast on the trend index, and a lifetime-total row would be
-- a HOTTER contention point -- one row per (space,principal,metric) for all
-- time -- for no query the range SUM does not already answer cheaply).
--
-- BOTH space_id and principal ride every row so per-space AND per-principal
-- rollups are possible later; the E3 MVP `stats` tool surfaces per-space only
-- (SUM across principals), but the key is already there for a per-user view.
--
-- Contention ceiling (honest flag, per the feature brief): concurrent counted
-- calls that target the SAME (space, principal, metric, bucket_date) row
-- serialize on that row's update lock. At demo / single-writer scale this is a
-- non-issue. The documented FUTURE optimizations, NOT built here because demo
-- scale does not need them: bucket-sharding (spread a hot day across N shard
-- rows summed at read time) and batched-flush (accumulate in-process and flush
-- one upsert per interval). The engine mitigates today by keeping the bump in
-- its OWN short transaction, OUTSIDE the counted operation's transaction and its
-- advisory lock, so a hot metrics row never extends a write's critical section.
--
-- RLS: same space-scoped pattern as the rest of the engine (migration 0007).
-- The metrics tables carry no scope_id -- usage counts are space-level, not
-- scope-level -- so the predicate is a plain space match, and `stats` therefore
-- only ever returns the caller's own space. SELECT is space-scoped (any member
-- reads the space's aggregate); INSERT/UPDATE are additionally principal-scoped
-- (a bump writes only the caller's own row, mirroring pending_writes_write).

-- space_id FK is ON DELETE CASCADE -- the ONLY cascade in the schema, and
-- deliberately so. Every other table's space FK is a plain reference because
-- those rows are KNOWLEDGE under the "no hard deletes" invariant (design/01);
-- metrics_rollup is pure DERIVED TELEMETRY, meaningless without its space, so if
-- a space is ever removed its usage counters should go with it rather than
-- orphan. In production this never fires (spaces are created, never deleted --
-- design/01/04), so it is behavior-neutral there; its practical effect is that
-- deleting a space cleans up its rollup rows without every caller having to know
-- this table exists (which is what test teardowns rely on).
CREATE TABLE metrics_rollup (
  space_id    text NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
  principal   text NOT NULL,
  metric      text NOT NULL,
  bucket_date date NOT NULL,
  count       bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (space_id, principal, metric, bucket_date)
);

-- Trend query index: `stats` scans (space_id, metric) over a bounded
-- bucket_date range. The PK's leading (space_id, principal, ...) does not serve
-- the principal-agnostic per-space trend scan, so this second index is what
-- makes the read a range-scan rather than a filter over the whole space.
CREATE INDEX metrics_rollup_trend ON metrics_rollup (space_id, metric, bucket_date);

ALTER TABLE metrics_rollup ENABLE ROW LEVEL SECURITY;
ALTER TABLE metrics_rollup FORCE ROW LEVEL SECURITY;

-- Read: space-scoped only. Any principal in the space may read the space's
-- aggregate usage (the MVP surfaces per-space totals + series). A private-scope
-- style predicate is deliberately NOT used: metrics carry no scope.
CREATE POLICY metrics_rollup_read ON metrics_rollup FOR SELECT
  USING (space_id = current_setting('engram.space_id', true));

-- Write: space AND principal match -- a bump may only create the caller's own
-- (space, principal, ...) row (same shape as pending_writes_write). INSERT ...
-- ON CONFLICT DO UPDATE needs BOTH the INSERT WITH CHECK and the UPDATE
-- USING/WITH CHECK to be satisfied for the conflicting row, so all three are
-- present and identically predicated.
CREATE POLICY metrics_rollup_write ON metrics_rollup FOR INSERT
  WITH CHECK (space_id = current_setting('engram.space_id', true)
              AND principal = current_setting('engram.principal', true));

CREATE POLICY metrics_rollup_update ON metrics_rollup FOR UPDATE
  USING (space_id = current_setting('engram.space_id', true)
         AND principal = current_setting('engram.principal', true))
  WITH CHECK (space_id = current_setting('engram.space_id', true)
              AND principal = current_setting('engram.principal', true));

-- migrate:down
DROP POLICY IF EXISTS metrics_rollup_update ON metrics_rollup;
DROP POLICY IF EXISTS metrics_rollup_write ON metrics_rollup;
DROP POLICY IF EXISTS metrics_rollup_read ON metrics_rollup;
ALTER TABLE metrics_rollup NO FORCE ROW LEVEL SECURITY;
ALTER TABLE metrics_rollup DISABLE ROW LEVEL SECURITY;
DROP TABLE metrics_rollup;
