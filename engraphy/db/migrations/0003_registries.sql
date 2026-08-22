-- migrate:up
-- node_types, edge_types, edge_rules (design/01 §Table definitions)

CREATE TABLE node_types (
  space_id  text NOT NULL REFERENCES spaces(id),
  name      text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{1,40}$'),
  description text NOT NULL,
  attr_spec jsonb NOT NULL,                   -- the attr-spec language
  PRIMARY KEY (space_id, name)
);

CREATE TABLE edge_types (
  space_id      text NOT NULL REFERENCES spaces(id),
  name          text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{1,40}$'),
  description   text NOT NULL,
  bidirectional boolean NOT NULL DEFAULT false,   -- traversal hint (e.g. relates_to)
  PRIMARY KEY (space_id, name)
);

CREATE TABLE edge_rules (
  space_id  text NOT NULL,
  type      text NOT NULL,
  src_type  text NOT NULL,
  dst_type  text NOT NULL,
  PRIMARY KEY (space_id, type, src_type, dst_type),
  FOREIGN KEY (space_id, type)     REFERENCES edge_types (space_id, name) ON DELETE CASCADE,
  FOREIGN KEY (space_id, src_type) REFERENCES node_types (space_id, name),
  FOREIGN KEY (space_id, dst_type) REFERENCES node_types (space_id, name)
);
-- '*' wildcards in pack files are EXPANDED to concrete rows at apply time:
-- the runtime check is a plain lookup, never wildcard logic.

-- migrate:down
DROP TABLE edge_rules;
DROP TABLE edge_types;
DROP TABLE node_types;
