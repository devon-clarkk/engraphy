-- migrate:up
-- nodes (incl. author_principal, embedding vector(384), search tsvector), edges (design/01)

CREATE TABLE nodes (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  space_id       text NOT NULL,
  type           text NOT NULL,
  scope_id       text NOT NULL,
  title          text NOT NULL CHECK (char_length(title) BETWEEN 3 AND 200),
  body           text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 8000),
  attrs          jsonb NOT NULL DEFAULT '{}',
  status         text NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','superseded','merged','archived')),
  canonical_id   uuid REFERENCES nodes(id),
  embedding      vector(384) NOT NULL,
  embedding_model text NOT NULL,
  search         tsvector GENERATED ALWAYS AS (
                   setweight(to_tsvector('english', title), 'A') ||
                   setweight(to_tsvector('english', body),  'B')) STORED,
  source_client  text NOT NULL,               -- token's client name (server-set)
  author_principal text NOT NULL,             -- token's principal (server-set; 06)
  source_session text,
  recall_count   integer NOT NULL DEFAULT 0,
  last_recalled_at timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (space_id, type)     REFERENCES node_types (space_id, name),
  FOREIGN KEY (space_id, scope_id) REFERENCES scopes (space_id, id),
  CHECK ((status = 'merged') = (canonical_id IS NOT NULL))
);

CREATE TABLE edges (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  space_id   text NOT NULL REFERENCES spaces(id),
  src_id     uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  dst_id     uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  type       text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (src_id, dst_id, type),
  CHECK (src_id <> dst_id),
  FOREIGN KEY (space_id, type) REFERENCES edge_types (space_id, name)
);

-- migrate:down
DROP TABLE edges;
DROP TABLE nodes;
