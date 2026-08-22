-- migrate:up
-- HNSW on nodes.embedding (vector_cosine_ops), GIN on search, composite btrees (design/01 §Indexes)

CREATE INDEX nodes_space_scope_type_status_idx ON nodes (space_id, scope_id, type, status);

-- Instance-wide (not per-space) -- dedup/search queries always filter space first
-- (design/01 §Indexes), so the space filter narrows before the HNSW scan.
CREATE INDEX nodes_embedding_hnsw_idx ON nodes USING hnsw (embedding vector_cosine_ops);

CREATE INDEX nodes_search_gin_idx ON nodes USING gin (search);

CREATE INDEX nodes_canonical_id_idx ON nodes (canonical_id) WHERE canonical_id IS NOT NULL;

CREATE INDEX edges_src_type_idx ON edges (src_id, type);
CREATE INDEX edges_dst_type_idx ON edges (dst_id, type);

CREATE INDEX inbox_pending_idx ON inbox (space_id, status) WHERE status = 'pending';

-- migrate:down
DROP INDEX inbox_pending_idx;
DROP INDEX edges_dst_type_idx;
DROP INDEX edges_src_type_idx;
DROP INDEX nodes_canonical_id_idx;
DROP INDEX nodes_search_gin_idx;
DROP INDEX nodes_embedding_hnsw_idx;
DROP INDEX nodes_space_scope_type_status_idx;
