# Architecture Overview

Engraphy stores memory as a **typed knowledge graph in Postgres**. Everything an
agent remembers is a row in `nodes`; everything relating two memories is a row in
`edges`. The shape of those types — what a `person` is, which attributes a `fact`
carries, which edges may connect them — is declared per space by a **pack**
(see [Build your own pack](03-packs.md)). The engine itself is
**mechanism, not policy**: it does not know what a "person" is; it knows the pack
said this node type exists with this attribute schema and these edge rules.

## 1. The memory model

### Nodes

A node is one durable memory. The columns that matter to a developer:

| Column | Meaning |
|---|---|
| `id` | uuid, the handle you pass to `get` / `traverse` / `supersede`. |
| `type` | a pack-declared node type (`fact`, `event`, `person`, `decision`, `note`, …). |
| `scope_id` | which scope (isolation container) the node lives in. |
| `title`, `body` | the memory's summary and full text. `title` 3–200 chars, `body` 1–8000. |
| `attrs` | a JSON object validated against the type's attribute schema (in the DB, by trigger). |
| `embedding` | a 384-dim pgvector, unit-normalised, of the node's searchable surface. |
| `search` | a generated `tsvector` (title=A, body=B, searchable attrs=C, addenda=D) for the lexical leg. |
| `status` | `active`, `pending` (a parked duplicate-check), `superseded`, or `archived`. |
| `recall_count` | bumped each time the node is surfaced by a read (recall stats). |
| `source_client`, `author_principal` | provenance. |

Node **types** and their attribute schemas live in the per-space `node_types`
registry. Attribute validation is enforced by a **database trigger**, not by
application code, so a malformed write is rejected at the row, whatever path wrote
it.

### Edges

An edge is a typed, directed relationship: `(space_id, src_id, dst_id, type)`. Edge
**types** live in the `edge_types` registry (`bidirectional` true/false), and
**edge rules** (`edge_rules`: `type`, `src_type`, `dst_type`) constrain which node
types an edge may connect — also enforced in the database. Shipping edge types
include:

- `involves` — this memory concerns / was stated by this **person**.
- `references` — this memory is about this **thing/project**.
- `works_at`, `knows` — entity relationships (conversational pack).
- `supersedes` — this node replaces an older one (attached by the `supersede` tool).
- `relates_to` — a generic association, also the "declared distinct" handshake.
- `same_topic` — **attached automatically by the engine** when two writes are
  near-duplicates but each carries its own information (see §3). Walk it to see
  every stored statement on a topic.

## 2. Isolation: spaces, principals, scopes, and RLS

- A **space** is a top-level tenant. Node types, edges, principals, and scopes are
  all space-scoped; two spaces share nothing.
- A **principal** is an actor within a space (a user, an agent). Each principal
  created through the operator CLI gets a private, *ambient* `personal-<id>` scope.
- A **scope** is an isolation container *inside* a space with a `visibility`
  (`private`, `team-read`, `team-write`) and an owner. Dedup candidate queries are
  scope-filtered, so conversation A's facts never become merge candidates for
  conversation B.
- **Row-Level Security enforces all of this in Postgres.** The application role is
  **not** `BYPASSRLS`; `nodes`, `edges`, `scopes`, `scope_grants`, `principals`,
  and more carry RLS policies keyed on the space and the reader's readable scopes.
  An unreadable row is simply *not there* for that reader — "existence is
  information" — which is why `get` returns unknown/unreadable ids in a `missing`
  list rather than raising.

Every read/write runs inside `engraphy.server.db.transaction(pool, space_id,
principal)`, which sets the Postgres session variables the RLS policies read.

## 3. The write path — dedup-gated, non-destructive

A write never blindly inserts. It is **banded** against existing memory by
embedding similarity, and the band decides what happens. The three thresholds are
per-space config (defaults shown), read on every write:

| Config key | Default | Meaning |
|---|---|---|
| `dedup.t_high` | `0.95` | at/above this cosine → **merge** band |
| `dedup.t_low` | `0.80` | `[t_low, t_high)` → **pending** band |
| `resonance.floor` | `0.75` | related-memory report threshold (context, not dedup) |

Within the **merge** band, Engraphy does **not** absorb blindly. It compares the
incoming text against the matched cluster's existing statements by Jaccard
overlap:

- **Non-novel** (`J ≥ 0.8` against the cluster) → **absorb**: the incoming is a
  restatement; the canonical node stands, optionally gaining an addendum.
- **Novel** (`J < 0.8`) → **merge-link**: the incoming is the *same topic* but
  *distinct content*, so it is inserted as its **own embedded, searchable node**
  and joined to the canonical by a `same_topic` edge. Nothing is lost. This is the
  **non-destructive dedup** property (invariant I1).

```mermaid
flowchart TD
    W[write: title, body, attrs, scope] --> E[embed searchable surface]
    E --> C[find candidates in scope, by cosine]
    C --> B{top similarity?}
    B -->|>= t_high 0.95| M{Jaccard vs cluster?}
    B -->|[t_low 0.80, t_high)| P[PENDING: park a duplicate-check verdict]
    B -->|< t_low| I[INSERT: new active node]
    M -->|J >= 0.8 non-novel| A[ABSORB: keep canonical, optional addendum]
    M -->|J < 0.8 novel| L[MERGE-LINK: new member node + same_topic edge]
    I --> LOG[dedup_log + audit]
    A --> LOG
    L --> LOG
    P --> LOG
    LOG --> CM[COMMIT]
    CM --> R[resonance report: related nodes >= resonance.floor, excl. self]
    R --> OUT[return: node OR pending verdict, + resonance]
```

The **pending** band is the important safety case: a borderline duplicate is not
guessed. `write` returns a verdict the caller resolves with `resolve_duplicate`
(`distinct` keeps both; `merge` folds them). Bulk `import` routes pending items to
a review-queue CSV instead of parking them.

After commit, the write returns a **resonance report**: other memories above
`resonance.floor` similarity (excluding the node just written) — related context,
never part of the dedup decision.

### Supersede chains

When a fact *changes* (a job, a preference, a plan), you don't overwrite it — you
`supersede` it. The old node's `status` flips to `superseded`, the new node is
inserted, and a `supersedes` edge records the replacement. The chain is walkable,
so history is preserved rather than destroyed. (Auto-merge deliberately cannot do
this: it can't tell a correction from a restatement, which is why a contradicting
write should call `supersede`, not rely on merge.)

## 4. The read path — hybrid retrieval + graph reach

`search` runs **two legs** and fuses them (design/07 §Exact formulas):

1. **Vector leg** — embed the query (with the `search_query:` prefix — the
   asymmetric convention nomic-embed uses), take the top-30 by cosine.
2. **Lexical leg** — `websearch_to_tsquery` over the `search` tsvector, top-30 by
   `ts_rank_cd`.
3. **Reciprocal Rank Fusion** (`k = 60`), tie-broken by `created_at DESC` then
   `id ASC` — a total order, so results are deterministic.
4. **Read-time near-duplicate collapse**: nodes ≥ 0.95 cosine to a higher-ranked
   hit are dropped (the same "same fact restated" bar the write path merges at),
   **except** pairs joined by `same_topic`/`relates_to` — those were deliberately
   kept distinct, so collapsing them would undo merge-link.
5. Cut to `limit` (≤ 25).

```mermaid
flowchart TD
    Q[search: query, scope] --> QE[embed query - search_query prefix]
    QE --> VL[vector leg: top-30 cosine]
    Q --> LL[lexical leg: websearch_to_tsquery, top-30 ts_rank_cd]
    VL --> RRF[RRF fuse k=60, tie-break created_at then id]
    LL --> RRF
    RRF --> COL[collapse read-time near-dupes >=0.95, exempt same_topic/relates_to]
    COL --> LIM[cut to limit <= 25]
    LIM --> ENV[envelope: results[], scopes_searched, truncated]
    ENV --> PULL[caller may traverse edges / get full bodies on demand]
```

- **`traverse`** walks edges from a `start_id` (`out`/`in`/`both`, depth-capped,
  optionally filtered by `edge_types`). It is how graph structure reaches an
  answer — e.g. depth-1 from a `person` anchor lists every fact about them, and
  `edge_types=['same_topic']` from a hit lists its cluster siblings.
- **`get`** hydrates full node bodies plus capped edge summaries (≤ 10 out / ≤ 10
  in) for up to 25 ids.
- **`briefing`** returns pack-declared session-start sections (due commitments,
  relevant preferences, recent notes) — the "start of conversation" read.

The design intent is *reference, not content*: retrieval returns handles (ids) and
the caller pulls more on demand (`traverse` a hit's edges, `get` full bodies) rather
than every read dragging in bodies that compete for the reader's attention.

## 5. Universal searchability & the dual-index invariant

Two invariants make the graph trustworthy for retrieval:

- **I2 — Universal searchability.** A fact's content is searchable no matter where
  it is stored. Content-bearing attributes (strings, dates) enter the embedded
  document and the tsvector (weight C) via a stored `extra_search` column, so a
  fact recorded only in a typed attribute — `occupation: "paediatric nurse"` — is
  still found by a search for "nurse", not merely retrievable by `get`. Addenda
  (text folded onto a node during absorb) enter the tsvector at weight D.
- **I4 — Dual-index consistency.** Everything the *graph* side can reach, the
  *semantic* side has indexed, and vice-versa: every node a `traverse` walk reaches
  is embedded and lexically indexed, so you can never walk to a fact that search
  cannot also find. A CI test pins this so the two indices cannot drift.

The searchable surface of a node is a single function —
`searchable_text(title, body, extra)` — used identically on the write and rebuild
paths, so what gets embedded and what gets lexically indexed can never disagree.

## 6. Capture inbox

Low-friction capture that hasn't been curated yet lands in the **inbox** (`POST
/inbox`, or `inbox_review` over MCP). Items are `list`ed, then `promote`d into real
nodes (through the same dedup write path) or `discard`ed. This separates "grab this
now" from "commit it to structured memory".

## 7. The restore sentinel

Every space gets one engine-owned, archived `engram_sentinel` node at creation. It
never surfaces in search, briefing, or dedup — its only job is to let
`engraphy-admin verify-restore` prove a backup restored correctly by asserting the
sentinel's presence. You will see it mentioned in `space create` output; you never
interact with it.

---

Next: [Setup & install](02-setup.md) to bring this up locally, or
[Build your own pack](03-packs.md) to define your own node/edge ontology.
