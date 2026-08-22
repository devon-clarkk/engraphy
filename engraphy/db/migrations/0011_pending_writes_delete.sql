-- migrate:up
-- pending_writes DELETE policy (design/implementation/dedup-write-path-plan.md
-- §Pending resolution against a changed world). QUESTIONS.md
-- "pending-write-consumption" (resolved 2026-07-16, Devon: option (a)) --
-- resolve_duplicate consumes the parked row on success, so the row must be
-- deletable by its author. This is the schema's FIRST and (so far) ONLY DELETE
-- policy: every other table is append-or-update-only by design (01: "no hard
-- deletes" for nodes; merge chains via canonical_id). pending_writes is not
-- memory -- it is a 24h scratch row holding an un-adopted payload -- so
-- consuming it destroys nothing a user ever committed to.
--
-- The policy mirrors pending_writes_read (0008) exactly: space + author, per
-- visibility-and-rls-plan.md's "a parked write is visible only to its author
-- (it may quote private content)". Deletability therefore never exceeds
-- visibility -- you can only consume what you could already see.
--
-- WHY THE POLICY AND THE GRANT ARE ONE UNIT (do not split them): with
-- GRANT DELETE but no policy, FORCE ROW LEVEL SECURITY makes the DELETE
-- succeed while matching zero rows -- no error, rowcount 0, row still present.
-- resolve_duplicate would then return a success envelope over a parked write
-- that is still resolvable, and a replay within the 24h TTL would insert a
-- second node from one payload -- exactly the duplicate the dedup pipeline
-- exists to prevent. Verified empirically before this migration was written.
-- The grant itself lives with the app role's other grants (engram/tests/
-- conftest.py today -- no doc gives literal role DDL; see DECISIONS-DELTA.md).

CREATE POLICY pending_writes_delete ON pending_writes FOR DELETE
  USING (space_id = current_setting('engram.space_id', true)
         AND author_principal = current_setting('engram.principal', true));

-- migrate:down
DROP POLICY IF EXISTS pending_writes_delete ON pending_writes;
