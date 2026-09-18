-- migrate:up
-- Flexible partial dates (approved 2026-08): a `date` attr may be a full ISO
-- date OR an imprecise partial --
-- year-only (`YYYY`) or year+month (`YYYY-MM`). Conversational sources state
-- dates imprecisely ("in 2022", "June 2023"); the strict full-date-only rule
-- rejected them and, because a rejected attr aborts the whole write, the entire
-- node (title, body, all content) was lost -- a leading cause of write-time
-- node loss on conversational sources.
--
-- Partials are STORED VERBATIM (never coerced to a fake full date). Validity is
-- checked by padding to a full date and casting (rpad(v,10,'-01'):
-- '2022' -> '2022-01-01', '2023-06' -> '2023-06-01'), so an out-of-range month
-- ('2023-13') or impossible day ('2026-02-30') still fails for partials and full
-- dates alike. Zero-padding is required by the regex (`\d{4}(-\d{2}(-\d{2})?)?`)
-- so stored values sort lexicographically == chronologically (briefing's
-- where_attr/order_by compare dates as text -- migration touches nothing there).
--
-- attr_spec.py's Python mirror carries the identical rule; the two are held
-- equal by test_attr_spec_parity.py (whose value pool now generates partial
-- dates). Only the `WHEN 'date'` branch changes; every other phase is 0017's.
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
  -- pack-declared key -- see migration 0017) and exempt from this check.
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
          IF jsonb_typeof(v) <> 'string' OR (v #>> '{}') !~ '^\d{4}(-\d{2}(-\d{2})?)?$' THEN
            errors := errors || format('attrs.%s must be a date', k);
          ELSE
            -- Pad a partial (YYYY / YYYY-MM) to a full date for validation only;
            -- the value is stored verbatim. Out-of-range month/day still fails.
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
-- Restore the strict full-date-only validator (migration 0017's definition).
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
