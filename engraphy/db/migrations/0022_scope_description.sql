-- migrate:up
-- Scope descriptions: the free-text "what this scope governs + when to write
-- here" that the read-only `scope_guide` tool surfaces as a routing manifest.
--
-- NULLABLE + app-enforced (not NOT NULL): the mandatory-on-create guarantee is
-- enforced at the wire/tool layer (wire_types.SPEC marks scope_create.description
-- required -> app.py's wire_types.validate rejects a missing one; core.scopes
-- rejects an empty one). It is deliberately NOT a storage constraint because the
-- ~40 direct-SQL `INSERT INTO scopes` fixtures across the test suite, plus legacy
-- rows and the admin/bootstrap paths, create scopes WITHOUT going through that
-- tool -- a NOT NULL column would break every one of them for no real safety
-- gain (they are trusted fixtures; the only network-reachable create path,
-- scope_create, already guarantees a non-empty description). Nullable is also
-- forward-compatible: a later migration can tighten to NOT NULL once every
-- insert path supplies a description, without a risky big-bang now.
ALTER TABLE scopes ADD COLUMN description text;

-- Backfill existing scopes (e.g. the demo/drill/devon spaces and their scopes)
-- with a safe, human-meaningful value -- their display name -- so scope_guide
-- returns something useful for scopes that predate descriptions. `id` is the
-- fallback and is always non-empty (CHECK id ~ '^[a-z0-9][a-z0-9-]{1,62}$'), so
-- the COALESCE can never yield NULL/empty even if a display_name is blank.
UPDATE scopes
   SET description = COALESCE(NULLIF(btrim(display_name), ''), id)
 WHERE description IS NULL;

-- migrate:down
ALTER TABLE scopes DROP COLUMN description;
