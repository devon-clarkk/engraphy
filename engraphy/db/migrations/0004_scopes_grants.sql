-- migrate:up
-- scopes (owner_principal, visibility, ambient, hints), scope_grants (design/01, design/06)

CREATE TABLE scopes (
  space_id   text NOT NULL REFERENCES spaces(id),
  id         text NOT NULL CHECK (id ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  display_name text NOT NULL,
  owner_principal text,                       -- NULL for ownerless team-write scopes (06)
  visibility text NOT NULL DEFAULT 'private'
             CHECK (visibility IN ('private','team-read','team-write')),
  ambient    boolean NOT NULL DEFAULT false,
  hints      text[] NOT NULL DEFAULT '{}',    -- client-side context matching (repo URLs etc.)
  archived   boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (space_id, id),
  FOREIGN KEY (space_id, owner_principal) REFERENCES principals (space_id, id)
);

CREATE TABLE scope_grants (                                -- per-principal exceptions
  space_id  text NOT NULL,
  scope_id  text NOT NULL,
  principal text NOT NULL,
  level     text NOT NULL CHECK (level IN ('read','write')),
  PRIMARY KEY (space_id, scope_id, principal),
  FOREIGN KEY (space_id, scope_id)  REFERENCES scopes (space_id, id),
  FOREIGN KEY (space_id, principal) REFERENCES principals (space_id, id)
);

-- migrate:down
DROP TABLE scope_grants;
DROP TABLE scopes;
