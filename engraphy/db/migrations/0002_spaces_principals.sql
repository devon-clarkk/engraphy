-- migrate:up
-- spaces, principals (design/01 §Table definitions, design/06 §Principals)

CREATE TABLE spaces (
  id           text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  display_name text NOT NULL,
  pack_name    text,                          -- set by pack apply
  pack_version integer,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE principals (
  space_id   text NOT NULL REFERENCES spaces(id),
  id         text NOT NULL CHECK (id ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  display_name text NOT NULL,
  role       text NOT NULL DEFAULT 'member' CHECK (role IN ('member','space_admin')),
  archived   boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (space_id, id)
);

-- migrate:down
DROP TABLE principals;
DROP TABLE spaces;
