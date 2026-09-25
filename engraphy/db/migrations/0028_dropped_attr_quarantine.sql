-- migrate:up
-- Never destroy a node on a typed-attr validation failure
-- (approved 2026-08). The write path
-- now MOVES an attr that fails validation into a reserved `dropped` quarantine
-- bucket (engraphy.core.attr_spec.sanitize_attrs) instead of letting the DB
-- trigger reject the whole write. Two validator changes make the quarantined
-- node storable:
--   * Phase 1 (required presence): a required key is ALSO satisfied by its
--     presence in an object `dropped` bucket -- so quarantining an invalid
--     REQUIRED attr (e.g. event.occurred_on = "last month") no longer trips the
--     required-presence check. Only an object `dropped` counts (matches the
--     bucket shape and attr_spec.py's isinstance(dict) guard).
--   * Phase 3 (closed spec): `dropped` joins `addenda` as an engine-reserved key
--     exempt from the unknown-key check (it is never pack-declared).
-- Builds on migration 0027's flexible partial-date rule (unchanged here). The
-- Python mirror (attr_spec.py) carries identical logic; test_attr_spec_parity.py
-- (its key pool now includes `dropped`) holds the two equal.
CREATE OR REPLACE FUNCTION engraphy_validate_attrs(spec jsonb, attrs jsonb)
RETURNS text[] LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  errors text[] := '{}';  req jsonb;  opt jsonb;  cond jsonb;  closed boolean;
  k text;  v jsonb;  rule jsonb;  c jsonb;  d date;
BEGIN
  req    := COALESCE(spec #> '{attrs,required}', '{}'::jsonb);
  opt    := COALESCE(spec #> '{attrs,optional}', '{}'::jsonb);
  cond   := COALESCE(spec #> '{attrs,requires}', '[]'::jsonb);
  closed := COALESCE((spec #>> '{attrs,closed}')::boolean, true);

  -- Phase 1: required presence (satisfied by a top-level key OR a quarantined
  -- key in an object `dropped` bucket).
  FOR k IN SELECT jsonb_object_keys(req) ORDER BY 1 LOOP
    IF NOT attrs ? k
       AND NOT COALESCE(jsonb_typeof(attrs -> 'dropped') = 'object'
                        AND (attrs -> 'dropped') ? k, false) THEN
      errors := errors || format('attrs.%s is required', k);
    END IF;
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

  -- Phase 3: closed / unknown keys. `addenda` and `dropped` are engine-reserved
  -- (never pack-declared) and exempt from this check.
  IF closed THEN
    FOR k IN SELECT jsonb_object_keys(attrs) ORDER BY 1 LOOP
      IF k <> 'addenda' AND k <> 'dropped' AND NOT (req ? k OR opt ? k) THEN
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
          IF jsonb_typeof(v) <> 'string' OR (v #>> '{}') !~ '^\d{4}(-\d{2}(-\d{2})?)?$' THEN
            errors := errors || format('attrs.%s must be a date', k);
          ELSE
            BEGIN
              d := rpad(v #>> '{}', 10, '-01')::date;
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
-- Restore migration 0027's validator (flexible dates, addenda-only exemption,
-- strict required-presence without the `dropped` quarantine satisfaction).
CREATE OR REPLACE FUNCTION engraphy_validate_attrs(spec jsonb, attrs jsonb)
RETURNS text[] LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  errors text[] := '{}';  req jsonb;  opt jsonb;  cond jsonb;  closed boolean;
  k text;  v jsonb;  rule jsonb;  c jsonb;  d date;
BEGIN
  req    := COALESCE(spec #> '{attrs,required}', '{}'::jsonb);
  opt    := COALESCE(spec #> '{attrs,optional}', '{}'::jsonb);
  cond   := COALESCE(spec #> '{attrs,requires}', '[]'::jsonb);
  closed := COALESCE((spec #>> '{attrs,closed}')::boolean, true);

  FOR k IN SELECT jsonb_object_keys(req) ORDER BY 1 LOOP
    IF NOT attrs ? k THEN errors := errors || format('attrs.%s is required', k); END IF;
  END LOOP;

  FOR c IN SELECT * FROM jsonb_array_elements(cond) LOOP
    IF attrs ? (c #>> '{when,key}')
       AND jsonb_typeof(attrs -> (c #>> '{when,key}')) = 'string'
       AND attrs ->> (c #>> '{when,key}') = c #>> '{when,equals}'
       AND NOT attrs ? (c ->> 'key') THEN
      errors := errors || format('attrs.%s is required when %s=%s',
                 c ->> 'key', c #>> '{when,key}', c #>> '{when,equals}');
    END IF;
  END LOOP;

  IF closed THEN
    FOR k IN SELECT jsonb_object_keys(attrs) ORDER BY 1 LOOP
      IF k <> 'addenda' AND NOT (req ? k OR opt ? k) THEN
        errors := errors || format('attrs.%s is not allowed (closed spec)', k);
      END IF;
    END LOOP;
  END IF;

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
          IF jsonb_typeof(v) <> 'string' OR (v #>> '{}') !~ '^\d{4}(-\d{2}(-\d{2})?)?$' THEN
            errors := errors || format('attrs.%s must be a date', k);
          ELSE
            BEGIN
              d := rpad(v #>> '{}', 10, '-01')::date;
            EXCEPTION WHEN others THEN
              errors := errors || format('attrs.%s must be a valid ISO date', k);
            END;
          END IF;
      END CASE;
    END IF;
  END LOOP;
  RETURN errors;
END $$;
