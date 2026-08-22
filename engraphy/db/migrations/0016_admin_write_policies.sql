-- migrate:up
-- Admin write paths: the four admin_* tools (design/06 §Space administration,
-- E2-plan.md §5.5, DECISIONS-DELTA 2026-07-19 items 1a/2a). Three of the four
-- mutate an RLS-covered table -- admin_member_add INSERTs principals,
-- admin_scope_visibility UPDATEs scopes, admin_grant INSERTs scope_grants --
-- and each of those tables has had ENABLE+FORCE ROW LEVEL SECURITY since
-- 0002/0007 with deliberately NO write policy (fail-closed), exactly like
-- scopes_write (0015) and the inbox INSERT/UPDATE policies (0013/0014). This is
-- that land for the admin surface. admin_token_create needs nothing here:
-- api_tokens is instance-level, not RLS-covered.
--
-- Predicate (Devon-approved 2a): every admin write requires the CALLER to be a
-- space_admin -- space-match AND the acting principal (engram.principal) holds
-- role='space_admin' in this space. This makes RLS a faithful backstop to the
-- tool-layer gate: a non-space-admin caller already gets ENGRAM_ROLE at the
-- tool, and even if that gate were somehow bypassed, RLS still refuses the
-- write. The check is an inline EXISTS on principals (gated by principals_read,
-- a plain space-match SELECT policy -- the acting principal's own row is always
-- readable, so the subquery resolves; no recursion, since the subquery is a
-- SELECT and these are INSERT/UPDATE policies). Deliberately duplicated by the
-- app-layer gate's own `SELECT role FROM principals` check rather than shared
-- through a SECURITY DEFINER function: a boolean role test is trivially
-- equivalent in both spots and this avoids adding a function EXECUTE grant.
--
-- DEFERRED (2a): design/06 also grants a plain scope OWNER an owner-scoped path
-- to admin_scope_visibility/admin_grant on their own scopes. Not implemented --
-- these policies are space_admin-only. To honor it later, add
-- `OR owner_principal = current_setting('engram.principal', true)` (scopes) /
-- an analogous owner-of-scope check (scope_grants) alongside the admin EXISTS.
--
-- Grants are unchanged: principals, scopes, and scope_grants are already in
-- conftest's SELECT+INSERT+UPDATE _RLS_TABLES set, so under FORCE RLS a policy
-- is the only thing standing between a granted-but-denied write and a working
-- one.
--
-- scopes_admin_read (Devon-approved (b), 2026-07-19): a space_admin may SELECT
-- scope METADATA across the space. This exists because Postgres applies the
-- SELECT policy (not just UPDATE's USING) to the row-read of
-- `UPDATE scopes ... WHERE ... RETURNING ...` -- without it, admin_scope_
-- visibility cannot even target a scope the admin does not personally read,
-- defeating design/06's "space_admin: set visibility" for the space. Node
-- contents stay private: nodes_read gates on engram_readable_scopes() (a
-- SECURITY DEFINER function with its own query), NOT on this scopes SELECT
-- policy, so this widens only the scopes TABLE (scope_list + this tool's
-- read-back), never node visibility. Deliberate, documented consequence: a
-- space_admin's scope_list now returns every scope's metadata in the space
-- (id/display_name/visibility/owner), including other members' private scopes'
-- existence -- appropriate for the space's operator, and it reveals no node.

CREATE POLICY scopes_admin_read ON scopes FOR SELECT
  USING (
    space_id = current_setting('engram.space_id', true)
    AND EXISTS (SELECT 1 FROM principals a
                WHERE a.space_id = current_setting('engram.space_id', true)
                  AND a.id = current_setting('engram.principal', true)
                  AND a.role = 'space_admin'));

CREATE POLICY principals_write ON principals FOR INSERT
  WITH CHECK (
    space_id = current_setting('engram.space_id', true)
    AND EXISTS (SELECT 1 FROM principals a
                WHERE a.space_id = current_setting('engram.space_id', true)
                  AND a.id = current_setting('engram.principal', true)
                  AND a.role = 'space_admin'));

CREATE POLICY scopes_admin_update ON scopes FOR UPDATE
  USING (
    space_id = current_setting('engram.space_id', true)
    AND EXISTS (SELECT 1 FROM principals a
                WHERE a.space_id = current_setting('engram.space_id', true)
                  AND a.id = current_setting('engram.principal', true)
                  AND a.role = 'space_admin'))
  WITH CHECK (
    space_id = current_setting('engram.space_id', true)
    AND EXISTS (SELECT 1 FROM principals a
                WHERE a.space_id = current_setting('engram.space_id', true)
                  AND a.id = current_setting('engram.principal', true)
                  AND a.role = 'space_admin'));

CREATE POLICY scope_grants_write ON scope_grants FOR INSERT
  WITH CHECK (
    space_id = current_setting('engram.space_id', true)
    AND EXISTS (SELECT 1 FROM principals a
                WHERE a.space_id = current_setting('engram.space_id', true)
                  AND a.id = current_setting('engram.principal', true)
                  AND a.role = 'space_admin'));

-- migrate:down
DROP POLICY IF EXISTS scope_grants_write ON scope_grants;
DROP POLICY IF EXISTS scopes_admin_update ON scopes;
DROP POLICY IF EXISTS scopes_admin_read ON scopes;
DROP POLICY IF EXISTS principals_write ON principals;
