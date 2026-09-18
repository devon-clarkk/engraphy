-- Search latency against the seeded store, so a memory saving is never quoted
-- without the speed it cost. Same two legs the read path issues.
\set ON_ERROR_STOP on
\timing off

DO $$
DECLARE
  probe vector(384);
  n int;
  t0 timestamptz;
  vec_ms double precision;
  lex_ms double precision;
BEGIN
  -- Warm first, so the figure is steady-state and not one cold index read.
  FOR i IN 1..50 LOOP
    SELECT ('[' || string_agg((random() - 0.5)::text, ',') || ']')::vector
      INTO probe FROM generate_series(1, 384);
    SELECT count(*) INTO n FROM (
      SELECT id FROM nodes WHERE space_id = 'fp-measure' AND status = 'active'
       ORDER BY embedding <=> probe LIMIT 30) t;
  END LOOP;

  t0 := clock_timestamp();
  FOR i IN 1..200 LOOP
    SELECT ('[' || string_agg((random() - 0.5)::text, ',') || ']')::vector
      INTO probe FROM generate_series(1, 384);
    SELECT count(*) INTO n FROM (
      SELECT id FROM nodes WHERE space_id = 'fp-measure' AND status = 'active'
       ORDER BY embedding <=> probe LIMIT 30) t;
  END LOOP;
  vec_ms := extract(epoch FROM clock_timestamp() - t0) * 1000 / 200;

  t0 := clock_timestamp();
  FOR i IN 1..200 LOOP
    SELECT count(*) INTO n FROM (
      SELECT id FROM nodes WHERE space_id = 'fp-measure'
         AND search @@ websearch_to_tsquery('english', 'seeded node body text')
       ORDER BY ts_rank_cd(search, websearch_to_tsquery('english', 'seeded node body text')) DESC
       LIMIT 30) t;
  END LOOP;
  lex_ms := extract(epoch FROM clock_timestamp() - t0) * 1000 / 200;

  RAISE NOTICE 'vector leg  % ms/query (includes generating the probe vector)', round(vec_ms::numeric, 2);
  RAISE NOTICE 'lexical leg % ms/query', round(lex_ms::numeric, 2);
END $$;

SELECT name, setting, unit FROM pg_settings
 WHERE name IN ('shared_buffers', 'max_connections', 'maintenance_work_mem',
                'effective_cache_size', 'autovacuum_max_workers')
 ORDER BY name;
