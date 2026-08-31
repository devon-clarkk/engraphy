-- migrate:up
-- Make (space_id, principal, client_name) unique among LIVE tokens only, so a
-- revoked credential can be replaced by a fresh one under the same client name.
--
-- Why this is needed. `token revoke` sets `revoked = true` and leaves the row in
-- place, because the schema carries a no-hard-deletes invariant and a revoked
-- row is the audit record that the credential once existed. The table-level
-- UNIQUE (space_id, principal, client_name) from 0008 counts that retained row,
-- so re-minting for the same client name raises a unique violation and rotation
-- is impossible.
--
-- Why not mint under a different client name. `client_name` becomes
-- `source_client` on every node the token writes, so it is provenance. Minting
-- `engraphy-agent-2` to dodge the constraint would make every fact written
-- after a rotation claim to come from a client that does not exist, and the
-- provenance of everything written before it would silently mean something
-- different from the provenance of everything after.
--
-- The property the constraint was protecting is preserved exactly: at most one
-- LIVE token per (space, principal, client name). What changes is that revoked
-- rows no longer occupy the slot. `auth.resolve_token` reads by `token_hash`
-- and checks `revoked`, so multiple revoked rows sharing a triple are inert.
--
-- BOTH statements are required. Adding the index without dropping the
-- constraint leaves the original UNIQUE in force and changes nothing.
--
-- Safe to apply to a populated table: every existing row is live-or-revoked and
-- already satisfies the narrower key, because the wider key it satisfied today
-- implies it. Nothing is rewritten and no token is invalidated.
ALTER TABLE api_tokens DROP CONSTRAINT api_tokens_space_id_principal_client_name_key;

CREATE UNIQUE INDEX api_tokens_live_identity_key
    ON api_tokens (space_id, principal, client_name)
    WHERE NOT revoked;

-- migrate:down
-- Restoring the table-level UNIQUE re-imposes the wider key over every row,
-- live and revoked alike. On a database where a rotation has already happened
-- there is a revoked row and a live row sharing a triple, and this statement
-- FAILS on that data. That is the honest behaviour: the down migration cannot
-- put back a constraint the data no longer satisfies, and silently deleting the
-- revoked history to make room would destroy the audit record that the whole
-- retention rule exists to keep.
--
-- Down is therefore clean on a database that has not rotated, and refuses on
-- one that has. An operator who genuinely needs to go back past this point
-- restores the pre-migration dump instead.
DROP INDEX api_tokens_live_identity_key;

ALTER TABLE api_tokens
    ADD CONSTRAINT api_tokens_space_id_principal_client_name_key
    UNIQUE (space_id, principal, client_name);
