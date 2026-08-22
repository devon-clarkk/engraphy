-- migrate:up
-- engram_validate_attrs() per design/implementation/attr-spec-interpreter-plan.md (skeleton there),
-- nodes_validate_attrs / edges_validate / nodes_touch triggers (design/01 §Trigger enforcement)
--
-- Storage note (DECISIONS-DELTA.md): node_types.attr_spec stores exactly the
-- {"attrs": {"required": ..., "optional": ..., "closed": ..., "requires": ...}}
-- shape engram_validate_attrs()'s `spec` argument expects (spec #> '{attrs,required}'
-- etc.) -- i.e. a pack node type's `attrs:` value, wrapped one level under the
-- literal key "attrs". This is the same shape every fixture in
-- attr_spec_cases.yaml uses, so pack apply writes it directly with no
-- translation step, and the trigger calls engram_validate_attrs(attr_spec, NEW.attrs)
-- with no rewrapping either.

CREATE OR REPLACE FUNCTION engram_validate_attrs(spec jsonb, attrs jsonb)
RETURNS text[] LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  errors text[] := '{}';  req jsonb;  opt jsonb;  cond jsonb;  closed boolean;
  k text;  v jsonb;  rule jsonb;  c jsonb;  d date;
BEGIN
  req    := COALESCE(spec #> '{attrs,required}', '{}'::jsonb);
  opt    := COALESCE(spec #> '{attrs,optional}', '{}'::jsonb);
  cond   := COALESCE(spec #> '{attrs,requires}', '[]'::jsonb);
  closed := COALESCE((spec #>> '{attrs,closed}')::boolean, true);

  -- Phase 1: required presence
  FOR k IN SELECT jsonb_object_keys(req) ORDER BY 1 LOOP
    IF NOT attrs ? k THEN errors := errors || format('attrs.%s is required', k); END IF;
  END LOOP;

  -- Phase 2: conditionals, array order
  FOR c IN SELECT * FROM jsonb_array_elements(cond) LOOP
    IF attrs ? (c #>> '{when,key}')
       AND jsonb_typeof(attrs -> (c #>> '{when,key}')) = 'string'
       AND attrs ->> (c #>> '{when,key}') = c #>> '{when,equals}'
       AND NOT attrs ? (c ->> 'key') THEN
      errors := errors || format('attrs.%s is required when %s=%s',
                 c ->> 'key', c #>> '{when,key}', c #>> '{when,equals}');
    END IF;
  END LOOP;

  -- Phase 3: closed / unknown keys
  IF closed THEN
    FOR k IN SELECT jsonb_object_keys(attrs) ORDER BY 1 LOOP
      IF NOT (req ? k OR opt ? k) THEN
        errors := errors || format('attrs.%s is not allowed (closed spec)', k);
      END IF;
    END LOOP;
  END IF;

  -- Phase 4: value checks (lexicographic; rule = req->k else opt->k)
  FOR k IN SELECT jsonb_object_keys(attrs) ORDER BY 1 LOOP
    rule := COALESCE(req -> k, opt -> k);
    CONTINUE WHEN rule IS NULL;
    v := attrs -> k;
    IF rule ? 'enum' THEN
      IF jsonb_typeof(v) <> 'string' OR NOT (rule -> 'enum') ? (v #>> '{}') THEN
        errors := errors || format('attrs.%s must be one of %s', k,
          (SELECT string_agg(e #>> '{}', '|') FROM jsonb_array_elements(rule -> 'enum') e));
      END IF;
    ELSE
      CASE rule ->> 'type'
        WHEN 'string' THEN
          IF jsonb_typeof(v) <> 'string' THEN
            errors := errors || format('attrs.%s must be a string', k);
          ELSIF char_length(v #>> '{}') > 2000 THEN
            errors := errors || format('attrs.%s must be at most 2000 characters', k);
          END IF;
        WHEN 'int' THEN
          IF jsonb_typeof(v) <> 'number' THEN
            errors := errors || format('attrs.%s must be a int', k);
          ELSIF (v #>> '{}')::numeric <> trunc((v #>> '{}')::numeric) THEN
            errors := errors || format('attrs.%s must be a int', k);
          END IF;
        WHEN 'number' THEN
          IF jsonb_typeof(v) <> 'number' THEN
            errors := errors || format('attrs.%s must be a number', k);
          END IF;
        WHEN 'bool' THEN
          IF jsonb_typeof(v) <> 'boolean' THEN
            errors := errors || format('attrs.%s must be a bool', k);
          END IF;
        WHEN 'date' THEN
          IF jsonb_typeof(v) <> 'string' OR (v #>> '{}') !~ '^\d{4}-\d{2}-\d{2}$' THEN
            errors := errors || format('attrs.%s must be a date', k);
          ELSE
            BEGIN
              d := (v #>> '{}')::date;
            EXCEPTION WHEN others THEN
              errors := errors || format('attrs.%s must be a valid ISO date', k);
            END;
          END IF;
      END CASE;
    END IF;
  END LOOP;
  RETURN errors;
END $$;

-- nodes_validate_attrs: loads the row's (space_id, type) attr-spec from
-- node_types and interprets it; raises ERRCODE 23514 with the joined message
-- when non-empty. The FK on (space_id, type) guarantees a node_types row
-- exists by the time this trigger runs (BEFORE, but FK is checked as part of
-- the same statement's constraint enforcement and a bad type never reaches here
-- through the app -- still, a missing spec here is a genuine data error and
-- must not crash silently, so it also raises).
CREATE OR REPLACE FUNCTION nodes_validate_attrs_fn() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  spec jsonb;
  errors text[];
BEGIN
  SELECT attr_spec INTO spec FROM node_types WHERE space_id = NEW.space_id AND name = NEW.type;
  IF spec IS NULL THEN
    RAISE EXCEPTION 'unknown node type % in space %', NEW.type, NEW.space_id USING ERRCODE = '23514';
  END IF;

  errors := engram_validate_attrs(spec, NEW.attrs);
  IF array_length(errors, 1) > 0 THEN
    RAISE EXCEPTION '%', array_to_string(errors, '; ') USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER nodes_validate_attrs BEFORE INSERT OR UPDATE ON nodes
  FOR EACH ROW EXECUTE FUNCTION nodes_validate_attrs_fn();

-- nodes_touch: updated_at, plus consistency guard: canonical_id target must
-- be same space + same type (design/01 §Trigger enforcement).
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

CREATE TRIGGER nodes_touch BEFORE INSERT OR UPDATE ON nodes
  FOR EACH ROW EXECUTE FUNCTION nodes_touch_fn();

-- edges_validate: both endpoints exist in space_id (cross-space edge = an
-- exception), rule row exists in edge_rules for (type, src_type, dst_type).
-- The visibility-side creation rule (read-both / write-one) is enforced in
-- the write path against engram_writable_scopes() -- this trigger enforces
-- only the structural half (design/01 §Trigger enforcement).
CREATE OR REPLACE FUNCTION edges_validate_fn() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_src_space text; v_src_type text;
  v_dst_space text; v_dst_type text;
BEGIN
  SELECT space_id, type INTO v_src_space, v_src_type FROM nodes WHERE id = NEW.src_id;
  SELECT space_id, type INTO v_dst_space, v_dst_type FROM nodes WHERE id = NEW.dst_id;

  IF v_src_space IS NULL OR v_dst_space IS NULL THEN
    RAISE EXCEPTION 'edge endpoint does not exist' USING ERRCODE = '23514';
  END IF;
  IF v_src_space <> NEW.space_id OR v_dst_space <> NEW.space_id THEN
    RAISE EXCEPTION 'cross-space edge is not permitted' USING ERRCODE = '23514';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM edge_rules er
    WHERE er.space_id = NEW.space_id AND er.type = NEW.type
      AND er.src_type = v_src_type AND er.dst_type = v_dst_type
  ) THEN
    RAISE EXCEPTION 'edge_rules: no rule for type=%, src=%, dst=%',
      NEW.type, v_src_type, v_dst_type USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER edges_validate BEFORE INSERT OR UPDATE ON edges
  FOR EACH ROW EXECUTE FUNCTION edges_validate_fn();

-- migrate:down
DROP TRIGGER IF EXISTS edges_validate ON edges;
DROP FUNCTION IF EXISTS edges_validate_fn();
DROP TRIGGER IF EXISTS nodes_touch ON nodes;
DROP FUNCTION IF EXISTS nodes_touch_fn();
DROP TRIGGER IF EXISTS nodes_validate_attrs ON nodes;
DROP FUNCTION IF EXISTS nodes_validate_attrs_fn();
DROP FUNCTION IF EXISTS engram_validate_attrs(jsonb, jsonb);
