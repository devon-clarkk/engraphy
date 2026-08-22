-- migrate:up
-- Per-token scope restriction: a token marked `no_scope_all` is REFUSED
-- `scope='all'` by search and briefing (ENGRAM_SCOPE), rather than having the
-- call resolved to its principal's readable set and merely audited.
--
-- Why a token column and not a principal grant. Scope grants (`scope_grants`,
-- 0004) are keyed by PRINCIPAL, so they bound what `scope='all'` RETURNS for a
-- bearer without ever denying the call, and anything that later grants that
-- principal a scope for an unrelated reason silently widens the bearer's reach
-- with no diff to review. The control an unattended session needs is a property
-- of the CREDENTIAL: the same principal, holding a restricted token, cannot ask
-- the broad question at all. This is the token-scope-restriction design decision.
--
-- ADDITIVE and fail-OPEN on apply: NOT NULL DEFAULT false means every token that
-- already exists keeps working exactly as it did, with no restriction, and no
-- backfill is needed. A restriction is only ever acquired by minting a NEW token
-- with the flag set (auth.mint_token / `token create --no-scope-all` /
-- admin_token_create's no_scope_all argument). Applying this migration cannot
-- lock anybody out, which is the property that makes it safe to run first and
-- mint second.
ALTER TABLE api_tokens ADD COLUMN no_scope_all boolean NOT NULL DEFAULT false;

-- migrate:down
ALTER TABLE api_tokens DROP COLUMN no_scope_all;
