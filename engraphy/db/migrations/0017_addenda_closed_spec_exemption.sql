-- migrate:up
-- Fix (DECISIONS-DELTA.md, 2026-07-19 "E0/E1 kernel bug found during E3 work"):
-- engram_validate_attrs()'s Phase 3 closed-spec check has no exemption for the
-- `attrs.addenda` reserved key, so dedup.py's merge-addendum write
-- (`UPDATE nodes SET attrs = jsonb_set(attrs, '{addenda}', ...)`) re-fires
-- nodes_validate_attrs_fn() and raises `CheckViolation: attrs.addenda is not
-- allowed (closed spec)` on any node type declared `closed: true` -- which is
-- every node type in both shipped packs. `addenda` is reserved at the app
-- layer (engram.core.attr_spec.RESERVED_ATTR_KEYS, consumed by
-- dedup.py::_validate_no_reserved_attrs and update.py -- callers can never
-- supply it themselves), never something a pack author declares in
-- `required`/`optional`, so it must be exempt from the closed-spec
-- unknown-key check the same way it already is from Phase 4's value checks
-- (COALESCE(req -> k, opt -> k) is NULL for a key neither section names, so
-- Phase 4 already skips it via `CONTINUE WHEN rule IS NULL` -- only Phase 3
-- was missing the equivalent exemption). attr_spec.py's Python mirror carries
-- the identical exemption; the two are held equal by test_attr_spec_parity.py's
-- fuzzer, which requires `addenda` in its generated-key pool to actually
-- exercise this path (see that file's KEYS comment).
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

  -- Phase 3: closed / unknown keys. `addenda` is engine-reserved (never a
  -- pack-declared key -- see migration header) and exempt from this check.
  IF closed THEN
    FOR k IN SELECT jsonb_object_keys(attrs) ORDER BY 1 LOOP
      IF k <> 'addenda' AND NOT (req ? k OR opt ? k) THEN
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

-- migrate:down
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
