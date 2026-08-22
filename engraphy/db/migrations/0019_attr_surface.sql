-- migrate:up
-- Phase C (fact-searchability-model.md I2; implementation fact-searchability-phase-c.md
-- §2.2): content-bearing attrs enter the searchable surface. The write path renders
-- the searchable attrs (string/date by construct default, or per the `searchable`
-- flag) into a stored `extra_search` column -- one source that BOTH the embedding
-- (via searchable_text) and the lexical leg (this tsvector) read, so they cannot
-- drift. Addenda bodies also enter the lexical leg (weight D) here for the first
-- time (parent §3.2). Nothing is recomputed by this DDL: `extra_search` defaults
-- to '' so every existing row stays byte-identical to Phase B until
-- `engram-admin surface rebuild` recomputes and re-embeds it.

ALTER TABLE nodes ADD COLUMN extra_search text NOT NULL DEFAULT '';

-- Addenda-body concatenation for the tsvector's weight-D leg. IMMUTABLE + SQL so a
-- generated column may call it (jsonb operators and string_agg over a set are
-- immutable; the CASE guards a non-array `addenda`, which jsonb_array_elements
-- would otherwise reject).
CREATE OR REPLACE FUNCTION engram_addenda_text(attrs jsonb)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT COALESCE(string_agg(elem ->> 'body', ' '), '')
  FROM jsonb_array_elements(
    CASE WHEN jsonb_typeof(attrs -> 'addenda') = 'array'
         THEN attrs -> 'addenda' ELSE '[]'::jsonb END) AS elem
$$;

-- Regenerate the search tsvector: title A, body B, extra_search C, addenda D. The
-- GIN index depends on the column, so drop it first and recreate it after.
DROP INDEX nodes_search_gin_idx;
ALTER TABLE nodes DROP COLUMN search;
ALTER TABLE nodes ADD COLUMN search tsvector GENERATED ALWAYS AS (
  setweight(to_tsvector('english', title), 'A') ||
  setweight(to_tsvector('english', body),  'B') ||
  setweight(to_tsvector('english', extra_search), 'C') ||
  setweight(to_tsvector('english', engram_addenda_text(attrs)), 'D')) STORED;
CREATE INDEX nodes_search_gin_idx ON nodes USING gin (search);

-- migrate:down
DROP INDEX nodes_search_gin_idx;
ALTER TABLE nodes DROP COLUMN search;
ALTER TABLE nodes ADD COLUMN search tsvector GENERATED ALWAYS AS (
  setweight(to_tsvector('english', title), 'A') ||
  setweight(to_tsvector('english', body),  'B')) STORED;
CREATE INDEX nodes_search_gin_idx ON nodes USING gin (search);
ALTER TABLE nodes DROP COLUMN extra_search;
DROP FUNCTION engram_addenda_text(jsonb);
