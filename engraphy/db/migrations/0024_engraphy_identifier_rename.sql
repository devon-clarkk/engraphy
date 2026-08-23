-- migrate:up
-- Finish the Engram -> Engraphy rename inside the database.
--
-- Until now a small set of database-internal identifiers deliberately kept the
-- `engram` prefix, and COMPATIBILITY.md documented them as frozen: renaming
-- them would have broken an already-provisioned cluster. That freeze is lifted.
-- The project is young enough that one operator-visible upgrade step is cheaper
-- than carrying two names forever, so this migration renames every remaining
-- `engram` identifier and COMPATIBILITY.md no longer promises otherwise.
--
-- What this renames:
--   functions  engram_addenda_text, engram_readable_scopes,
--              engram_writable_scopes, engram_validate_attrs  -> engraphy_*
--   GUCs       engram.space_id, engram.principal              -> engraphy.*
--   role       engram_app                                     -> engraphy_app
--   node type  engram_sentinel                                -> engraphy_sentinel
--
-- What it deliberately does NOT rename:
--   * The DATABASE name. `ALTER DATABASE ... RENAME TO` cannot run from a
--     session connected to that database, nor inside a transaction block, and
--     this migration is both. It is an operator step, documented in
--     COMPATIBILITY.md, not something a forward migration can do.
--   * Table names. There are none prefixed `engram_`; all 17 tables in `public`
--     are unprefixed. Nothing to do.
--   * A SQL schema named `engram`. There is none; everything lives in `public`.
--     `engram.` was only ever the custom-GUC namespace, handled below.
--
-- Why so much of this is dynamic SQL rather than literal DDL. The GUC namespace
-- is a string CONSTANT baked into ~23 policy expressions and two function
-- bodies; Postgres has no rename for it, so each policy must be dropped and
-- recreated. Transcribing that DDL by hand from engraphy/db/schema.sql would be
-- wrong twice over: schema.sql is a generated artifact, and a real install may
-- sit at any applied version (a deployment at 0021 has a different policy set
-- from one at 0023). Reading pg_policy / pg_get_functiondef at APPLY time and
-- substituting only the namespace literal keeps every other attribute -
-- PERMISSIVE/RESTRICTIVE, the command, the role list, SECURITY DEFINER, STABLE,
-- `SET search_path` - byte-identical to whatever is actually installed, instead
-- of to whatever a hand-copied snapshot happened to say.
--
-- The final assertion block is what makes that trustworthy: it re-scans every
-- surface and raises if a single `engram` identifier survived, so a substitution
-- that silently matched nothing fails the migration instead of shipping.

-- ---------------------------------------------------------------------------
-- 1. Functions: rename in place, never DROP/CREATE.
--
-- ALTER FUNCTION ... RENAME is OID-stable, so everything that references these
-- by OID follows transparently and for free: the RLS policy expressions, the
-- EXECUTE grants held by the app role, and - the one that matters most -
-- nodes.search, a `GENERATED ALWAYS AS (... engram_addenda_text(attrs) ...)
-- STORED` column. Dropping and recreating engram_addenda_text would either fail
-- on that dependency or force a full rewrite of the nodes table; renaming it
-- rewrites nothing.
ALTER FUNCTION public.engram_addenda_text(jsonb)          RENAME TO engraphy_addenda_text;
ALTER FUNCTION public.engram_readable_scopes()            RENAME TO engraphy_readable_scopes;
ALTER FUNCTION public.engram_writable_scopes()            RENAME TO engraphy_writable_scopes;
ALTER FUNCTION public.engram_validate_attrs(jsonb, jsonb) RENAME TO engraphy_validate_attrs;

-- ---------------------------------------------------------------------------
-- 2. Function BODIES that carry the old names as text.
--
-- Two distinct hazards, both invisible to any schema-shape check:
--   * engraphy_readable_scopes / engraphy_writable_scopes read
--     current_setting('engram.space_id') - a string constant the rename above
--     does not touch.
--   * nodes_validate_attrs_fn calls engram_validate_attrs(...) from a PL/pgSQL
--     body, which is resolved by NAME at runtime. Rename the function without
--     fixing this body and the catalog looks perfectly renamed while every
--     INSERT into nodes fails.
--
-- Each definition is round-tripped through pg_get_functiondef so the replacement
-- differs from the original in the substituted literal and nothing else. That is
-- the point: engraphy_readable_scopes is STABLE SECURITY DEFINER with
-- `SET search_path TO 'public'`, and retyping it by hand risks quietly dropping
-- one of those and weakening RLS without failing a superuser-run test.
--
-- The loop is bounded by an explicit allow-list of engine-owned functions so it
-- can never rewrite a pgvector function, and the `~ 'engram[._]'` guard means a
-- function with nothing to change is left alone entirely - which is how
-- engraphy_addenda_text (renamed above, body clean) stays untouched, avoiding a
-- CREATE OR REPLACE against the function the generated column depends on.
DO $mig$
DECLARE
  r        record;
  new_def  text;
  n_fixed  int := 0;
BEGIN
  FOR r IN
    SELECT p.oid, p.proname, pg_get_functiondef(p.oid) AS def
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
      AND p.proname IN (
        'engraphy_addenda_text', 'engraphy_readable_scopes',
        'engraphy_writable_scopes', 'engraphy_validate_attrs',
        'nodes_validate_attrs_fn', 'nodes_touch_fn', 'edges_validate_fn')
      AND pg_get_functiondef(p.oid) ~ 'engram[._]'
  LOOP
    -- '.' before '_': neither substitution can produce the other's pattern
    -- ('engraphy.' does not contain 'engram.'), so the order is safe either way,
    -- but fixing it makes the result reproducible rather than incidental.
    new_def := replace(replace(r.def, 'engram.', 'engraphy.'), 'engram_', 'engraphy_');
    EXECUTE new_def;
    n_fixed := n_fixed + 1;
    RAISE NOTICE '0024: rewrote body of %', r.proname;
  END LOOP;
  RAISE NOTICE '0024: % function body/bodies rewritten', n_fixed;
END
$mig$;

-- ---------------------------------------------------------------------------
-- 3. RLS policies: drop and recreate with the new GUC namespace.
--
-- Unavoidable - a policy expression cannot be ALTERed in place, and the
-- namespace is a literal inside it. Every attribute is carried across:
-- polpermissive -> AS PERMISSIVE/RESTRICTIVE, polcmd -> FOR ..., polroles -> TO
-- ... (an empty role array means the policy targets PUBLIC, which is the case
-- for all of them here, and omitting TO reproduces that exactly).
--
-- Function references inside these expressions need no substitution: step 1
-- already renamed the functions, and pg_get_expr renders them from the OID, so
-- the text read here ALREADY says engraphy_readable_scopes(). Only the GUC
-- literal is replaced. This is also why step 1 must run before this block.
DO $mig$
DECLARE
  r        record;
  stmt     text;
  cmd      text;
  n_fixed  int := 0;
BEGIN
  FOR r IN
    SELECT p.polname,
           c.relname,
           p.polpermissive,
           p.polcmd,
           pg_get_expr(p.polqual,      p.polrelid) AS qual,
           pg_get_expr(p.polwithcheck, p.polrelid) AS withcheck,
           ARRAY(SELECT quote_ident(rolname) FROM pg_roles WHERE oid = ANY(p.polroles)) AS roles
    FROM pg_policy p
    JOIN pg_class     c ON c.oid = p.polrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND (coalesce(pg_get_expr(p.polqual,      p.polrelid), '') ~ 'engram[._]'
        OR coalesce(pg_get_expr(p.polwithcheck, p.polrelid), '') ~ 'engram[._]')
  LOOP
    cmd := CASE r.polcmd
             WHEN 'r' THEN 'SELECT'
             WHEN 'a' THEN 'INSERT'
             WHEN 'w' THEN 'UPDATE'
             WHEN 'd' THEN 'DELETE'
             WHEN '*' THEN 'ALL'
           END;
    IF cmd IS NULL THEN
      RAISE EXCEPTION '0024: unrecognised polcmd % on policy %.%',
        r.polcmd, r.relname, r.polname;
    END IF;

    stmt := format('CREATE POLICY %I ON public.%I AS %s FOR %s',
                   r.polname, r.relname,
                   CASE WHEN r.polpermissive THEN 'PERMISSIVE' ELSE 'RESTRICTIVE' END,
                   cmd);

    IF array_length(r.roles, 1) IS NOT NULL THEN
      stmt := stmt || ' TO ' || array_to_string(r.roles, ', ');
    END IF;

    IF r.qual IS NOT NULL THEN
      stmt := stmt || ' USING ('
              || replace(replace(r.qual, 'engram.', 'engraphy.'), 'engram_', 'engraphy_')
              || ')';
    END IF;

    IF r.withcheck IS NOT NULL THEN
      stmt := stmt || ' WITH CHECK ('
              || replace(replace(r.withcheck, 'engram.', 'engraphy.'), 'engram_', 'engraphy_')
              || ')';
    END IF;

    EXECUTE format('DROP POLICY %I ON public.%I', r.polname, r.relname);
    EXECUTE stmt;
    n_fixed := n_fixed + 1;
  END LOOP;
  RAISE NOTICE '0024: % RLS policies rewritten onto the engraphy.* GUC namespace', n_fixed;
END
$mig$;

-- ---------------------------------------------------------------------------
-- 4. The application role.
--
-- Guarded on both sides so this is a no-op in the two cases where it should be:
-- a fresh install whose role has not been provisioned yet (deploy/
-- provision-app-role.sql runs AFTER migrate, so the role legitimately does not
-- exist at this point), and a cluster already carrying engraphy_app.
--
-- Table and EXECUTE grants are held against the role's OID, which the rename
-- preserves, so nothing needs re-granting. The password is the catch: a role's
-- md5 verifier is salted with the role NAME, so renaming an md5 role BLANKS its
-- password, while a SCRAM-SHA-256 verifier (the pg16 default) survives intact.
-- Rather than making the operator determine which they have, the live-apply plan
-- makes re-running deploy/provision-app-role.sql mandatory after this migration.
-- That script is idempotent, resets the password either way, and re-grants under
-- the new function names.
DO $mig$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_app')
     AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engraphy_app')
  THEN
    EXECUTE 'ALTER ROLE engram_app RENAME TO engraphy_app';
    RAISE NOTICE '0024: role engram_app renamed to engraphy_app. Update every DSN (ENGRAPHY_DATABASE_URL) and re-run deploy/provision-app-role.sql to reset the password.';
  ELSIF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engraphy_app') THEN
    RAISE NOTICE '0024: role engraphy_app already present, nothing to rename';
  ELSE
    RAISE NOTICE '0024: no engram_app role on this cluster (not yet provisioned), nothing to rename';
  END IF;
END
$mig$;

-- ---------------------------------------------------------------------------
-- 5. The reserved node type `engram_sentinel` -> `engraphy_sentinel`.
--
-- The ONLY part of this migration that touches stored DATA rather than schema
-- objects. It is included because the name is engine-owned and reserved (see
-- skills/contracts-and-reserved-names.md), so leaving it would mean the one
-- `engram` string still visible to an agent over the tool surface.
--
-- It cannot be a plain UPDATE. Three foreign keys reference
-- node_types(space_id, name) - from nodes, and twice from edge_rules - and all
-- three are ON UPDATE NO ACTION, so renaming the parent row out from under them
-- is rejected. Insert the new parent, repoint the children, drop the old parent.
--
-- The nodes UPDATE fires nodes_validate_attrs_fn, which revalidates against the
-- new type's attr_spec; that spec is copied verbatim from the old row, so the
-- validation is a tautology and cannot reject. It also fires nodes_touch_fn,
-- which bumps updated_at on the sentinel rows - cosmetic, and correct in the
-- sense that the row genuinely did change.
DO $mig$
DECLARE
  n_types int; n_nodes int; n_rules int;
BEGIN
  INSERT INTO node_types (space_id, name, description, attr_spec)
  SELECT space_id, 'engraphy_sentinel', description, attr_spec
  FROM node_types WHERE name = 'engram_sentinel'
  ON CONFLICT (space_id, name) DO NOTHING;
  GET DIAGNOSTICS n_types = ROW_COUNT;

  UPDATE nodes SET type = 'engraphy_sentinel' WHERE type = 'engram_sentinel';
  GET DIAGNOSTICS n_nodes = ROW_COUNT;

  UPDATE edge_rules SET src_type = 'engraphy_sentinel' WHERE src_type = 'engram_sentinel';
  GET DIAGNOSTICS n_rules = ROW_COUNT;
  UPDATE edge_rules SET dst_type = 'engraphy_sentinel' WHERE dst_type = 'engram_sentinel';

  DELETE FROM node_types WHERE name = 'engram_sentinel';

  RAISE NOTICE '0024: sentinel node type renamed in % space(s); % node(s), % edge rule(s) repointed',
    n_types, n_nodes, n_rules;
END
$mig$;

-- ---------------------------------------------------------------------------
-- 6. Assertions.
--
-- The migration is only worth trusting if it can prove it did what it claims.
-- Each block above substitutes into whatever it finds; a pattern that matched
-- nothing would leave the identifier behind and still commit. This re-scans
-- every surface independently and refuses to commit if any of them still
-- carries an `engram` identifier - including the generated column expression on
-- nodes.search, which nothing above writes to directly and which is therefore
-- the sharpest single check that the OID-stable function rename worked.
DO $mig$
DECLARE
  offender text;
BEGIN
  SELECT format('policy %s.%s', tablename, policyname) INTO offender
  FROM pg_policies
  WHERE schemaname = 'public'
    AND (coalesce(qual, '') ~ 'engram[._]' OR coalesce(with_check, '') ~ 'engram[._]')
  LIMIT 1;
  IF offender IS NOT NULL THEN
    RAISE EXCEPTION '0024 assertion failed: % still references an engram identifier', offender;
  END IF;

  SELECT format('function %s', p.proname) INTO offender
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'public'
    AND (p.proname ~ '^engram_' OR coalesce(p.prosrc, '') ~ 'engram[._]')
  LIMIT 1;
  IF offender IS NOT NULL THEN
    RAISE EXCEPTION '0024 assertion failed: % still references an engram identifier', offender;
  END IF;

  -- Generated columns and column defaults (nodes.search lives here).
  SELECT format('default/generated expr on %s.%s', c.relname, a.attname) INTO offender
  FROM pg_attrdef d
  JOIN pg_class c ON c.oid = d.adrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
  WHERE n.nspname = 'public' AND pg_get_expr(d.adbin, d.adrelid) ~ 'engram[._]'
  LIMIT 1;
  IF offender IS NOT NULL THEN
    RAISE EXCEPTION '0024 assertion failed: % still references an engram identifier', offender;
  END IF;

  -- CHECK constraints and index expressions.
  SELECT format('constraint %s on %s', conname, conrelid::regclass) INTO offender
  FROM pg_constraint
  WHERE connamespace = 'public'::regnamespace
    AND coalesce(pg_get_constraintdef(oid), '') ~ 'engram[._]'
  LIMIT 1;
  IF offender IS NOT NULL THEN
    RAISE EXCEPTION '0024 assertion failed: % still references an engram identifier', offender;
  END IF;

  -- Reserved-name data.
  IF EXISTS (SELECT 1 FROM node_types WHERE name ~ 'engram') THEN
    RAISE EXCEPTION '0024 assertion failed: node_types still carries an engram name';
  END IF;
  IF EXISTS (SELECT 1 FROM nodes WHERE type ~ 'engram') THEN
    RAISE EXCEPTION '0024 assertion failed: nodes.type still carries an engram name';
  END IF;

  -- The role. Not fatal when absent (see step 4), fatal when still old.
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_app') THEN
    RAISE EXCEPTION '0024 assertion failed: role engram_app still exists';
  END IF;

  RAISE NOTICE '0024: all assertions passed, no engram identifiers remain';
END
$mig$;

-- migrate:down
-- Exact inverse of the up-path, in reverse order. Same technique throughout:
-- read the installed definition and substitute the namespace literal back.
DO $mig$
BEGIN
  INSERT INTO node_types (space_id, name, description, attr_spec)
  SELECT space_id, 'engram_sentinel', description, attr_spec
  FROM node_types WHERE name = 'engraphy_sentinel'
  ON CONFLICT (space_id, name) DO NOTHING;
  UPDATE nodes       SET type     = 'engram_sentinel' WHERE type     = 'engraphy_sentinel';
  UPDATE edge_rules  SET src_type = 'engram_sentinel' WHERE src_type = 'engraphy_sentinel';
  UPDATE edge_rules  SET dst_type = 'engram_sentinel' WHERE dst_type = 'engraphy_sentinel';
  DELETE FROM node_types WHERE name = 'engraphy_sentinel';
END
$mig$;

DO $mig$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engraphy_app')
     AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_app')
  THEN
    EXECUTE 'ALTER ROLE engraphy_app RENAME TO engram_app';
  END IF;
END
$mig$;

DO $mig$
DECLARE
  r record; stmt text; cmd text;
BEGIN
  FOR r IN
    SELECT p.polname, c.relname, p.polpermissive, p.polcmd,
           pg_get_expr(p.polqual,      p.polrelid) AS qual,
           pg_get_expr(p.polwithcheck, p.polrelid) AS withcheck,
           ARRAY(SELECT quote_ident(rolname) FROM pg_roles WHERE oid = ANY(p.polroles)) AS roles
    FROM pg_policy p
    JOIN pg_class c ON c.oid = p.polrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND (coalesce(pg_get_expr(p.polqual,      p.polrelid), '') ~ 'engraphy[._]'
        OR coalesce(pg_get_expr(p.polwithcheck, p.polrelid), '') ~ 'engraphy[._]')
  LOOP
    cmd := CASE r.polcmd WHEN 'r' THEN 'SELECT' WHEN 'a' THEN 'INSERT'
                         WHEN 'w' THEN 'UPDATE' WHEN 'd' THEN 'DELETE'
                         WHEN '*' THEN 'ALL' END;
    stmt := format('CREATE POLICY %I ON public.%I AS %s FOR %s',
                   r.polname, r.relname,
                   CASE WHEN r.polpermissive THEN 'PERMISSIVE' ELSE 'RESTRICTIVE' END, cmd);
    IF array_length(r.roles, 1) IS NOT NULL THEN
      stmt := stmt || ' TO ' || array_to_string(r.roles, ', ');
    END IF;
    IF r.qual IS NOT NULL THEN
      stmt := stmt || ' USING ('
              || replace(replace(r.qual, 'engraphy.', 'engram.'), 'engraphy_', 'engram_') || ')';
    END IF;
    IF r.withcheck IS NOT NULL THEN
      stmt := stmt || ' WITH CHECK ('
              || replace(replace(r.withcheck, 'engraphy.', 'engram.'), 'engraphy_', 'engram_') || ')';
    END IF;
    EXECUTE format('DROP POLICY %I ON public.%I', r.polname, r.relname);
    EXECUTE stmt;
  END LOOP;
END
$mig$;

DO $mig$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT p.oid, pg_get_functiondef(p.oid) AS def
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
      AND p.proname IN (
        'engraphy_addenda_text', 'engraphy_readable_scopes',
        'engraphy_writable_scopes', 'engraphy_validate_attrs',
        'nodes_validate_attrs_fn', 'nodes_touch_fn', 'edges_validate_fn')
      AND pg_get_functiondef(p.oid) ~ 'engraphy[._]'
  LOOP
    EXECUTE replace(replace(r.def, 'engraphy.', 'engram.'), 'engraphy_', 'engram_');
  END LOOP;
END
$mig$;

ALTER FUNCTION public.engraphy_addenda_text(jsonb)          RENAME TO engram_addenda_text;
ALTER FUNCTION public.engraphy_readable_scopes()            RENAME TO engram_readable_scopes;
ALTER FUNCTION public.engraphy_writable_scopes()            RENAME TO engram_writable_scopes;
ALTER FUNCTION public.engraphy_validate_attrs(jsonb, jsonb) RENAME TO engram_validate_attrs;
