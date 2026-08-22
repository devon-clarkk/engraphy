-- Provisions the `engram_app` DB role: the non-superuser, NOBYPASSRLS role
-- the running server connects as (design/01 s.Row-level security: "The
-- app's DB role is not BYPASSRLS"). Run ONCE per deployment, by a superuser,
-- against the target database, AFTER `dbmate up` (or `engraphy-admin migrate`)
-- has created the schema -- this script grants on tables/functions that
-- must already exist.
--
-- This is the production counterpart of engraphy/tests/conftest.py's
-- `_ensure_app_role` fixture, which provisions the identical role for tests
-- on every session. That fixture's own comment flags the gap this script
-- closes: "Production provisioning of the app role needs this same grant --
-- flag for the E3 deploy docs" (referring specifically to the
-- schema_migrations grant /healthz depends on). Keep this file's GRANT list
-- in sync with conftest.py's _RLS_TABLES / _READ_ONLY_TABLES /
-- _UNGATED_WRITE_TABLES / _DELETE_TABLES if the schema's table set changes.
--
-- Usage (cloud/compose profile -- runs in the `admin` sidecar, which carries
-- psql; nothing needs to be installed on the host, and postgres needs no
-- published port because the sidecar is on the compose network):
--   docker compose --profile admin run --rm admin \
--     psql "$ENGRAPHY_DATABASE_URL" \
--       -v app_role_password="$ENGRAPHY_APP_ROLE_PASSWORD" \
--       -f deploy/provision-app-role.sql
--
-- Usage (local/overlay profile -- psql on the host):
--   psql "$ENGRAPHY_DATABASE_URL" \
--     -v app_role_password="$(openssl rand -base64 32)" \
--     -f deploy/provision-app-role.sql
--
-- (or paste the password in directly -- :'app_role_password' below is a
-- psql variable substitution, not a literal to edit in this file.)

\set ON_ERROR_STOP on

-- psql's :'var' substitution does NOT happen inside dollar-quoted ($$...$$)
-- strings (deliberately, so it never mangles a PL/pgSQL body's own `:=`) --
-- so the password can't be referenced from inside a DO block directly. This
-- builds the CREATE/ALTER ROLE statement as plain text (substitution DOES
-- happen here, outside any $$ block) and \gexec's it instead.
-- NOTE the deliberate absence of a terminating `;` before `\gexec` below.
-- With a semicolon, psql executes the SELECT *and prints its result* -- which
-- is the fully-substituted `CREATE ROLE ... PASSWORD '<secret>'` text -- into
-- the operator's terminal, shell scrollback, and any CI log, and THEN \gexec
-- re-runs the same buffer. Ending the statement with `\gexec` alone both
-- suppresses that echo (\gexec executes the returned rows instead of
-- displaying them) and runs the query exactly once.
SELECT format('CREATE ROLE engram_app LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE',
              :'app_role_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_app')
UNION ALL
SELECT format('ALTER ROLE engram_app PASSWORD %L', :'app_role_password')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_app')
\gexec

-- GRANT ... ON DATABASE needs a literal identifier, not an expression --
-- current_database() can't be spliced in directly, so this goes through
-- dynamic SQL too (mirrors conftest.py's Python-side f-string equivalent;
-- no psql variable involved here, so a plain DO block is fine).
DO $$
BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO engram_app', current_database());
END $$;

GRANT USAGE ON SCHEMA public TO engram_app;

-- RLS-gated tables: the app role needs SELECT/INSERT/UPDATE to reach them at
-- all, but FORCE ROW LEVEL SECURITY (already set by the migrations) is what
-- actually restricts which rows -- these GRANTs alone would let the role see
-- everything if RLS were ever disabled, which is exactly why FORCE matters.
GRANT SELECT, INSERT, UPDATE ON
  nodes, edges, scopes, scope_grants, principals, pending_writes, inbox, dedup_log,
  metrics_rollup
  TO engram_app;

-- The schema's only DELETE grant (resolve_duplicate consumes the parked row
-- it resolves) -- see migration 0011's header comment.
GRANT DELETE ON pending_writes TO engram_app;

-- Read-only support tables (registries + config -- written only by
-- engraphy-admin pack apply/upgrade, never by the running server).
GRANT SELECT ON spaces, node_types, edge_types, edge_rules, config TO engram_app;

-- Not RLS-covered (instance-level / append-only audit).
GRANT SELECT, INSERT, UPDATE ON api_tokens, audit_log TO engram_app;

GRANT EXECUTE ON FUNCTION engram_readable_scopes() TO engram_app;
GRANT EXECUTE ON FUNCTION engram_writable_scopes() TO engram_app;
GRANT EXECUTE ON FUNCTION engram_validate_attrs(jsonb, jsonb) TO engram_app;

-- /healthz reads schema_migrations through the app-role pool
-- (applied_schema_version) -- unconditional here, unlike conftest.py's test
-- fixture, because a production DB always has this table by the time this
-- script runs (dbmate/engraphy-admin migrate creates it before anything else
-- connects).
GRANT SELECT ON schema_migrations TO engram_app;
