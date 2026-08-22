# Wire-type enforcement — implementer plan

Executes the 2026-07-21 ruling (DECISIONS-DELTA.md; normative semantics in
[design/07 §Per-argument wire types](../07-implementation-contracts.md#per-argument-wire-types)).
Scope: one session. **Builds BEFORE E4** — see design/05's E4-entry items. This plan is procedural;
where it and 07 disagree, 07 wins.

## What is being built

Dispatcher-side validation of every `tools/call`'s arguments against 07's per-argument table,
plus a truthful published `inputSchema` generated from the same spec. The MCP SDK's own
validation path (`validate_input=True` + `_tool_cache`) is **never** enabled — permanent design,
ruled, not revisitable without a new DECISIONS-DELTA entry.

## Module: `engraphy/server/wire_types.py`

1. **The spec.** One declarative table, transcribed from 07's per-argument table — every core and
   admin tool, every argument: JSON type (`string | integer | boolean | object | array_of_string |
   array_of_link_items`), required flag, enum values where 07 lists them. Aliases need no entries:
   validation runs on the *resolved core tool* with *merged* arguments. Transcribe, do not invent —
   any mismatch you notice between 07's table and a dispatcher's actual reads is a QUESTIONS.md
   entry, not a silent fix in either direction.
2. **`validate(core_name: str, arguments: dict) -> None`**, raising the existing
   `ValidationError`-class path (or `ToolError("VALIDATION", …)` directly — match how the funnel
   point already shapes errors). Checks, in order, per 07's pinned semantics:
   - unknown argument name → refuse, naming it (closed surface);
   - explicit `null` anywhere → refuse ("omit the key instead" phrasing in the message);
   - required-argument presence (including the two conditionals: `merge_into` iff
     `resolution == "merge"`; `inbox_review`'s per-action requirements — encode these directly,
     they are why generic jsonschema was rejected);
   - JSON type per spec — `integer` accepts a JSON number with zero fractional part;
   - enum membership;
   - uuid-typed strings: RFC-4122 textual form, case-insensitive.
   Every message names field + rule (07's error-table contract). Do NOT reject out-of-range
   integers — clamping stays downstream, where it already lives.
   Link items: type-check only (array of objects; `type`/`src_id`/`dst_id` strings; no other
   keys). Endpoint-count semantics stay in core `_validate_links_shape` — do not duplicate them.
3. **`input_schema(core_name: str) -> dict`** — a real JSON Schema generated from the same spec
   (types, `required`, `enum`; `additionalProperties: false`). This replaces
   `tool_registry._input_schema`'s `{name: {} …}` body; `_REQUIRED_ARGS`/`_ADMIN_TOOL_ARGS`/
   `_ADMIN_REQUIRED_ARGS` in tool_registry.py collapse into the spec (single source — delete the
   duplicates, don't mirror them).

## Wiring

- `app.py::handle_call_tool`: insert `wire_types.validate(core_name, merged_args)` **after** the
  role/rate gates and **before** dispatch (07 pins this position — malformed floods stay
  rate-throttled; gate order untouched). `validate_input=False` stays; update `app.py`'s and
  `tool_registry.py`'s docstrings, which still say "no 07 fixture pins per-argument wire types" —
  stale since 07's table landed.
- Dispatchers keep their `arguments["x"]` reads and enum checks (defense in depth; their messages
  were never pinned, only their code). Do not refactor them in this session.

## Tests

- Unit tests over `validate()`: per rule class (unknown key, null, missing required, both
  conditionals both ways, wrong type per JSON type, integral-float accepted, enum miss, uuid
  malformed / uppercase-accepted), plus one loop asserting every spec'd tool accepts its own
  golden wire fixture's request arguments where one exists (`fixtures/wire/*.json`) — the spec
  must not reject the pinned contract.
- Wire-level: one test per error class through the real funnel (server test harness), asserting
  `ENGRAPHY_VALIDATION` text shape; one alias test (bad-typed arg through a pack alias refused
  identically — 03's "same validation" line, now enforced); one test that a well-typed
  out-of-range `limit` is still clamped, not refused.
- `tools/list` test: published schemas now carry real types/required/enums; two spaces with
  different packs still serve different tool lists with no bleed (extend the existing isolation
  suite case rather than duplicating it).
- Existing suite: expect some tests that send sloppy arguments (null-as-absent, wrong types) to go
  red — fix the TEST to the contract, never the validator to the test, per the deviation protocol.
  If a red test reveals a *fixture* sending out-of-contract arguments, that is a QUESTIONS.md
  entry (fixtures are spec).

## Explicitly out of scope

- Flipping `validate_input=True` or touching the SDK's `_tool_cache` — ruled out permanently.
- New wire arguments, `search(min_similarity=…)`, or any surface change — this build changes what
  is *refused*, never what a correct call *does*.
- Golden wire fixtures: none change. If one appears to need changing, stop — that is a
  QUESTIONS.md entry, because it means the spec transcription and the pinned contract disagree.
