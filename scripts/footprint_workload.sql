-- Give Postgres enough work to fault in what a coworker's instance would.
--
-- An idle Postgres is a misleading number. `shared_buffers` is reserved but
-- untouched at boot, the HNSW index is on disk and not in memory, and the
-- per-backend allocations that `work_mem` governs have never happened. All
-- three appear the moment somebody actually searches, so the footprint worth
-- quoting is the one after a workload, not the one after `up -d`.
--
-- Seeds a realistic personal-memory store and then drives the vector index
-- hard enough to page it in. Deliberately NOT the full engine write path:
-- this is measuring Postgres, and going through the MCP server would fold the
-- server's own allocations into the same number.

\set ON_ERROR_STOP on
\timing off

-- A space to hang the rows off. Reuses the engine's own schema, so the row
-- shape and every index are exactly what ships.
INSERT INTO spaces (id, display_name) VALUES ('fp-measure', 'Footprint')
  ON CONFLICT (id) DO NOTHING;
INSERT INTO principals (space_id, id, display_name) VALUES ('fp-measure', 'p1', 'P')
  ON CONFLICT DO NOTHING;
INSERT INTO node_types (space_id, name, description, attr_spec)
  VALUES ('fp-measure', 'note', 'n', '{"attrs": {"closed": false}}'::jsonb)
  ON CONFLICT DO NOTHING;
INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility)
  VALUES ('fp-measure', 'scope1', 'S1', 'p1', 'private')
  ON CONFLICT DO NOTHING;

-- 20,000 nodes. Chosen as a deliberate overestimate of a personal memory
-- store: the design's own performance budgets are set at 10k, so this is
-- twice the number anyone is expected to hold, which keeps the reported
-- Postgres figure a ceiling rather than a best case.
--
-- Random unit vectors rather than real embeddings. The index does not care
-- where a vector came from, and generating 20k real ones would measure the
-- embedder, which is a different line of the report.
INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding,
                   embedding_model, source_client, author_principal)
SELECT 'fp-measure', 'note', 'scope1',
       'Seeded node ' || g,
       'Body text for seeded node ' || g || ', long enough to carry a tsvector worth indexing.',
       '{}'::jsonb,
       (SELECT ('[' || string_agg((random() - 0.5)::text, ',') || ']')::vector
          FROM generate_series(1, 384)),
       'footprint-probe', 'probe', 'p1'
FROM generate_series(1, 20000) g;

ANALYZE nodes;

-- 500 index-touching searches. Each one is the same shape the read path
-- issues: nearest neighbours by cosine over the HNSW index, scoped and
-- status-filtered.
DO $$
DECLARE
  probe vector(384);
  n int;
BEGIN
  FOR i IN 1..500 LOOP
    SELECT ('[' || string_agg((random() - 0.5)::text, ',') || ']')::vector
      INTO probe FROM generate_series(1, 384);
    SELECT count(*) INTO n FROM (
      SELECT id FROM nodes
       WHERE space_id = 'fp-measure' AND status = 'active'
       ORDER BY embedding <=> probe
       LIMIT 30) t;
  END LOOP;
END $$;

-- And the lexical leg, which is the other index a search touches.
DO $$
DECLARE n int;
BEGIN
  FOR i IN 1..200 LOOP
    SELECT count(*) INTO n FROM (
      SELECT id FROM nodes
       WHERE space_id = 'fp-measure'
         AND search @@ websearch_to_tsquery('english', 'seeded node body text')
       ORDER BY ts_rank_cd(search, websearch_to_tsquery('english', 'seeded node body text')) DESC
       LIMIT 30) t;
  END LOOP;
END $$;

SELECT count(*) AS seeded_nodes FROM nodes WHERE space_id = 'fp-measure';
SELECT name, setting, unit FROM pg_settings
 WHERE name IN ('shared_buffers', 'work_mem', 'maintenance_work_mem',
                'max_connections', 'effective_cache_size', 'max_worker_processes')
 ORDER BY name;
