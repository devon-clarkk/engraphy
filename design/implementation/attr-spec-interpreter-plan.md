# Implementation Plan — Attr-Spec Interpreter

The enforcement kernel: the component that makes "schema enforcement at the data layer, for type systems installed as data" true. It exists **twice by design** — once in plpgsql (the authority, inside the `nodes_validate_attrs` trigger) and once in Python (`engraphy/core/attr_spec.py`, for friendly pydantic errors and pack conformance scans) — and the two are held identical by running the **same fixture file** against both, plus a parity fuzzer.

**Normative inputs:** [07 §Exact formulas / §Pack file schema](../07-implementation-contracts.md), [01 §The attr-spec language](../01-core-data-model.md#the-attr-spec-language)
**Fixture file:** `engraphy/tests/fixtures/attr_spec_cases.yaml` (starter cases committed; expand to ≥ 40)

---

## Contract

One logical function, two implementations:

```
validate_attrs(spec: jsonb, attrs: jsonb) -> text[]   -- ordered error strings; empty = valid
```

- plpgsql: `engraphy_validate_attrs(spec jsonb, attrs jsonb) RETURNS text[]` — pure, IMMUTABLE, no table access. The trigger wrapper loads the spec from `node_types` and raises `ERRCODE '23514'` with `array_to_string(errors, '; ')` when non-empty.
- Python: `validate_attrs(spec: dict, attrs: dict) -> list[str]` — byte-identical error strings.

**Error strings (exact formats — these are contract, fixtures assert them verbatim):**

| Condition | String |
|-----------|--------|
| Required key absent | `attrs.<key> is required` |
| Conditional key absent while triggered | `attrs.<key> is required when <when_key>=<equals>` |
| Unknown key under closed spec | `attrs.<key> is not allowed (closed spec)` |
| Enum miss | `attrs.<key> must be one of <a\|b\|c>` (list in spec order) |
| Type miss | `attrs.<key> must be a <string\|int\|number\|bool\|date>` |
| Invalid date value | `attrs.<key> must be a valid ISO date` |
| String too long | `attrs.<key> must be at most 2000 characters` |

**Error ordering (deterministic, so fixtures can assert the full array):** phase order first — (1) required-presence, (2) conditional-presence, (3) unknown-keys, (4) per-key value checks — and within each phase, keys in **lexicographic order**. One error per key per phase (first failure wins for that key).

## Algorithm (both implementations follow this exactly)

```
errors = []
req  = spec.attrs.required  or {}          # spec normalized at pack-apply time;
opt  = spec.attrs.optional  or {}          # interpreter still tolerates missing sections
cond = spec.attrs.requires  or []
closed = spec.attrs.closed  if present else TRUE
known = keys(req) ∪ keys(opt)

# Phase 1 — required presence (lexicographic over req keys)
for key in sorted(req): if key not in attrs: errors += required-error

# Phase 2 — conditional presence (in ARRAY ORDER as written in the pack —
# conditionals are few; array order is the author's order and is deterministic)
for c in cond:
    if attrs.get(c.when.key) is JSON-string equal to c.when.equals
       and c.key not in attrs: errors += conditional-error

# Phase 3 — unknown keys (lexicographic over attrs keys)
if closed:
    for key in sorted(attrs): if key not in known: errors += not-allowed-error

# Phase 4 — per-key value checks (lexicographic over attrs keys that are in `known`)
for key in sorted(attrs ∩ known):
    check attrs[key] against (req|opt)[key]   # first failure only
```

**Per-key checks, precisely:**

| Spec | Passes iff |
|------|-----------|
| `{enum: [...]}` | JSON type is string AND value ∈ list (case-sensitive) |
| `{type: string}` | JSON string AND `char_length ≤ 2000` (length failure uses the too-long error) |
| `{type: int}` | JSON number AND `value = trunc(value)` (so `5.0` **passes**, `5.1` fails) |
| `{type: number}` | JSON number |
| `{type: bool}` | JSON boolean |
| `{type: date}` | JSON string AND matches `^\d{4}-\d{2}-\d{2}$` (else type-miss error) AND casts to a real date (else valid-ISO error — `2026-02-30` takes this path) |

**JSON `null` is a value, not an absence:** `{"severity": null}` counts as *present* for phases 1–3 and fails phase 4 with the type/enum error. (This is the single most common divergence bug between implementations — it's fixture-covered in both directions.)

## plpgsql skeleton (authoritative shape — implementer completes mechanically)

```sql
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
        WHEN 'string' THEN …  -- typeof + length, per the table above
        WHEN 'int'    THEN …  -- typeof number + trunc equality
        WHEN 'number' THEN …
        WHEN 'bool'   THEN …
        WHEN 'date'   THEN …  -- regex, then BEGIN d := (v #>> '{}')::date;
                              -- EXCEPTION WHEN others THEN valid-ISO error; END
      END CASE;
    END IF;
  END LOOP;
  RETURN errors;
END $$;
```

Note `IMMUTABLE` is honest (no table reads) and lets Postgres cache within statements. The trigger, `pack validate`'s conformance scan ([04](../04-operations-and-governance.md)), and `pack upgrade`'s tightening scan all call this same function — **there is exactly one authority**.

## Traps (each is a committed fixture case)

1. **JSON null vs absent** — both directions (null required key ≠ missing; null optional key fails type check).
2. **`5.0` vs `5.1` for `int`** — jsonb numbers arrive as numeric; trunc-equality, not typeof-int.
3. **`2026-02-30`** — regex passes, cast must fail → the *valid ISO date* error, not the type error.
4. **Multibyte strings** — `char_length` (codepoints), never `octet_length`; a 2000-emoji string passes.
5. **Empty spec + closed default** — `fact`-style types: `{}` spec rejects *any* attr key.
6. **Conditional when the `when` key is itself absent or non-string** — conditional does not trigger.
7. **Both enum and required-missing on the same key** — only the phase-1 error appears (one error per key per phase, and phase 4 skips keys absent from attrs by construction).
8. **Error array ordering** — a case with 4+ simultaneous violations asserting the exact array.

## Test plan

| Test | Assert |
|------|--------|
| `test_attr_spec_pg.py` | Every fixture case via `SELECT engraphy_validate_attrs(...)` — exact array match |
| `test_attr_spec_py.py` | Same file, Python implementation — exact match |
| `test_attr_spec_parity.py` | **Parity fuzz**: 2,000 generated (spec, attrs) pairs (hypothesis or seeded random covering the grammar) — plpgsql and Python outputs identical |
| `test_trigger_wiring.py` | Trigger rejects with ERRCODE 23514 and the joined message; valid rows insert |
| Coverage gate | Python implementation at 100% branch coverage (CI-enforced for this module only) |

## Build order

1. Commit expanded fixture file (≥ 40 cases; starter set is committed — extend, never weaken).
2. Python implementation until `test_attr_spec_py` green.
3. plpgsql until `test_attr_spec_pg` green against the *same file*.
4. Parity fuzzer; divergences fixed in whichever side violates this plan (if the plan itself is ambiguous → `QUESTIONS.md`, per the [deviation protocol](../07-implementation-contracts.md#the-deviation-protocol)).
5. Trigger wiring + `pack validate` reuse.
