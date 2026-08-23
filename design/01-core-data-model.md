# Engraphy — Core Data Model

The storage engine and schema machinery: spaces, scopes, the type registries that make schema enforcement a data-layer guarantee for *any* pack, the node/edge tables, the attr-spec language, and row-level security.

**Status:** Living document
**Last updated:** July 2026
**Scope:** All DDL, the registry/pack model, trigger enforcement, tenancy at the data layer
**Revised July 2026 (tenancy v2):** spaces redefined as trust boundaries containing **principals** with scope-level **visibility** — the team-sharing model is specified in [06](06-teams-and-sharing.md); this doc carries the resulting DDL
**Out of scope:** Retrieval/dedup behavior ([02](02-retrieval-and-dedup.md)), API/auth ([03](03-api-auth-and-tenancy.md)), a pack's semantics (the *meanings* of Error/Pattern/Decision/Check live in the pack, not the engine)

> **Design note:** The storage-engine decision record (Postgres 16 + pgvector, native install, rejected alternatives) is not re-argued here. What changed: enums → registries, single-tenant → spaces, `life`-scope special case → generic *ambient* scopes.

---

## Table of contents

1. [Goals](#goals)
2. [Decision record: registries as data, not DDL](#decision-record-registries-as-data-not-ddl)
3. [Decision record: packs are declarative](#decision-record-packs-are-declarative)
4. [Tenancy: spaces](#tenancy-spaces)
5. [Scopes and ambience](#scopes-and-ambience)
6. [The attr-spec language](#the-attr-spec-language)
7. [Table definitions](#table-definitions)
8. [Trigger enforcement](#trigger-enforcement)
9. [Row-level security](#row-level-security)
10. [Indexes](#indexes)
11. [File reference](#file-reference)
12. [Testing and validation](#testing-and-validation)
13. [Acceptance criteria](#acceptance-criteria)
14. [Rollback plan](#rollback-plan)
15. [Deferred work](#deferred-work)

---

## Goals

### Primary

- **Schema enforcement without hardcoded schema.** The engine must reject a wrong type, an illegal edge, or a malformed attribute at the database layer — for type systems it has never heard of, installed as data.
- **Hard isolation between spaces.** Two trust boundaries on one server can never see, link to, dedup against, or infer the existence of each other's memories. *Within* a space, multiple principals share memory under the scope-visibility model of [06](06-teams-and-sharing.md) — private by default, shared by choice.
- **Same guarantees as the single-tenant design:** ACID, no hard deletes of knowledge, embeddings mandatory, provenance on every row.

### Secondary

- A shipped starter pack proving the pack mechanism and serving as the sane default for a new space.
- Pack evolution (adding a type, tightening an attr spec) without engine releases.

### Non-goals

- Arbitrary code execution in packs (see decision record).
- Cross-space sharing/federation — not even opt-in. Team sharing exists, but it lives *inside* a space ([06](06-teams-and-sharing.md)); the between-spaces wall stays absolute.
- Storing artifacts (files, transcripts). Nodes hold distilled knowledge; a pack can define a `resource`-like pointer type.

---

## Decision record: registries as data, not DDL

The single-tenant design used Postgres **enums** (`node_type`, `edge_type`) — unknown types were unrepresentable. Enums are DDL: adding Alex's space with different types would mean shared, migration-gated, instance-wide type lists. Wrong shape for a product.

**Decision: types live in registry tables (`node_types`, `edge_types`, `edge_rules`), keyed by space, installed by packs. Node/edge rows carry `type text` with a composite FK into the registry, and validation triggers read the registry.**

What's preserved and what's traded, honestly:

| Property | Enums (before) | Registries (now) |
|---|---|---|
| Unknown type rejected at data layer | Yes (unrepresentable) | Yes (FK violation) |
| Per-type attr validation at data layer | Trigger with hardcoded CASE | Trigger interpreting the registry's attr-spec — same guarantee, now generic |
| Per-space type systems | No | **Yes — the point** |
| Type list change | DDL migration (engine release) | `engraphy-admin pack apply` (data change, still versioned/audited — [04](04-operations-and-governance.md)) |
| Typo-proofing of the registry itself | n/a | Pack files are the only writer of registries; hand-editing registry rows is not a supported operation |

Rejected: per-space Postgres schemas with per-space enums (N schemas × migrations = operational fan-out; cross-space code paths diverge); one shared enum superset (spaces see each other's type names — an isolation leak and a coordination trap).

## Decision record: packs are declarative

**Decision: a pack is a versioned YAML document — node types with attr-specs, edge types, edge rules, ambient-scope designation, briefing spec ([02](02-retrieval-and-dedup.md)), and tool aliases ([03](03-api-auth-and-tenancy.md)). No executable code in packs, v1.**

Why: everything the two shipped packs need (the example pack's full type system; the starter pack) is expressible declaratively — checked by writing both packs before freezing the format. Declarative packs are diffable security reviews, can't crash the server, and can be validated exhaustively. The cost: genuinely novel *behavior* (a custom composed tool beyond aliasing) needs an engine release. Accepted; code plugins are deferred work with an entry-point design sketched there, to be built only when a real pack hits the ceiling.

```yaml
# pack.yaml (shape; the example pack is the canonical complete one)
pack: example
version: 1
node_types:
  error:
    description: "A concrete thing that went wrong…"
    attrs:
      required:
        severity:    {enum: [low, medium, high]}
        happened_at: {type: date}
      optional:
        surface:     {type: string}
      closed: true
edge_types:
  derived_from: {description: "…", bidirectional: false}
edge_rules:
  - {type: derived_from, src: pattern, dst: error}
  - {type: involves, src: "*", dst: person}      # '*' expands against this pack's types at apply time
ambient_scopes: [life]        # scope ids to mark ambient when present
briefing: …                   # see 02
tool_aliases: …               # see 03
```

---

## Tenancy: spaces and principals

A **space** is one *trust boundary's* entire memory world: a person (`nova`, `alex`) or a team (`acme-studio`). Within a space live **principals** (members) — full model in [06](06-teams-and-sharing.md); a single-person space has exactly one principal and behaves identically to the pre-v2 design. Properties:

- Every data row carries `space_id`. There are no instance-global nodes, scopes, or types.
- One pack installed per space (a space without a pack accepts no writes). Two spaces may run the same pack at different versions.
- Per-space config (dedup thresholds, search constants) in a `config` table keyed by space.
- Tokens bind to exactly one **(space, principal)** ([03](03-api-auth-and-tenancy.md)); nothing in the MCP surface takes a space or principal argument — **the token *is* the identity**, which makes cross-space requests inexpressible rather than merely forbidden.
- Embedding model is per-**instance**, not per-space (one loaded model, one geometry; recorded per row for migration — [04](04-operations-and-governance.md)).

## Scopes and ambience

A scope is a context partition inside a space (projects, work areas — the critique's "six or more separate work areas" become six scope rows, not six string prefixes).

- Reads are scope-filtered in SQL; `scope='all'` means *all scopes readable by the requesting principal* ([06](06-teams-and-sharing.md)) and is never a default.
- Scopes carry **`owner_principal`** and **`visibility`** (`private` / `team-read` / `team-write`), plus per-principal `scope_grants` exceptions — the sharing model, specified in [06](06-teams-and-sharing.md).
- A scope may be flagged **`ambient`**: ambient scopes join the scope-set of every query *by principals who can read them* (personal-private ambient = one member's always-on context; team-read ambient = org-wide context). This generalizes the `life` scope into a mechanism any pack can use.
- Cross-scope edges: creatable when the principal can read both endpoints and write at least one; visible only to principals who can read both ([06](06-teams-and-sharing.md) — this replaces the earlier ambient-endpoint rule). Cross-**space** edges are impossible (FK + trigger).

## The attr-spec language

The deliberately small validation language that a plpgsql trigger can interpret — **the subset is the product**; if a pack needs more, that's a signal the data belongs in `body` or a separate node:

| Construct | Meaning |
|-----------|---------|
| `{type: string}` | JSON string, ≤ 2000 chars |
| `{type: int}` / `{type: number}` | JSON number (int-checked for `int`) |
| `{type: bool}` | JSON boolean |
| `{type: date}` | String, ISO-8601 date, validated by cast |
| `{enum: [a, b, c]}` | String from the list |
| `required:` / `optional:` | Key presence classes |
| `closed: true` | Unknown keys rejected (default true; `false` permitted but discouraged) |
| `requires: {key: X, when: {key: Y, equals: Z}}` | One conditional form — covers "command required iff method=command". The only non-flat construct; anything fancier is refused at pack validation |

No nesting, no arrays (v1), no regex, no cross-field arithmetic. `engraphy-admin pack validate` rejects specs outside this grammar before anything touches the registry.

---

## Table definitions

Authoritative shapes (full DDL in `engraphy/db/migrations/`):

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE spaces (
  id           text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  display_name text NOT NULL,
  pack_name    text,                          -- set by pack apply
  pack_version integer,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE node_types (
  space_id  text NOT NULL REFERENCES spaces(id),
  name      text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{1,40}$'),
  description text NOT NULL,
  attr_spec jsonb NOT NULL,                   -- the attr-spec language
  PRIMARY KEY (space_id, name)
);

CREATE TABLE edge_types (
  space_id      text NOT NULL REFERENCES spaces(id),
  name          text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{1,40}$'),
  description   text NOT NULL,
  bidirectional boolean NOT NULL DEFAULT false,   -- traversal hint (e.g. relates_to)
  PRIMARY KEY (space_id, name)
);

CREATE TABLE edge_rules (
  space_id  text NOT NULL,
  type      text NOT NULL,
  src_type  text NOT NULL,
  dst_type  text NOT NULL,
  PRIMARY KEY (space_id, type, src_type, dst_type),
  FOREIGN KEY (space_id, type)     REFERENCES edge_types (space_id, name) ON DELETE CASCADE,
  FOREIGN KEY (space_id, src_type) REFERENCES node_types (space_id, name),
  FOREIGN KEY (space_id, dst_type) REFERENCES node_types (space_id, name)
);
-- '*' wildcards in pack files are EXPANDED to concrete rows at apply time:
-- the runtime check is a plain lookup, never wildcard logic.

CREATE TABLE scopes (
  space_id   text NOT NULL REFERENCES spaces(id),
  id         text NOT NULL CHECK (id ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  display_name text NOT NULL,
  owner_principal text,                       -- NULL for ownerless team-write scopes (06)
  visibility text NOT NULL DEFAULT 'private'
             CHECK (visibility IN ('private','team-read','team-write')),
  ambient    boolean NOT NULL DEFAULT false,
  hints      text[] NOT NULL DEFAULT '{}',    -- client-side context matching (repo URLs etc.)
  archived   boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (space_id, id),
  FOREIGN KEY (space_id, owner_principal) REFERENCES principals (space_id, id)
);
-- principals and scope_grants: DDL in 06-teams-and-sharing.md

CREATE TABLE nodes (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  space_id       text NOT NULL,
  type           text NOT NULL,
  scope_id       text NOT NULL,
  title          text NOT NULL CHECK (char_length(title) BETWEEN 3 AND 200),
  body           text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 8000),
  attrs          jsonb NOT NULL DEFAULT '{}',
  status         text NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','superseded','merged','archived')),
  canonical_id   uuid REFERENCES nodes(id),
  embedding      vector(384) NOT NULL,
  embedding_model text NOT NULL,
  search         tsvector GENERATED ALWAYS AS (
                   setweight(to_tsvector('english', title), 'A') ||
                   setweight(to_tsvector('english', body),  'B')) STORED,
  source_client  text NOT NULL,               -- token's client name (server-set)
  author_principal text NOT NULL,             -- token's principal (server-set; 06)
  source_session text,
  recall_count   integer NOT NULL DEFAULT 0,
  last_recalled_at timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (space_id, type)     REFERENCES node_types (space_id, name),
  FOREIGN KEY (space_id, scope_id) REFERENCES scopes (space_id, id),
  CHECK ((status = 'merged') = (canonical_id IS NOT NULL))
);

CREATE TABLE edges (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  space_id   text NOT NULL REFERENCES spaces(id),
  src_id     uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  dst_id     uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  type       text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (src_id, dst_id, type),
  CHECK (src_id <> dst_id),
  FOREIGN KEY (space_id, type) REFERENCES edge_types (space_id, name)
);
```

Support tables, with `space_id` added: `inbox`, `dedup_log`, `pending_writes`, `audit_log`, `config (space_id, key, value)`, and instance-level `api_tokens` (see [03](03-api-auth-and-tenancy.md)) and `schema_migrations`. Node status semantics (no hard deletes; merge chains via `canonical_id`) are unchanged.

## Trigger enforcement

Three generic triggers replace the hardcoded ones:

1. **`nodes_validate_attrs`** — loads the row's `(space_id, type)` attr-spec from `node_types` and interprets it: required-key presence, per-key type/enum checks, `closed` handling, the one conditional form. ~150 lines of plpgsql, exhaustively unit-tested against the spec grammar. Error messages name the key and the rule (they are read by a model that will retry). The stored `attr_spec` wrapper shape is exactly the interpreter's `spec` argument: `{"attrs": {"required": …, "optional": …, "closed": …, "requires": …}}` — pack apply and the trigger call `engraphy_validate_attrs()` with zero translation. **One reserved-key exemption** (migration 0017): the engine-managed `addenda` key is exempt from the closed-spec unknown-key check on **both** interpreters (plpgsql and the Python mirror, held identical by the parity fuzzer — whose generated-key pool must contain `addenda`, or the two sides can only agree by both being wrong, which is how the original bug shipped: without the exemption, no `closed: true` node type could ever receive a merge addendum, i.e. every type in both shipped packs).
2. **`edges_validate`** — both endpoints exist in `space_id` (cross-space edge = exception), rule row exists in `edge_rules`; the visibility-side creation rule (read-both / write-one) is enforced in the write path against `engraphy_writable_scopes()` — the trigger enforces the structural half, the auth layer the principal half ([06](06-teams-and-sharing.md)).
3. **`nodes_touch`** — `updated_at`, plus consistency guard: `canonical_id` target must be same space + same type. **Recall-skip** (migration 0012): the trigger preserves `OLD.updated_at` when every content column is unchanged — a recall-stats bump (`recall_count`, `last_recalled_at`) is a read wearing an UPDATE, and must not corrupt "last content modification"; `updated_at` stamps only on INSERT or a real content edit.

Plus the FK web above: even with every trigger dropped, an unknown type or dangling scope is still an FK violation — two independent layers, same philosophy as before.

## Row-level security

The single-tenant design rejected RLS as pointless. Multi-principal reverses that:

**Decision: RLS enabled on all data tables. Two policy layers: space pinning (`space_id = current_setting('engraphy.space_id')`) and, on nodes/edges, readable-scope filtering via `engraphy_readable_scopes()` reading both GUCs (`engraphy.space_id`, `engraphy.principal`) — the same function the application queries use, so app and backstop cannot disagree ([06](06-teams-and-sharing.md)). The app's DB role is *not* `BYPASSRLS`.**

This is defense-in-depth, not the primary mechanism (the primary is that the API can't express cross-space or cross-principal queries — [03](03-api-auth-and-tenancy.md)): a bug in any query that forgets a filter returns nothing instead of someone else's memory. The isolation promise is the product's most important property, so it gets two layers like everything else load-bearing.

Write policies grew **per-tool and additively** as E1/E2 landed each behavior (the migrations are the authoritative list): the schema's one sanctioned DELETE is `pending_writes` (resolution consumes the parked row — the grant and the policy are one unit and must never be split, since under FORCE RLS a grant without a policy makes the DELETE silently match zero rows); inbox INSERT/UPDATE are space-pinned + writable-or-unscoped (triage is shared, unlike author-private pending writes); the four `admin_*` tools' writes are backstopped by space_admin-predicated policies (migration 0016), so even a bypassed app-layer gate cannot make a non-admin's write stick. `config` and the registries are **non-RLS reference tables** read with an explicit token-bound `space_id` filter — the repo-wide reference-table pattern, not an oversight.

## Indexes

With `space_id` prefixed where it sharpens selectivity: `(space_id, scope_id, type, status)`, HNSW on `embedding` (instance-wide; dedup/search queries always filter space first), GIN on `search`, `(space_id, status) WHERE status='pending'` on inbox, partial index on `canonical_id`, `edges (src_id, type)` / `(dst_id, type)`.

---

## File reference

| File | Contents |
|------|----------|
| `engraphy/db/migrations/*.sql` | dbmate migrations: extensions, spaces, registries, scopes, nodes/edges, triggers, support tables, RLS, indexes |
| `engraphy/db/attr_spec.sql` | The attr-spec interpreter function (own file — it's the subtle one) |
| `engraphy/packs/starter/pack.yaml` | Shipped starter pack: `note`, `person`, `preference`, `commitment`, `project_ref` + minimal edges — the default for a new space (Alex's day-one pack) |
| `engraphy/admin/packs.py` | `pack validate` / `pack apply` / `pack upgrade` ([04](04-operations-and-governance.md)) |
| `engraphy/tests/test_schema_*.py` | Constraint tests below |

*(The example pack is a self-contained pack committed here at `engraphy/tests/fixtures/packs/example-pack.yaml` so this repo's CI and tests run standalone.)*

---

## Testing and validation

The full constraint suite, re-expressed generically and extended:

| Test | Assert |
|------|--------|
| Unknown type / dangling scope / illegal edge / bad attrs (each construct of the spec language, valid + invalid) | Rejected by Postgres with the trigger's named-key message |
| The conditional form | `command` required iff `method=command` — both directions |
| Pack apply: wildcard expansion | `involves: * → person` expands to exactly the pack's type count |
| Two spaces, same type names, different attr-specs | Each space enforced against its own spec |
| Cross-space edge | Impossible (trigger + FK) |
| Cross-scope edge creation (read-both / write-one) | [06](06-teams-and-sharing.md)'s rule enforced in both layers (`edges_write` RLS policy + write path); *(the v1 "one endpoint must be ambient" row that stood here was retired by 06 and was never implemented — corrected at the 2026-07-20 fold-back)* |
| RLS probe | Session with `engraphy.space_id = 'alex'` sees zero `nova` rows through every query shape, including a deliberately filter-free `SELECT`; within a team space, a member-pinned session sees zero rows from teammates' private scopes the same way |
| Concurrent writers | Two connections × 500 mixed inserts across two spaces — zero errors, zero lost rows, zero cross-space leakage |
| Both shipped packs | `pack validate` passes; `pack apply` from empty; the memory chain (Error→Pattern→Decision→Check) inserts and traverses under the example pack |

## Acceptance criteria

- [ ] Migrations from empty DB, zero manual steps; both packs apply cleanly.
- [ ] Every constraint test passes via **raw SQL** (server bypassed).
- [ ] RLS probe passes; app role confirmed non-`BYPASSRLS`.
- [ ] The memory chain (Error→Pattern→Decision→Check) inserts, links, and traverses under the example pack (`engraphy/tests/fixtures/packs/example-pack.yaml`) — standalone CI. (The full acceptance-surface reproduction is the E4 adoption gate in [05](05-roadmap.md) — not an E0 blocker.)
- [ ] Attr-spec interpreter: 100% branch coverage (it is the enforcement kernel).

## Rollback plan

Pre-adoption: drop/recreate. Post-adoption: the migration policy applies (additive-first, two-release destructive, restore-tested backups before destructive changes — [04](04-operations-and-governance.md)). Pack rollback: `pack apply` of the prior version, subject to the row-conformance check in [04](04-operations-and-governance.md).

## Deferred work

| Item | Rationale |
|------|-----------|
| Code plugins (entry-point profiles) | Build when a real pack hits the declarative ceiling |
| Array/nested attr-spec constructs | The flat grammar has not yet pinched |
| Cross-space sharing | Isolation promise outranks hypothetical demand |
| Per-space embedding models | One geometry per instance until a real conflict appears |
