-- migrate:up
-- inbox SELECT policy (E1 briefing footer). 0008 created inbox ENABLE+FORCE
-- with deliberately NO policy (fail-closed until E1 needed a real predicate);
-- the briefing footer's inbox_pending_count is that need. Predicate per the
-- E1 briefing gate (Devon, 2026-07-16, recorded in DECISIONS-DELTA.md):
--
--   space match AND (scope_id IS NULL OR scope_id IN readable)
--
-- scope_id IS NULL rows are deliberate: an unscoped inbox item ("pin it,
-- I'll file it later") must surface in EVERY briefing footer rather than
-- being orphaned behind a scope nobody asked about. A scoped row stays
-- invisible to principals who cannot read its scope -- captured content may
-- quote private material, so visibility never exceeds the scope's own.
--
-- SELECT only. Inbox INSERT (capture) and UPDATE (promote/discard) policies
-- land with the inbox tool itself later in E1/E2 -- deny-all until then, same
-- additive sequencing 0008 used for the table itself.

CREATE POLICY inbox_read ON inbox FOR SELECT
  USING (space_id = current_setting('engram.space_id', true)
         AND (scope_id IS NULL
              OR scope_id IN (SELECT engram_readable_scopes())));

-- migrate:down
DROP POLICY IF EXISTS inbox_read ON inbox;
