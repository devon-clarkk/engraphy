-- migrate:up
-- nodes_touch: do NOT bump updated_at on a recall-only update (design/02
-- "recall stats batched on every read"; QUESTIONS.md "recall-stats-vs-nodes-
-- touch", resolved 2026-07-16 Devon option a). updated_at means "last CONTENT
-- edit"; a recall bump (recall_count / last_recalled_at, from the read-path
-- recall stats) is a read, not an edit, so it must leave updated_at where it
-- was. The E0 trigger (migration 0006) set NEW.updated_at := now() on EVERY
-- update, which corrupted updated_at once reads started bumping recall. This
-- is a Devon-authorized edit to the E0 kernel trigger, applied as a new
-- migration (0006 stays as-applied).
--
-- Detection is by content-column equality: if every content column is
-- unchanged (so only recall_count/last_recalled_at and/or updated_at differ),
-- preserve OLD.updated_at; otherwise stamp now(). A no-op update (nothing
-- changed) also preserves, which is strictly more correct than the old
-- unconditional bump. The canonical-id consistency guard is unchanged.
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
    NEW.updated_at := OLD.updated_at;   -- recall-only or no-op: leave it
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
-- Restore the original unconditional bump (migration 0006's function body).
CREATE OR REPLACE FUNCTION nodes_touch_fn() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_canon_space text;
  v_canon_type  text;
BEGIN
  NEW.updated_at := now();
  IF NEW.canonical_id IS NOT NULL THEN
    SELECT space_id, type INTO v_canon_space, v_canon_type
      FROM nodes WHERE id = NEW.canonical_id;
    IF v_canon_space IS DISTINCT FROM NEW.space_id OR v_canon_type IS DISTINCT FROM NEW.type THEN
      RAISE EXCEPTION 'canonical_id target must be same space and type' USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END $$;
