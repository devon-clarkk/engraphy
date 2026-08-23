# Engraphy — Implementation Contracts

The determinism kit: exact formulas, canonical wire shapes, error codes, the pack-file schema, the golden-fixture mandate, and the deviation protocol. This document exists because the other docs, while decision-complete, left several behaviors specified in prose — and prose is where two implementations diverge. **If an implementing model follows this document mechanically, two independent implementations should be behaviorally interchangeable.**

**Status:** Living document — normative; where it conflicts with prose in docs 01–06, this document wins
**Last updated:** July 2026
**Revised July 2026 (detail levels):** `search` and `traverse` gain a `detail` parameter (`full` | `summary`; summary = node envelope without `body`). `traverse` defaults to `summary`, `search` to `full` — rationale in the I/O section.
**Audience:** The implementing model (any capability level). Written to be followed, not interpreted.

---

## Table of contents

1. [The deviation protocol](#the-deviation-protocol)
2. [Exact formulas](#exact-formulas)
3. [Canonical tool I/O shapes](#canonical-tool-io-shapes)
4. [Per-argument wire types](#per-argument-wire-types)
5. [Error codes and message format](#error-codes-and-message-format)
6. [Pack file schema](#pack-file-schema)
7. [Golden fixtures: the fixtures-first rule](#golden-fixtures-the-fixtures-first-rule)
8. [Implementation order and definition of done](#implementation-order-and-definition-of-done)

---

## The deviation protocol

Rules for the implementer when the docs are silent or ambiguous — this is the single most important section for a weaker model:

1. **Never invent silently.** If a behavior isn't specified here or in docs 01–06, do not choose creatively.
2. For gaps with an obviously boring option (a default limit, a log format), take the boring option and **record it as one line in `DECISIONS-DELTA.md`** at repo root: `[date] [doc §] gap: <what> — chose: <what> — because: <one clause>`. That file is reviewed at every phase exit and folded back into these docs.
3. For gaps where two reasonable options diverge behaviorally (anything touching visibility, dedup, validation, or money/data loss), **stop and write a `QUESTIONS.md` entry instead of code**. A blocked module with a good question beats a finished module with a guess.
4. Never weaken a test to pass it. If a golden fixture seems wrong, that's a `QUESTIONS.md` entry — fixtures are part of the spec.
5. The acceptance criteria in each doc are executable definitions of done. "Roughly working" does not exist as a state.

## Exact formulas

**Embedding & similarity.** The pinned model's task-instruction prefixes are mandatory (its card's own "must"; adopted 2026-07-16 — embedding unprefixed text is out-of-distribution for this model family). Stored/compared **document** text embeds as `"search_document: " + title + "\n" + body`; search **query** text embeds as `"search_query: " + query` — prefix concatenated directly, no extra separator, exactly one `\n` between title and body, no trimming beyond the model tokenizer's own. The document prefix applies everywhere node text is embedded: write path, dedup candidates, resonance, `update` re-embeds, import. Dedup and resonance therefore stay symmetric (document↔document); only search is asymmetric (query↔document), which is the model's own retrieval design — the two similarity distributions are **not comparable** (the dedup bands and `resonance.floor` are document↔document numbers; `briefing.semantic_floor` is a query↔document number). Default model `nomic-embed-text-v1.5`; output truncated to the first 384 dimensions, then **L2-re-normalized**. All stored and query vectors are unit-norm, so `similarity(a, b) = a · b` (equivalently `1 - cosine_distance`); pgvector operator `<=>` with `vector_cosine_ops`, similarity = `1 - (a <=> b)`.

**Dedup bands** (per-(space, model) config keys `dedup.t_high`, `dedup.t_low`; defaults for the default model: `0.95`, `0.80`). Band selection uses the **maximum** candidate similarity `s`: `s >= t_high` → AUTO-MERGE; `t_low <= s < t_high` → PENDING; `s < t_low` → INSERT. Boundary cases are exactly as the inequalities read (≥ on both lower bounds).

**Config-read contract** (folded back 2026-07-20; applies to `dedup.t_high`, `dedup.t_low`, `resonance.floor`, `briefing.semantic_floor`). The engine reads these keys per call, inside the call's transaction, with **no cache** — a config change is effective on the next write/briefing, mirroring token revocation's no-cache-window posture. Precedence: explicit caller parameter (test surface) > per-space `config` row > code default (`0.95` / `0.80` / `0.75` / `0.50`). A malformed value (unparseable float, or violating `0 < t_low <= t_high <= 1`, floors outside `(0, 1]`) **fails the call loudly** (`ENGRAPHY_INTERNAL`-class — config is operator input, not caller input; silent fallback to defaults is forbidden). Keys are per-space while exactly one embedding model is loaded per instance; model-qualified keys wait for a second model. Search's own constants (`k=60`, top-30 per leg, cap 25) are deliberately **fixed, not config-read, in v1** (Devon, 2026-07-16 — no consumer needs per-space search tuning; the lookup becomes config-read additively when one does).

**Addendum novelty (Jaccard).** Tokenize by: Unicode-lowercase, replace every non-alphanumeric codepoint with a space, split on whitespace, discard empty strings; take the **set** of tokens. `A` = tokens of the incoming body; `B` = tokens of the canonical node's body concatenated with all existing addenda bodies (space-joined). `J = |A ∩ B| / |A ∪ B|` (define `J = 1.0` when both sets are empty). Append an addendum iff `J < 0.8`.

**RRF fusion.** Each leg returns an ordered list (rank 1 = best, up to 30). For every node appearing in either leg: `score = Σ_legs 1 / (60 + rank)` (absent from a leg contributes 0). Sort by score descending; ties broken by `created_at` descending, then `id` ascending (total order — no nondeterministic ties). Return top `limit`.

**Resonance report.** After a non-PENDING write: top-3 nodes by similarity ≥ `0.75` (config `resonance.floor`), any type, writer-readable scopes, `status='active'`, excluding the node just written/merged-into. Each entry: `{id, type, scope, title, similarity (2dp), links: [{type, direction, peer_id, peer_title}] (≤ 5, readable-filtered)}`. Total orders, wire-visible so normative: entries by `similarity` descending then `id` ascending; each entry's links by `(direction, type, peer_id)` before the ≤ 5 cap. Skipped entirely under import mode (import discards envelopes by construction — [02](02-retrieval-and-dedup.md)).

**Briefing semantic-section floor** (July 2026 revision). A `semantic: true` briefing section applies a relevance floor to the vector leg *before* RRF fusion: candidates with query↔document similarity `< briefing.semantic_floor` (per-space config key, same governance family as the dedup keys; default `0.50`) are dropped from that leg — `>=` survives, the same boundary convention as the bands. Lexical-leg hits are never floor-dropped. A node therefore appears in a semantic section only if it clears the floor or lexically matches the hint; a section emptied by the floor returns `"nodes": []`. The floor applies **only** to briefing semantic sections — `search` applies no floor and its envelope is unchanged.

**Rate limits.** Per token, sliding 60s window: 60 read-tool calls, 30 write-tool calls (config-overridable per space via `rate.read_per_min` / `rate.write_per_min`). Exceeding returns `ENGRAPHY_RATE_LIMITED` with `retry_after_ms`. `inbox_review` classifies per call: `action: list` is a read, `promote`/`discard` are writes.

## Canonical tool I/O shapes

Normative envelopes (JSON as it crosses MCP). Every response carries `"v": 1`. Examples are contracts — field names, casing, and nesting are exact; optional fields marked `?` are omitted (never `null`) when absent.

**`write` → inserted:**
```json
{"v": 1, "outcome": "inserted",
 "node": {"id": "…uuid…", "type": "error", "scope": "project-alpha",
          "title": "…", "body": "…", "attrs": {…}, "status": "active",
          "author": "devon", "created_at": "2026-07-06T12:00:00Z"},
 "resonance": [ {…as specified above…} ]}
```

**`write` → auto-merged:**
```json
{"v": 1, "outcome": "merged",
 "canonical": {…node…}, "similarity": 0.97, "addendum_added": true,
 "links_attached": 2, "links_skipped": 0, "resonance": […],
 "instruction": "Compare your write with the canonical node's body: if it contradicts or updates it rather than restating it, call supersede with old_id set to the canonical id to make your version the current fact."}
```

The `instruction` field (July 2026 revision, per the dupstream contradiction finding — [02](02-retrieval-and-dedup.md) §What auto-merge cannot see) is a **static string, always present** on `merged` envelopes, mirroring the pending envelope's `instruction` — it is the engine's side of the caller contract: auto-merge cannot distinguish restatement from negation, so the envelope must say what to check and name the repair verb. `resolve_duplicate`'s and `inbox_review(promote)`'s merged outcomes carry it identically (they return the write envelope verbatim); import mode still strips the report. Byte-pinned by `fixtures/wire/write_merged.json`.

**`write` → pending:**
```json
{"v": 1, "outcome": "needs_confirmation", "pending_id": "…uuid…",
 "expires_at": "…+24h…",
 "candidates": [{"id": "…", "title": "…", "body": "…", "similarity": 0.87}],
 "instruction": "Call resolve_duplicate with resolution 'distinct' or 'merge'."}
```

**`search`** (`detail` parameter: `full` default | `summary`):
```json
{"v": 1, "detail": "full",
 "results": [{"node": {…}, "score": 0.0323, "similarity?": 0.81,
              "edge_count": 3}], "scopes_searched": ["project-alpha", "life"],
 "truncated": false}
```

**`briefing`:** `{"v": 1, "sections": [{"name": "...", "nodes": [{…node…, "linked?": [{…}]}]}], "footer": {"inbox_pending": 3}}` — section order = pack order; empty sections included with `"nodes": []`.

**`traverse`** (`detail` parameter: `summary` default | `full`): `{"v": 1, "detail": "summary", "nodes": [{…node…, "depth": 2}], "edges": [{"src": "…", "dst": "…", "type": "…"}], "truncated": false}` — depth = minimum hops from start; start node included at depth 0.

**Detail levels.** The **summary envelope** is the node envelope with `body` omitted — nothing else changes (title, attrs, status, author, timestamps all present; merge chains resolve identically at both levels). `detail` never affects *which* nodes are returned or their order, only their weight. Default rationale: `search` answers a question — its top-ranked hits are usually read immediately, so `full` keeps the common case one call; `traverse` maps a neighborhood — its worst legal call is 50 nodes × 8,000-char bodies, so `summary` is the default and callers hydrate the ids they actually need via `get`, which is always full. `briefing` and `get` have no `detail` parameter.

**`get`:** `{"v": 1, "nodes": [{…node…, "addenda": […], "edges": {"out": […≤10], "in": […≤10]}}], "missing": ["…ids not found or not readable…"]}`.

**`resolve_duplicate`:** returns the `write` envelope of the final outcome (`inserted` with `"relates_edge_added": true`, or `merged`).

**Links wire shape** (`write.links` / `link.edges[]`, decided 2026-07-16 — one vocabulary, fully explicit endpoints, zero translation to the `edges` columns). A `write.links` item is `{"type": "…", "src_id?": "…", "dst_id?": "…"}` with **exactly one** endpoint present — the omitted one is the node being written. A `link.edges[]` item is the same shape with **both** required. Malformed item (both present, both absent, missing/non-string `type`) → `ENGRAPHY_VALIDATION`; an unreadable or unknown endpoint id → `ENGRAPHY_NOT_FOUND` ([06](06-teams-and-sharing.md)); a well-formed type with no matching rule surfaces as `ENGRAPHY_EDGE_RULE` naming the missing matrix row. Every attach path uses `ON CONFLICT (src_id, dst_id, type) DO NOTHING` (a duplicate item within one request silently dedupes); the `merged` envelope reports `links_attached`/`links_skipped`, the `inserted` envelope reports no counts.

**`supersede`:** returns the inserted node's `write` envelope plus `"superseded": "<old_id>"` (the analogue of `resolve_duplicate`'s rule — a write whose outcome names its side effect). Any non-INSERT band against a third node **refuses the whole call** — no replacement, no edge, no status flip, no `dedup_log` row — with `ENGRAPHY_SUPERSEDE_CONFLICT` naming the colliding node + similarity and instructing resolve-then-retry.

**`update`:** `{"v": 1, "outcome": "updated", "node": {…}}` — mirrors `write.inserted`'s node shape. Never re-runs dedup banding ([03](03-api-auth-and-tenancy.md)); re-embeds iff supplied `title`/`body` changes the stored `title + "\n" + body`; stored `attrs.addenda` is preserved whatever `attrs` the caller sends.

**`inbox_review`:** `promote` returns the `write` envelope of the final outcome, verbatim (promotion runs the full pipeline); the inbox row flips to `promoted` only on a terminal outcome — on `needs_confirmation` it stays `pending` (nothing is contracted to complete a parked intent; re-promoting after resolution auto-merges, idempotent at the data layer). `list` → `{"v": 1, "items": [{"id", "kind", "payload", "scope?", "status", "created_at"}], "truncated": false}` — default filter `status='pending'`, default limit 25, `offset` paging, **oldest-first** (`created_at` asc, `id` asc — triage drains the aged backlog). `discard` → `{"v": 1, "outcome": "discarded", "id": "…"}` — a status flip, never a delete.

**`scope_list`:** `{"v": 1, "scopes": [{"id", "display_name", "visibility", "ambient", "hints", "owner_principal", "created_at"}]}` — the row set is exactly the caller's **readable** set (`engraphy_readable_scopes()`, the single authority; archived scopes never appear), except a `space_admin` sees every scope's *metadata* space-wide (the deliberate, reviewed widening that makes [06](06-teams-and-sharing.md)'s "set visibility" administrable; node visibility is untouched — `nodes_read` gates on the definer function, not the scopes policy). Grant enumeration is deliberately excluded — who *else* sees a scope belongs to the role-gated admin tools. **`scope_create`** → `{"v": 1, "scope": {…same shape…}}`; `confirm` missing/false → `ENGRAPHY_VALIDATION`; duplicate id → `ENGRAPHY_VALIDATION` "scope id 'x' already in use" (accepted, recorded: the unique key must fail somehow, and this reveals a name collision with a possibly-unreadable scope).

**Admin tools** (all four space_admin-gated → `ENGRAPHY_ROLE` otherwise; absent from `tools/list` entirely when config `space_admin_tools: false`; every call audited). **`admin_token_create`** → `{"v": 1, "token": "<plaintext>", "principal": "…", "client_name": "…", "role": "…", "created_at": "…"}` — the plaintext exists in exactly one place ever: this envelope. The server stores only the SHA-256; the audit row records mint metadata, never the token or its hash; there is no retrieve-later path (lost = revoke + re-mint). **`admin_member_add`** → `{"v": 1, "principal": {…created row…}}` (creates the principal only — no personal scope; see [06](06-teams-and-sharing.md)). **`admin_scope_visibility`** → `{"v": 1, "scope": {…updated row, scope_list's shape…}}`. **`admin_grant`** → `{"v": 1, "grant": {"scope_id", "principal", "level"}}`.

Node envelope everywhere = the `write.node` shape above (plus `"resolved_from?": "…id…"` when a merge chain was followed). Timestamps: RFC 3339 UTC, seconds precision. UUIDs lowercase.

**Addenda on the wire** (July 2026 revision). Storage keeps merge history under `attrs.addenda` (the dedup plan's append target); the wire never does: every tool strips `addenda` from the `attrs` it ships, and merge history is surfaced by exactly one tool — `get`, as its top-level `addenda` array (shown above), once. No envelope carries it twice, and no envelope other than `get`'s carries it at all — addenda are hydration-weight data, `get`-only for the same reason bodies are summary-omitted and edges are `get`-only. `attrs.addenda` is engine-managed: `update` preserves the stored key whatever `attrs` the caller sends (round-tripping `get`'s attrs into `update` must not delete merge history), and `addenda` is a **reserved attrs key** — a caller-supplied `attrs.addenda` on `write`/`update`/import is rejected with `ENGRAPHY_VALIDATION` naming the reserved key.

## Per-argument wire types

The envelopes above pin what the server *returns*. This table pins what it *accepts* — added July 2026 to close the gap that made `call_tool(validate_input=False)` and the documentary `inputSchema` defensible: with no argument types pinned anywhere, there was nothing for a schema to assert, so publishing an empty `{}` per property was honest rather than lazy. Types below are JSON types as they arrive over MCP.

**Status: normative and ENFORCED (built 2026-07-21, before E4).** The server validates every `tools/call`'s arguments against this table at one funnel point, via `engraphy/server/wire_types.py` — the code-side transcription of the table — and publishes an `inputSchema` generated from that same spec. Dispatcher-level reads (`arguments["x"]` → `KeyError` → `ENGRAPHY_VALIDATION`) and the dispatchers' own enum checks remain as defense in depth. The "first-party clients are the only supported callers" caveat was the position *until* enforcement landed; the accepted surface is now what this table says it is.

**The enforcement design (normative — added here first per this doc's own rule, now implemented).** Enforcement is Engraphy's own, not the SDK's: a declarative per-tool argument spec (`engraphy/server/wire_types.py`, transcribed from this table — the code-side single source of truth) is validated at the one point every `tools/call` funnels through, against the **resolved core tool's** surface on the **merged** arguments (aliases inherit their target's surface; presets win, so a broken pack preset fails every call loudly rather than sometimes). Violations are `ENGRAPHY_VALIDATION` naming field + rule, per the error table. The SDK's own jsonschema path stays **permanently off** (`call_tool(validate_input=False)`) — now a positive design decision, not a workaround: the SDK `Server`'s process-global `_tool_cache` is shared by `list_tools`/`call_tool` across requests, so per-space tool sets would clobber each other's cached schemas under concurrency, and the SDK's jsonschema errors are not `ENGRAPHY_<CODE>`-shaped, so flipping it on would break this document's own error contract as a side effect. The published `inputSchema` is generated from the same spec (real types, required sets, enums — replacing the documentary `{}` per property) so the advertised schema and the enforced one cannot drift; it is a truthful advertisement for client UIs, never a validation source, which is what keeps the cache hazard dormant.

Enforcement semantics, pinned:
- **Closed argument surface.** An argument name outside this table → `ENGRAPHY_VALIDATION` naming it (the `additionalProperties: false` posture applied to the tool surface; it also makes "aliases may not introduce new arguments" call-time-enforced).
- **Explicit `null` is rejected everywhere**, as already stated below — omit the key instead.
- **integer** means a JSON number with zero fractional part (`25.0` is accepted as `25` — JSON Schema's own convention; client serializers differ on this and the difference carries no information).
- **uuid** arguments are validated as RFC-4122 textual form, accepted case-insensitively (Postgres's own acceptance — the wire does not tighten beyond what the database always meant); the server emits lowercase. A malformed uuid becomes `ENGRAPHY_VALIDATION` instead of a database-level cast failure.
- **Clamps stay clamps:** a well-typed out-of-range integer is clamped as the table states, never rejected. Type errors reject; range excess clamps.
- **Enums** are enforced at this layer; core-layer enum checks remain as defense in depth (their messages are not pinned, only their code).
- **Link items:** this layer checks JSON types only (array of objects; `type`/`src_id`/`dst_id` strings; no other keys). The endpoint-count semantics (exactly-one vs both) stay in the core's `_validate_links_shape` — one contract-holder, no duplicated logic.
- Validation runs **after** the role/rate gates and **before** dispatch — the rate limiter still throttles malformed floods, and the existing gate order is untouched.

Sequencing: this lands **before E4** ([05](05-roadmap.md) records it as an E4-entry item). The tightening can break nobody while first-party clients are the only callers; after E4, the de facto lenient surface is what real consumers would have coded against, and flipping enforcement on would be a breaking change in practice whatever this table says. Build plan: [implementation/wire-type-enforcement-plan.md](implementation/wire-type-enforcement-plan.md).

`?` = optional. Defaults are the server's, applied when the key is absent; an explicit `null` is **not** the same as absent and is not accepted anywhere.

| Tool | Argument | Type | Required | Default / constraint |
|---|---|---|---|---|
| `briefing` | `scope` | string | yes | scope id, or `"all"` |
| | `hint` | string | no | absent ⇒ semantic sections return `nodes: []` |
| `search` | `scope` | string | yes | scope id, or `"all"` |
| | `query` | string | yes | |
| | `types` | array of string | no | node type names; unknown entries match nothing (not an error) |
| | `limit` | integer | no | 25; clamped to 1–25 |
| | `include_inactive` | boolean | no | `false` |
| | `detail` | string enum | no | `"full"` \| `"summary"`; default `"full"` |
| `traverse` | `start_id` | string (uuid) | yes | |
| | `direction` | string enum | yes | `"out"` \| `"in"` \| `"both"` |
| | `edge_types` | array of string | no | absent ⇒ all edge types |
| | `max_depth` | integer | no | 4; clamped |
| | `limit` | integer | no | 50; clamped |
| | `detail` | string enum | no | `"summary"` \| `"full"`; default `"summary"` |
| `get` | `ids` | array of string (uuid) | yes | unknown/unreadable ids come back in `missing` |
| `write` | `scope` | string | yes | scope id (writable set) |
| | `type` | string | yes | registered node type name; the reserved `engraphy_sentinel` → `ENGRAPHY_VALIDATION` (ruled 2026-07-21 — applies to every write-pipeline entry point: `write`, `supersede`, import, promote) |
| | `title` | string | yes | 3–200 chars (DDL CHECK) |
| | `body` | string | yes | 1–8000 chars (DDL CHECK) |
| | `attrs` | object | no | `{}`; `attrs.addenda` is reserved ⇒ `ENGRAPHY_VALIDATION` |
| | `links` | array of link items | no | exactly one endpoint per item (see Links wire shape) |
| | `session_id` | string | no | stored as `source_session` |
| `supersede` | `old_id` | string (uuid) | yes | |
| | *(remaining)* | | | identical to `write`'s |
| `update` | `id` | string (uuid) | yes | |
| | `title` / `body` | string | no | same length CHECKs; re-embeds iff the stored `title + "\n" + body` changes |
| | `attrs` | object | no | stored `attrs.addenda` preserved regardless |
| `link` | `edges` | array of link items | yes | **both** endpoints per item |
| `resolve_duplicate` | `pending_id` | string (uuid) | yes | |
| | `resolution` | string enum | yes | `"distinct"` \| `"merge"` |
| | `merge_into` | string (uuid) | conditional | **required when `resolution == "merge"`** |
| `scope_list` | *(none)* | | | |
| `scope_create` | `id` | string | yes | scope id pattern |
| | `display_name` | string | yes | |
| | `confirm` | boolean | no | `false`; a falsy value ⇒ `ENGRAPHY_VALIDATION` |
| `inbox_review` | `action` | string enum | yes | `"list"` \| `"promote"` \| `"discard"` |
| | `limit` / `offset` | integer | no | `action="list"` only; 25 / 0 |
| | `id` | string (uuid) | conditional | required for `promote` and `discard` |
| | `type`, `scope`, `title`, `body` | string | conditional | required for `promote` (the reviewer authors the write) |
| | `attrs`, `links`, `session_id` | as `write`'s | no | `promote` only |
| `admin_member_add` | `id`, `display_name` | string | yes | |
| | `role` | string enum | no | `"member"` \| `"space_admin"`; default `"member"` |
| `admin_token_create` | `principal`, `client_name` | string | yes | |
| | `role` | string enum | yes | `"readwrite"` \| `"readonly"` |
| `admin_scope_visibility` | `scope_id` | string | yes | |
| | `visibility` | string enum | yes | `"private"` \| `"team-read"` \| `"team-write"` |
| `admin_grant` | `scope_id`, `principal` | string | yes | |
| | `level` | string enum | yes | `"read"` \| `"write"` |

**Two shapes referenced above.** A *link item* is `{"type": string, "src_id"?: string, "dst_id"?: string}` — `write.links` requires exactly one endpoint, `link.edges[]` requires both (full rules under Links wire shape). *uuid* means RFC-4122 textual form — accepted case-insensitively (Postgres's own acceptance), emitted lowercase; under the enforcement design above a malformed uuid is `ENGRAPHY_VALIDATION`, closing the gap where it surfaced as a database-level cast failure.

**Aliases inherit their target's argument types unchanged.** A pack's `tool_aliases` may preset any of its bound tool's declared arguments ([Pack file schema](#pack-file-schema)) and may not introduce new ones, so this table is the complete accepted surface for aliases too.

## Error codes and message format

Errors are returned as MCP tool errors with text `ENGRAPHY_<CODE>: <human sentence>` — the sentence is written for a model that will retry, naming the field and the rule:

| Code | When | Note |
|------|------|------|
| `VALIDATION` | pydantic or trigger rejection | Message names key + rule: `attrs.severity must be one of low\|medium\|high` |
| `NOT_FOUND` | Unknown id, **or readable-permission failure** | Deliberately identical for both ([06](06-teams-and-sharing.md): existence is information) |
| `SCOPE_UNKNOWN` | Scope doesn't exist / not readable — or, on write paths, not writable | Same collapse as NOT_FOUND, at scope level. Write paths check the **writable** set (dedup plan's world-change table: "unwritable → SCOPE_UNKNOWN"); nonexistent, unreadable, and readable-but-unwritable all get the byte-identical message ("does not exist or is not writable" on write paths) so the collapse holds |
| `NEEDS_CONFIRMATION` | Not an error — the PENDING envelope above | Never raised as an MCP error; it's a normal result |
| `EDGE_RULE` | Illegal (type, src, dst) combination — including an unregistered edge type | Message states the rule matrix row that's missing |
| `RATE_LIMITED` | Window exceeded | Includes `retry_after_ms` |
| `ROLE` | readonly token calling a write tool; non-space-admin calling admin tools | |
| `SCOPE` | A token minted `no_scope_all` asked for `scope='all'` (search, briefing) | The second per-token capability after `role`, added August 2026 (migration 0023). Distinct from `SCOPE_UNKNOWN` and deliberately NOT collapsed into it: not-found semantics keep a scope's existence secret from a **principal**, whereas this refuses the **bearer** and so tells the holder only about its own credential. `scope='all'` for an unrestricted token is unchanged — resolved to the principal's readable set and audited, which is what the exfiltration tripwire has always been |
| `PENDING_EXPIRED` | resolve_duplicate after TTL | Instructs re-write |
| `SUPERSEDE_CONFLICT` | `supersede`'s replacement banded MERGE/PENDING against a third node | Whole call rolled back; names the colliding node + similarity, instructs resolve-then-retry (added at the 2026-07-20 fold-back per the supersede-nonclean-band resolution) |
| `INTERNAL` | Anything else — including malformed per-space config values | Never leaks SQL/stack traces |

## Pack file schema

Normative JSON Schema (YAML is parsed then validated against this; shipped as `engraphy/packs/schema.json`):

```json
{"type": "object", "additionalProperties": false,
 "required": ["pack", "version", "node_types", "edge_types"],
 "properties": {
   "pack":    {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,40}$"},
   "version": {"type": "integer", "minimum": 1},
   "pack_format": {"type": "integer", "minimum": 1, "default": 1},
   "node_types": {"type": "object", "minProperties": 1,
     "patternProperties": {"^[a-z][a-z0-9_]{1,40}$": {
       "type": "object", "additionalProperties": false,
       "required": ["description"],
       "properties": {
         "description": {"type": "string"},
         "attrs": {"type": "object", "additionalProperties": false, "properties": {
           "required": {"$ref": "#/$defs/attrmap"},
           "optional": {"$ref": "#/$defs/attrmap"},
           "closed":   {"type": "boolean", "default": true},
           "requires": {"type": "array", "items": {"$ref": "#/$defs/conditional"}}}}}}}},
   "edge_types": {"type": "object",
     "patternProperties": {"^[a-z][a-z0-9_]{1,40}$": {
       "type": "object", "additionalProperties": false, "required": ["description"],
       "properties": {"description": {"type": "string"},
                      "bidirectional": {"type": "boolean", "default": false}}}}},
   "edge_rules": {"type": "array", "items": {
     "type": "object", "additionalProperties": false,
     "required": ["type", "src", "dst"],
     "properties": {"type": {"type": "string"}, "src": {"type": "string"},
                    "dst": {"type": "string"}}}},
   "ambient_scopes": {"type": "array", "items": {"type": "string"}},
   "briefing": {"$ref": "#/$defs/briefing"},
   "tool_aliases": {"type": "object"},
   "tool_descriptions": {"type": "object"}},
 "$defs": {
   "attrmap": {"type": "object", "patternProperties": {"^[a-z][a-z0-9_]{1,40}$": {
     "type": "object", "additionalProperties": false, "properties": {
       "type": {"enum": ["string", "int", "number", "bool", "date"]},
       "enum": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
     "oneOf": [{"required": ["type"]}, {"required": ["enum"]}]}}},
   "conditional": {"type": "object", "additionalProperties": false,
     "required": ["key", "when"], "properties": {
       "key": {"type": "string"},
       "when": {"type": "object", "additionalProperties": false,
                "required": ["key", "equals"],
                "properties": {"key": {"type": "string"}, "equals": {"type": "string"}}}}},
   "briefing": {"type": "object", "additionalProperties": false,
     "required": ["sections"], "properties": {
       "sections": {"type": "array", "maxItems": 8, "items": {
         "type": "object", "additionalProperties": false, "required": ["name"],
         "properties": {
           "name": {"type": "string"},
           "type": {"type": "string"}, "types": {"type": "array", "items": {"type": "string"}},
           "status": {"type": "string", "default": "active"},
           "semantic": {"type": "boolean"}, "top_k": {"type": "integer", "maximum": 10},
           "recent": {"type": "string", "pattern": "^[0-9]+d$"},
           "where_attr": {"type": "object", "additionalProperties": false, "properties": {
             "key": {"type": "string"}, "equals": {"type": "string"},
             "before": {"type": "string"}, "after": {"type": "string"}}},
           "order_by_attr": {"type": "string"},
           "include_linked": {"type": "object", "properties": {
             "edge": {"type": "string"}, "direction": {"enum": ["out", "in"]}}},
           "without_edge": {"type": "object", "properties": {
             "edge": {"type": "string"}, "direction": {"enum": ["out", "in"]}}}}}},
       "footer": {"type": "object", "properties": {
         "inbox_pending_count": {"type": "boolean"}}}}}}}
```

Cross-reference validation beyond the schema (in `pack validate`): every `edge_rules` src/dst/type and every briefing `type(s)`/`edge` must name a type defined in this pack (or `"*"` where permitted); `before`/`after` accept ISO dates or `+Nd` relative form; aliases may bind only to core tools and preset only that tool's declared arguments; name-pattern re-validation of `node_types`/`edge_types`/attrmap keys happens in Python on top of jsonschema (`patternProperties` alone does not reject non-matching keys — resolved 2026-07-15, option (b), for security). **Reserved names** a pack may not declare: the node type `engraphy_sentinel` ([04](04-operations-and-governance.md)'s sentinel convention) and the attrs key `addenda` (engine-managed merge history, above); `pack_format` greater than the engine's `CURRENT_PACK_FORMAT` warns, never refuses ([04](04-operations-and-governance.md)). Both reservations are enforced on the **write path** as well, not only against pack authors (ruled 2026-07-21): every write-pipeline entry point (`write`, `supersede`, import, `inbox_review` promote) refuses a caller naming the type `engraphy_sentinel` with `ENGRAPHY_VALIDATION`, at the same pre-transaction position as the reserved `attrs.addenda` check. This is deliberately an application-layer refusal and not a DDL constraint — the engine's own sentinel mint runs on the privileged `space create` CLI path, and a CHECK cannot distinguish that mint from an agent's write.

## Golden fixtures: the fixtures-first rule

Fixtures are part of the spec and are **written before the code they test** (they're small enough to review; the code is not). Mandatory fixture files, each with expected outputs committed:

| File | Covers | Shape |
|------|--------|-------|
| `fixtures/attr_spec_cases.yaml` | Every construct of the attr-spec grammar × valid/invalid | `{spec, attrs, expect: ok | error-substring}` — ≥ 40 cases |
| `fixtures/dedup_cases.yaml` | Band selection & merge mechanics | Text pairs + **pinned model+version** + expected band; similarity asserted within ±0.02 (fixtures re-baselined only on deliberate model change) |
| `fixtures/jaccard_cases.yaml` | The tokenizer + novelty threshold | `{body_a, body_b, expect_j (4dp), expect_addendum}` — includes Unicode, punctuation, empty cases |
| `fixtures/rrf_cases.yaml` | Fusion arithmetic + tie-breaking | Synthetic leg rankings → exact output order |
| `fixtures/briefing/*.yaml` | Every section construct against a seeded graph | Seed + pack fragment + expected section contents (ids, order) |
| `fixtures/visibility_matrix.py` | [06's](06-teams-and-sharing.md) generated matrix | Generator committed; expected outcomes exhaustive over (level, grant, role) × operation |
| `fixtures/packs/` | Valid + minimally-invalid pack files (one per schema rule) | Validation outcomes |
| `fixtures/wire/*.json` | The canonical I/O shapes above, byte-exact | Golden request/response pairs per tool per outcome. Every pinned **request** is mechanically replayed against the wire-type spec (`test_wire_types.py`), with no exemptions — added 2026-07-21 after a fixture was found pinning a request the server has always refused (QUESTIONS "wire-fixture-merge-into"). An artifact called normative that no test executes is a claim, not a check. Response-side replay is not yet built. |

## Component implementation plans

Three components carry concentrated risk (subtle semantics + severe failure modes) and have dedicated plans under [`design/implementation/`](implementation/) — each with the exact algorithm, near-complete authoritative skeletons, an enumerated trap list (every trap fixture-covered), and its own build order. **These plans are normative at the same level as this document:**

| Plan | Component | Why it earned a plan |
|------|-----------|----------------------|
| [attr-spec-interpreter-plan.md](implementation/attr-spec-interpreter-plan.md) | The dual plpgsql/Python validation kernel | Two implementations held identical by shared fixtures + parity fuzz; JSON-null and date-cast traps |
| [visibility-and-rls-plan.md](implementation/visibility-and-rls-plan.md) | Readable/writable-scope functions, GUC protocol, RLS policies | A mistake here leaks a teammate's private memory; SECURITY DEFINER recursion-break and FORCE RLS are non-obvious |
| [dedup-write-path-plan.md](implementation/dedup-write-path-plan.md) | The single write pipeline all writes travel | Concurrent-duplicate race (advisory lock), merge atomicity, pending-vs-changed-world table |

## Implementation order and definition of done

Module order within the E-phases (each module is done when its fixtures + its doc's acceptance rows pass; no module starts before its dependencies' fixtures are green):

```
E0: schema.sql → attr_spec interpreter (fixtures) → triggers (fixtures)
    → RLS + readable_scopes (visibility matrix) → pack validate/apply (pack fixtures)
E1: embedding (pinned model, dims, norm test) → jaccard (fixtures) → dedup (fixtures)
    → search+RRF (fixtures) → briefing (fixtures) → traverse → inbox → import
E2: auth/tokens → GUC transaction wrapper → tools (wire fixtures, in table order)
    → aliases → rate limits → audit → admin CLI
E3–E6: as the roadmap states; no new normative contracts expected — if one appears,
    it is added HERE first, then implemented.
```

Global definition of done, restated from the roadmap and binding: CI green on the full matrix, `DECISIONS-DELTA.md` empty or reviewed, `QUESTIONS.md` empty, and the relevant doc's acceptance checklist executed with evidence (test run links or recorded manual procedure).
