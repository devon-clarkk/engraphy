-- migrate:up
-- Phase B (design/analysis/fact-searchability-model.md §2; implementation
-- fact-searchability-phase-b.md §2.5): the merge path no longer forces a
-- distinct-but-near-duplicate write to choose between "second row" and
-- "invisible addendum" -- it inserts the incoming as its own searchable member
-- row and links it to the canonical with a `same_topic` edge. That write is
-- logged with band `merge_linked`. The addenda-promote migration
-- (`engram-admin addenda promote`), which rescues facts already buried as
-- addenda in existing stores, logs its inserts with band `merge_linked_promoted`.
-- Both are additive band values; the CHECK is widened to admit them. Nothing
-- re-embeds and no existing row changes -- see the spec's §5 on why no threshold
-- value moves in this phase.
--
-- The constraint is dropped by its auto-generated name (`dedup_log_band_check`,
-- from the inline `CHECK (band IN ...)` in 0010) and re-added under the same
-- name so a later migration can find it identically.
ALTER TABLE dedup_log DROP CONSTRAINT dedup_log_band_check;
ALTER TABLE dedup_log ADD CONSTRAINT dedup_log_band_check
  CHECK (band IN ('merge','pending','insert','merge_linked','merge_linked_promoted'));

-- migrate:down
-- Fail loudly rather than silently: the old CHECK cannot admit the new band
-- values, so down-migrating a store that has already logged a merge-link would
-- either error opaquely on the ADD CONSTRAINT or (worse, if forced) orphan the
-- provenance record. Refuse up front with a message that names the fix.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM dedup_log
    WHERE band IN ('merge_linked','merge_linked_promoted')
  ) THEN
    RAISE EXCEPTION 'cannot down-migrate 0018: dedup_log has rows with band '
      'merge_linked / merge_linked_promoted, which the pre-Phase-B CHECK '
      'rejects; these are the authoritative membership records for Phase B '
      'merge-links and must not be dropped';
  END IF;
END $$;
ALTER TABLE dedup_log DROP CONSTRAINT dedup_log_band_check;
ALTER TABLE dedup_log ADD CONSTRAINT dedup_log_band_check
  CHECK (band IN ('merge','pending','insert'));
