-- migrate:up
-- dedup_log real columns + RLS policy (design/02 §Deduplication write path,
-- design/implementation/dedup-write-path-plan.md step 7). QUESTIONS.md
-- "support-tables" (resolved 2026-07-15) built this table with minimal
-- E0-confirmed columns and no policy; E1 (this migration) adds the rest.
--
-- Shape/policy not stated anywhere in the docs (only "a dedup_log row for
-- every encounter, all bands -- the threshold-tuning dataset") -- DECISIONS-DELTA.md
-- gap: one row per write encounter (not per candidate), columns cover what
-- threshold governance (02 §Deduplication write path: "reviewed against
-- dedup_log outcomes") needs to reconstruct a band decision after the fact.
-- No SELECT policy -- same reasoning as audit_log: an internal analytics
-- table, never read through an MCP tool, so regular principals get zero rows
-- back (fail-closed) and admin-path reads use the superuser connection like
-- every other admin-CLI report in this repo.

ALTER TABLE dedup_log
  ADD COLUMN type             text NOT NULL DEFAULT '',
  ADD COLUMN node_id          uuid REFERENCES nodes(id),
  ADD COLUMN candidate_id     uuid REFERENCES nodes(id),
  ADD COLUMN similarity       real,
  ADD COLUMN band             text NOT NULL DEFAULT 'insert'
                               CHECK (band IN ('merge','pending','insert')),
  ADD COLUMN author_principal text NOT NULL DEFAULT '';

ALTER TABLE dedup_log ALTER COLUMN type DROP DEFAULT;
ALTER TABLE dedup_log ALTER COLUMN band DROP DEFAULT;
ALTER TABLE dedup_log ALTER COLUMN author_principal DROP DEFAULT;

CREATE POLICY dedup_log_write ON dedup_log FOR INSERT
  WITH CHECK (space_id = current_setting('engram.space_id', true));

-- migrate:down
DROP POLICY IF EXISTS dedup_log_write ON dedup_log;
ALTER TABLE dedup_log
  DROP COLUMN type,
  DROP COLUMN node_id,
  DROP COLUMN candidate_id,
  DROP COLUMN similarity,
  DROP COLUMN band,
  DROP COLUMN author_principal;
