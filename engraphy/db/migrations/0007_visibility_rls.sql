-- migrate:up
-- engram_readable_scopes()/engram_writable_scopes() (SECURITY DEFINER — see
-- design/implementation/visibility-and-rls-plan.md, do NOT "fix" the definer),
-- ENABLE+FORCE RLS + policies on ALL data tables (exact shapes in the plan)
--
-- Sequencing note (DECISIONS-DELTA.md): the plan's table list for ENABLE+FORCE
-- is nodes, edges, inbox, pending_writes, dedup_log, scopes, scope_grants,
-- principals -- but inbox/pending_writes/dedup_log don't exist until 0008
-- (support tables). This migration covers the five tables that already exist
-- by 0007 (scopes, scope_grants, principals, nodes, edges); 0008 enables RLS
-- on pending_writes/inbox/dedup_log immediately after creating them, in the
-- same file, per the plan's "one migration file, reviewed as a unit" intent
-- applied per-table rather than literally one file (the stub numbering,
-- 0007 before 0008, predates this migration's content and is preserved
-- per IMPLEMENTER.md: match the existing stubs' structure).

-- The single-authority visibility functions. SECURITY DEFINER breaks the RLS
-- recursion loop (policies on nodes/edges call these; if the functions were
-- themselves subject to the caller's RLS on scopes, evaluating a policy would
-- evaluate a policy). current_setting(..., true) returns NULL when unset ->
-- empty result set -> deny-all; there is deliberately no default value.
CREATE FUNCTION engram_readable_scopes() RETURNS SETOF text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT s.id FROM scopes s
  WHERE s.space_id = current_setting('engram.space_id', true)
    AND s.archived = false
    AND ( s.visibility IN ('team-read', 'team-write')
          OR s.owner_principal = current_setting('engram.principal', true)
          OR EXISTS (SELECT 1 FROM scope_grants g
                     WHERE g.space_id = s.space_id AND g.scope_id = s.id
                       AND g.principal = current_setting('engram.principal', true)) )
$$;

CREATE FUNCTION engram_writable_scopes() RETURNS SETOF text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT s.id FROM scopes s
  WHERE s.space_id = current_setting('engram.space_id', true)
    AND s.archived = false
    AND ( s.visibility = 'team-write'
          OR s.owner_principal = current_setting('engram.principal', true)
          OR EXISTS (SELECT 1 FROM scope_grants g
                     WHERE g.space_id = s.space_id AND g.scope_id = s.id
                       AND g.principal = current_setting('engram.principal', true)
                       AND g.level = 'write') )
$$;

-- scopes: existence is information -- SELECT policy is space match AND id IN
-- readable, not merely space-pinned (a private scope's very existence must
-- not leak to a non-owner, non-grantee member).
ALTER TABLE scopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE scopes FORCE ROW LEVEL SECURITY;

CREATE POLICY scopes_read ON scopes FOR SELECT
  USING (space_id = current_setting('engram.space_id', true)
         AND id IN (SELECT engram_readable_scopes()));

-- scope_grants, principals: space-pinned only -- a team roster (and who has
-- which grant) is not secret within its space (design/implementation/
-- visibility-and-rls-plan.md §RLS policies).
ALTER TABLE scope_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE scope_grants FORCE ROW LEVEL SECURITY;

CREATE POLICY scope_grants_read ON scope_grants FOR SELECT
  USING (space_id = current_setting('engram.space_id', true));

ALTER TABLE principals ENABLE ROW LEVEL SECURITY;
ALTER TABLE principals FORCE ROW LEVEL SECURITY;

CREATE POLICY principals_read ON principals FOR SELECT
  USING (space_id = current_setting('engram.space_id', true));

-- nodes
ALTER TABLE nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE nodes FORCE ROW LEVEL SECURITY;

CREATE POLICY nodes_read ON nodes FOR SELECT
  USING (space_id = current_setting('engram.space_id', true)
         AND scope_id IN (SELECT engram_readable_scopes()));

CREATE POLICY nodes_write ON nodes FOR INSERT
  WITH CHECK (space_id = current_setting('engram.space_id', true)
              AND scope_id IN (SELECT engram_writable_scopes()));

-- UPDATE gets BOTH: USING (readable -- you must see it) AND WITH CHECK (writable target).
CREATE POLICY nodes_update ON nodes FOR UPDATE
  USING (space_id = current_setting('engram.space_id', true)
         AND scope_id IN (SELECT engram_readable_scopes()))
  WITH CHECK (space_id = current_setting('engram.space_id', true)
              AND scope_id IN (SELECT engram_writable_scopes()));

-- edges
ALTER TABLE edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE edges FORCE ROW LEVEL SECURITY;

-- Both-endpoint rule via composition: the inner `nodes` subqueries are
-- themselves RLS-filtered for the session, so "both endpoints readable"
-- falls out rather than being re-implemented.
CREATE POLICY edges_read ON edges FOR SELECT
  USING (space_id = current_setting('engram.space_id', true)
         AND EXISTS (SELECT 1 FROM nodes n WHERE n.id = edges.src_id)
         AND EXISTS (SELECT 1 FROM nodes n WHERE n.id = edges.dst_id));

-- Creation rule (design/06 §Edge and traversal visibility): P may create an
-- edge iff P can read BOTH endpoints and write AT LEAST ONE. Same composition
-- trick as edges_read for the read-both half; writable_scopes for the
-- write-one half. This is the RLS backstop for the rule the write path also
-- enforces against engram_writable_scopes() before attempting the insert
-- (design/01 §Trigger enforcement: "the trigger enforces the structural
-- half, the auth layer the principal half") -- two independent layers.
CREATE POLICY edges_write ON edges FOR INSERT
  WITH CHECK (
    space_id = current_setting('engram.space_id', true)
    AND EXISTS (SELECT 1 FROM nodes n WHERE n.id = src_id)
    AND EXISTS (SELECT 1 FROM nodes n WHERE n.id = dst_id)
    AND (
      EXISTS (SELECT 1 FROM nodes n WHERE n.id = src_id
              AND n.scope_id IN (SELECT engram_writable_scopes()))
      OR EXISTS (SELECT 1 FROM nodes n WHERE n.id = dst_id
                 AND n.scope_id IN (SELECT engram_writable_scopes()))
    )
  );

-- migrate:down
DROP POLICY IF EXISTS edges_write ON edges;
DROP POLICY IF EXISTS edges_read ON edges;
ALTER TABLE edges NO FORCE ROW LEVEL SECURITY;
ALTER TABLE edges DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS nodes_update ON nodes;
DROP POLICY IF EXISTS nodes_write ON nodes;
DROP POLICY IF EXISTS nodes_read ON nodes;
ALTER TABLE nodes NO FORCE ROW LEVEL SECURITY;
ALTER TABLE nodes DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS principals_read ON principals;
ALTER TABLE principals NO FORCE ROW LEVEL SECURITY;
ALTER TABLE principals DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS scope_grants_read ON scope_grants;
ALTER TABLE scope_grants NO FORCE ROW LEVEL SECURITY;
ALTER TABLE scope_grants DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS scopes_read ON scopes;
ALTER TABLE scopes NO FORCE ROW LEVEL SECURITY;
ALTER TABLE scopes DISABLE ROW LEVEL SECURITY;

DROP FUNCTION IF EXISTS engram_writable_scopes();
DROP FUNCTION IF EXISTS engram_readable_scopes();
