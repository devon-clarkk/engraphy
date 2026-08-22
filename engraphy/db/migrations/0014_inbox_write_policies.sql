-- migrate:up
-- Inbox write-path: the capture columns and the INSERT/UPDATE RLS policies the
-- inbox tool needs (design/02 §The inbox, E1 inbox module). 0008 created inbox
-- with minimal E0-confirmed columns and ENABLE+FORCE + deliberately NO policy
-- (fail-closed); 0013 added the SELECT policy for the briefing footer and noted
-- "Inbox INSERT (capture) and UPDATE (promote/discard) policies land with the
-- inbox tool itself later in E1/E2 -- deny-all until then". This is that land.
--
-- Columns (DECISIONS-DELTA.md 2026-07-17): `kind` + `payload` carry the
-- POST /inbox {kind, payload, scope?} capture inputs ("what captures is the
-- client's business" -- payload is opaque, mirroring pending_writes' single
-- jsonb column). Added NULLABLE: additive to 0008's table, so the count-only
-- inbox INSERTs that E0/briefing seeds already do (id/space/scope/status only)
-- keep working; capture() always populates both at the application layer.
--
-- RLS predicates (Devon, 2026-07-17): INSERT and UPDATE both
--   space AND (scope_id IS NULL OR scope_id IN writable)
-- UPDATE uses the WRITABLE test -- only a principal who can write the item's
-- scope may promote or discard it (promotion writes a node into that scope;
-- discard mutates the row). The NULL branch keeps an unscoped capture
-- ("pin it, file later") capturable/actionable by anyone in the space, matching
-- 0013's SELECT policy so an unscoped item is uniformly visible-and-actionable.
-- Same author-agnostic posture as the SELECT policy (inbox triage is shared,
-- unlike pending_writes which is author-private). Grants are unchanged: inbox is
-- already in conftest's SELECT+INSERT+UPDATE _RLS_TABLES set; FORCE RLS keeps an
-- INSERT/UPDATE with no policy denied, so adding the policies is what opens the
-- write path -- no new grant, unlike 0011's DELETE special case.

ALTER TABLE inbox ADD COLUMN kind text;
ALTER TABLE inbox ADD COLUMN payload jsonb;

CREATE POLICY inbox_write ON inbox FOR INSERT
  WITH CHECK (space_id = current_setting('engram.space_id', true)
             AND (scope_id IS NULL
                  OR scope_id IN (SELECT engram_writable_scopes())));

CREATE POLICY inbox_update ON inbox FOR UPDATE
  USING (space_id = current_setting('engram.space_id', true)
         AND (scope_id IS NULL
              OR scope_id IN (SELECT engram_writable_scopes())))
  WITH CHECK (space_id = current_setting('engram.space_id', true)
              AND (scope_id IS NULL
                   OR scope_id IN (SELECT engram_writable_scopes())));

-- migrate:down
DROP POLICY IF EXISTS inbox_update ON inbox;
DROP POLICY IF EXISTS inbox_write ON inbox;
ALTER TABLE inbox DROP COLUMN IF EXISTS payload;
ALTER TABLE inbox DROP COLUMN IF EXISTS kind;
