-- Proves a freshly built vector.dll actually works in the Postgres it ships
-- beside, rather than merely having compiled. Run by
-- .github/workflows/pgvector-windows.yml against a throwaway cluster created
-- from the same PostgreSQL binaries the Windows installer bundles.
--
-- It exercises what Engraphy exercises and nothing else: 384 dimensions, which
-- is the embedding width every shipped profile produces, a cosine HNSW index,
-- which is what migration 0009 creates, and an ordered nearest-neighbour scan
-- through that index, which is the vector leg of hybrid search.

\set ON_ERROR_STOP on

CREATE EXTENSION vector;

SELECT extversion AS pgvector_version FROM pg_extension WHERE extname = 'vector';

CREATE TABLE probe (id int PRIMARY KEY, embedding vector(384));

INSERT INTO probe
SELECT g, (SELECT array_agg(random())::vector(384) FROM generate_series(1, 384))
FROM generate_series(1, 500) g;

CREATE INDEX probe_embedding_hnsw_idx ON probe USING hnsw (embedding vector_cosine_ops);

-- enable_seqscan off so the query has to go through the HNSW index. Without it
-- a broken index would still return rows by sequential scan and the probe would
-- pass while the thing being tested did nothing.
SET enable_seqscan = off;

SELECT count(*) AS neighbours FROM (
  SELECT id FROM probe
  ORDER BY embedding <=> (SELECT embedding FROM probe WHERE id = 1)
  LIMIT 10
) t;

-- The index has to be the plan, not a hope. A plan without an Index Scan here
-- means HNSW was not used and the probe proved nothing about it.
DO $$
DECLARE plan text;
BEGIN
  SELECT string_agg(l, E'\n') INTO plan FROM (
    EXPLAIN SELECT id FROM probe
    ORDER BY embedding <=> (SELECT embedding FROM probe WHERE id = 1) LIMIT 10
  ) AS e(l);
  IF plan NOT LIKE '%probe_embedding_hnsw_idx%' THEN
    RAISE EXCEPTION 'HNSW index was not used. Plan was: %', plan;
  END IF;
END $$;
