-- migrate:up
-- admin_member_archive (design/06 §Space administration: a space_admin may add
-- and archive members). The tool UPDATEs principals.archived, and principals has
-- carried ENABLE + FORCE ROW LEVEL SECURITY since 0007 with no UPDATE policy, so
-- that write fails closed until this policy exists.
--
-- The predicate is 0016's, unchanged: space-match AND the acting principal holds
-- role='space_admin' in this space. It is the RLS backstop to admin.py's
-- _assert_space_admin gate, exactly as 0016's policies are for the other admin
-- writes, so even with the app gate bypassed a plain member's transaction
-- cannot change a principal row.
--
-- principals_read (0007) is untouched, so token resolution and the door check
-- (auth.require_active_principal) read exactly what they read before this
-- migration. The policy admits any column; the one UPDATE the server issues on
-- principals is admin_member_archive's `SET archived`.

CREATE POLICY principals_admin_update ON principals FOR UPDATE
  USING (
    space_id = current_setting('engraphy.space_id', true)
    AND EXISTS (SELECT 1 FROM principals a
                WHERE a.space_id = current_setting('engraphy.space_id', true)
                  AND a.id = current_setting('engraphy.principal', true)
                  AND a.role = 'space_admin'))
  WITH CHECK (
    space_id = current_setting('engraphy.space_id', true)
    AND EXISTS (SELECT 1 FROM principals a
                WHERE a.space_id = current_setting('engraphy.space_id', true)
                  AND a.id = current_setting('engraphy.principal', true)
                  AND a.role = 'space_admin'));

-- migrate:down
DROP POLICY IF EXISTS principals_admin_update ON principals;
