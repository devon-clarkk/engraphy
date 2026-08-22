-- migrate:up
-- Phase C (fact-searchability-phase-c.md §4): a `surface rebuild` (or a model-swap
-- re-embed, design/04) recomputes `embedding`/`embedding_model`/`extra_search`
-- while the SEMANTIC content (title/body/attrs/status/...) is unchanged. That is a
-- re-INDEX, not a content edit, so it must leave `updated_at` where it was --
-- exactly the recall-skip rationale of migration 0012, now extended to the derived
-- columns. The embedding is a deterministic function of the searchable text; if
-- the content columns are all equal, an embedding/extra_search change carries no
-- new content and must not bump `updated_at`. A real content edit (title/body/
-- attrs change) still bumps, because those columns differ. `extra_search` is
-- likewise derived from `attrs` and is deliberately NOT a content column here.
CREATE OR REPLACE FUNCTION nodes_touch_fn() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_canon_space text;
  v_canon_type  text;
BEGIN
  IF TG_OP = 'UPDATE'
     AND NEW.type              =            OLD.type
     AND NEW.scope_id          =            OLD.scope_id
     AND NEW.title             =            OLD.title
     AND NEW.body              =            OLD.body
     AND NEW.attrs             =            OLD.attrs
     AND NEW.status            =            OLD.status
     AND NEW.canonical_id      IS NOT DISTINCT FROM OLD.canonical_id
     AND NEW.source_client     =            OLD.source_client
     AND NEW.author_principal  =            OLD.author_principal
     AND NEW.source_session    IS NOT DISTINCT FROM OLD.source_session
  THEN
    -- recall-only, re-index (embedding/extra_search), or no-op: leave it.
    NEW.updated_at := OLD.updated_at;
  ELSE
    NEW.updated_at := now();            -- INSERT or a real content edit
  END IF;

  IF NEW.canonical_id IS NOT NULL THEN
    SELECT space_id, type INTO v_canon_space, v_canon_type
      FROM nodes WHERE id = NEW.canonical_id;
    IF v_canon_space IS DISTINCT FROM NEW.space_id OR v_canon_type IS DISTINCT FROM NEW.type THEN
      RAISE EXCEPTION 'canonical_id target must be same space and type' USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END $$;

-- migrate:down
-- Restore migration 0012's check (embedding/embedding_model included).
CREATE OR REPLACE FUNCTION nodes_touch_fn() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_canon_space text;
  v_canon_type  text;
BEGIN
  IF TG_OP = 'UPDATE'
     AND NEW.type              =            OLD.type
     AND NEW.scope_id          =            OLD.scope_id
     AND NEW.title             =            OLD.title
     AND NEW.body              =            OLD.body
     AND NEW.attrs             =            OLD.attrs
     AND NEW.status            =            OLD.status
     AND NEW.canonical_id      IS NOT DISTINCT FROM OLD.canonical_id
     AND NEW.embedding         =            OLD.embedding
     AND NEW.embedding_model   =            OLD.embedding_model
     AND NEW.source_client     =            OLD.source_client
     AND NEW.author_principal  =            OLD.author_principal
     AND NEW.source_session    IS NOT DISTINCT FROM OLD.source_session
  THEN
    NEW.updated_at := OLD.updated_at;
  ELSE
    NEW.updated_at := now();
  END IF;

  IF NEW.canonical_id IS NOT NULL THEN
    SELECT space_id, type INTO v_canon_space, v_canon_type
      FROM nodes WHERE id = NEW.canonical_id;
    IF v_canon_space IS DISTINCT FROM NEW.space_id OR v_canon_type IS DISTINCT FROM NEW.type THEN
      RAISE EXCEPTION 'canonical_id target must be same space and type' USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END $$;
