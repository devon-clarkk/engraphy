-- migrate:up
-- scope_create's write path: scopes has had ENABLE+FORCE RLS and a SELECT
-- policy since 0007, deliberately with no INSERT policy yet -- 0007's own
-- comment scopes it to "the five tables that already exist by 0007", and
-- like inbox's INSERT/UPDATE policies (0013 -> 0014, "lands with the inbox
-- tool itself later in E1/E2 -- deny-all until then"), scope_create is that
-- landing for scopes. FORCE RLS means no policy = no INSERT at all, which is
-- exactly why scope_create cannot function without this.
--
-- WITH CHECK: space match AND owner_principal = the caller's own identity --
-- a principal may create a scope, but only one it owns (scope_create always
-- sets owner_principal=principal; this policy is what makes that the ONLY
-- legal value, not merely the application's convention -- a caller cannot
-- spoof a different owner_principal). No grant needed: scopes is already in
-- conftest's SELECT+INSERT _RLS_TABLES set (matching every other write-path
-- table's grant story).
CREATE POLICY scopes_write ON scopes FOR INSERT
  WITH CHECK (space_id = current_setting('engram.space_id', true)
              AND owner_principal = current_setting('engram.principal', true));

-- migrate:down
DROP POLICY IF EXISTS scopes_write ON scopes;
