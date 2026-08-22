-- migrate:up
-- inbox, dedup_log, pending_writes, audit_log, api_tokens, config (design/01, design/03)
--
-- inbox/dedup_log shape and RLS: QUESTIONS.md "support-tables" (resolved 2026-07-15) --
-- Engram's own docs never state their RLS policy predicate or full column shape
-- (the upstream design deferred their exact RLS predicate and full column shape).
-- Built here with minimal E0-confirmed columns only, RLS
-- ENABLE+FORCE with deliberately NO policy (Postgres RLS defaults deny-all with
-- zero matching policies -- fail-closed, not a guessed predicate). E1 (which
-- implements inbox promotion and dedup banding) adds the real policy and full
-- write-path columns additively.

CREATE TABLE config (
  space_id text NOT NULL REFERENCES spaces(id),
  key      text NOT NULL,
  value    jsonb NOT NULL,
  PRIMARY KEY (space_id, key)
);

-- api_tokens: instance-level table (queried by the auth middleware before any
-- space GUC is set, to resolve which space a bearer token belongs to -- not
-- RLS-covered, matching visibility-and-rls-plan.md's table list, which omits it).
CREATE TABLE api_tokens (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  space_id     text NOT NULL REFERENCES spaces(id),
  principal    text NOT NULL,
  client_name  text NOT NULL,
  token_hash   text NOT NULL,
  role         text NOT NULL CHECK (role IN ('readwrite','readonly')),
  revoked      boolean NOT NULL DEFAULT false,
  last_used_at timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (space_id, principal, client_name),
  FOREIGN KEY (space_id, principal) REFERENCES principals (space_id, id)
);

-- pending_writes: policy given verbatim in visibility-and-rls-plan.md --
-- "space + author_principal = current principal — a parked write is visible
-- only to its author (it may quote private content)." `payload` is E1's write
-- path's to shape (candidates snapshot, requested node, etc.) — a single
-- jsonb column so E1 doesn't need a DDL change to add fields to it.
CREATE TABLE pending_writes (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  space_id         text NOT NULL REFERENCES spaces(id),
  author_principal text NOT NULL,
  payload          jsonb NOT NULL,
  expires_at       timestamptz NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE pending_writes ENABLE ROW LEVEL SECURITY;
ALTER TABLE pending_writes FORCE ROW LEVEL SECURITY;

CREATE POLICY pending_writes_read ON pending_writes FOR SELECT
  USING (space_id = current_setting('engram.space_id', true)
         AND author_principal = current_setting('engram.principal', true));

CREATE POLICY pending_writes_write ON pending_writes FOR INSERT
  WITH CHECK (space_id = current_setting('engram.space_id', true)
              AND author_principal = current_setting('engram.principal', true));

-- inbox: minimal E0-confirmed columns (status/scope_id per design/01's index
-- hint `(space_id, status) WHERE status='pending'`). No policy -- deny-all
-- until E1.
CREATE TABLE inbox (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  space_id   text NOT NULL REFERENCES spaces(id),
  scope_id   text,
  status     text NOT NULL DEFAULT 'pending',
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE inbox FORCE ROW LEVEL SECURITY;
-- (intentionally no policy yet)

-- dedup_log: minimal E0-confirmed columns. No policy -- deny-all until E1.
CREATE TABLE dedup_log (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  space_id   text NOT NULL REFERENCES spaces(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE dedup_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE dedup_log FORCE ROW LEVEL SECURITY;
-- (intentionally no policy yet)

-- audit_log: not in the RLS plan's table list (admin-CLI-only access via a
-- separate role per design/03 "instance administration ... local CLI only");
-- not RLS-covered. Column shape beyond what 03 states ("gains space_id and
-- client_name") is a boring default (DECISIONS-DELTA.md) -- not exercised
-- until E2's tool layer writes to it.
CREATE TABLE audit_log (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  space_id    text NOT NULL REFERENCES spaces(id),
  principal   text,
  client_name text,
  action      text NOT NULL,
  detail      jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- migrate:down
DROP TABLE audit_log;

DROP POLICY IF EXISTS dedup_log_read ON dedup_log;
ALTER TABLE dedup_log NO FORCE ROW LEVEL SECURITY;
ALTER TABLE dedup_log DISABLE ROW LEVEL SECURITY;
DROP TABLE dedup_log;

DROP POLICY IF EXISTS inbox_read ON inbox;
ALTER TABLE inbox NO FORCE ROW LEVEL SECURITY;
ALTER TABLE inbox DISABLE ROW LEVEL SECURITY;
DROP TABLE inbox;

DROP POLICY IF EXISTS pending_writes_write ON pending_writes;
DROP POLICY IF EXISTS pending_writes_read ON pending_writes;
ALTER TABLE pending_writes NO FORCE ROW LEVEL SECURITY;
ALTER TABLE pending_writes DISABLE ROW LEVEL SECURITY;
DROP TABLE pending_writes;

DROP TABLE api_tokens;
DROP TABLE config;
