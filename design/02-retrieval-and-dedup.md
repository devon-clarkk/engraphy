# Engraphy — Retrieval, Deduplication, and Ingestion

The behavioral heart of the engine: how memory is found (hybrid search, pack-driven briefings, traversal), how duplicates are prevented at write time (bands, resonance, the handshake), and how memory gets in (writes, the inbox, bulk import/backfill).

**Status:** Living document
**Last updated:** July 2026
**Scope:** Search, briefing engine, traversal, dedup write path, resonance reports, inbox, bulk import
**Out of scope:** Tool/auth plumbing ([03](03-api-auth-and-tenancy.md)), storage shapes ([01](01-core-data-model.md))

> **Design note:** The embedding decision's *principles* are local in-process, server-side only, privacy-driven; rejected API embeddings/Ollama sidecar/client-side. **Revised July 2026 — default model upgraded:** `nomic-embed-text-v1.5` (137M, Apache-2.0, Matryoshka-trained) **truncated + re-normalized to 384 dims** — modern retrieval quality, same `vector(384)` column, no DDL change; `bge-small-en-v1.5` remains the supported fallback for constrained hardware. Dedup thresholds are per-(space, model) config and must be recalibrated via `dedup_log` sampling on any model change; the re-embed playbook ([04](04-operations-and-governance.md)) makes the default swappable forever. New here: the declarative briefing engine, resonance-on-every-write, bulk import, and the performance/SOTA sections. **Revised July 2026 (rerank slot):** the deferred cross-encoder slot is now fully specified (model tiers, importance-flag invocation, dedup exclusion) in [Search](#the-rerank-slot-designed-july-2026-adoption-still-trigger-gated); adoption trigger unchanged.

---

## Table of contents

1. [Goals](#goals)
2. [Search](#search)
3. [The briefing engine](#the-briefing-engine)
4. [Traversal](#traversal)
5. [Deduplication write path](#deduplication-write-path)
6. [Resonance reports](#resonance-reports)
7. [The inbox](#the-inbox)
8. [Bulk import and backfill](#bulk-import-and-backfill)
9. [Performance model and budgets](#performance-model-and-budgets)
10. [State-of-the-art audit](#state-of-the-art-audit)
11. [File reference](#file-reference)
12. [Testing and validation](#testing-and-validation)
13. [Acceptance criteria](#acceptance-criteria)
14. [Deferred work](#deferred-work)

---

## Goals

### Primary

- Both retrieval failure directions of flat text search — paraphrase misses and shared-word false hits — structurally covered (semantic leg catches the first, lexical leg the second, fusion arbitrates).
- Dedup so reliable that **re-telling the engine something it knows is free**: absorbed, acknowledged, and useful (reinforcement), never a second row.
- Multi-hop questions answered in one call with hard output caps — the context window is a budget the server respects.
- Session-start context ("what should I know right now?") as one pack-configurable call.

### Non-goals

- LLM calls inside the engine (carried over: Engraphy embeds, never generates — judgment belongs to the calling agent).
- Relevance learning/decay in v1 (`recall_count`/`last_recalled_at` are collected from day one; ranking upgrades are data-driven later).
- Perfect dedup. The bands minimize both error directions and *log every encounter* so thresholds improve with evidence; the escape hatch (forced choice) exists precisely because 0.80–0.95 cannot be automated honestly.

---

## Search

Now space/pack-generic:

1. **Vector leg:** embed query → top-30 cosine over `status='active'`, filtered by space (RLS + explicit), scope set, optional type filter.
2. **Lexical leg:** `websearch_to_tsquery` → top-30 by `ts_rank_cd`, same filters.
3. **RRF fusion** (`k=60`), return `limit` (≤ 25).
4. **Scope set:** requested scope ∪ the requesting principal's *readable* ambient scopes; `all` = every scope the principal can read ([06](06-teams-and-sharing.md)), explicit only. In a team space this is the cross-member query: `all` naturally spans teammates' `team-read` scopes.

Governance values (`dedup.*`, `resonance.floor`, `briefing.semantic_floor`) are per-space `config`-read on every call ([07](07-implementation-contracts.md)'s config-read contract); search's own constants (`k=60`, top-30 per leg, cap 25) are deliberately **fixed in v1** — no consumer needs per-space search tuning yet (Devon, 2026-07-16), and the lookup becomes config-read additively when one does. Recall stats batched on every read: one synchronous UPDATE over exactly the **surfaced** result ids (a truncated candidate was not read), shared by search/get/traverse/briefing via one helper. There is deliberately **no unfiltered dump endpoint** — the `read_graph`-shaped hole in the reference server is a hole, not a feature; the closest legal query is `search` with `all` + a type filter, still capped.

### The rerank slot (designed July 2026, adoption still trigger-gated)

The cross-encoder slot is now fully specified so adoption is a config change + model download, not a design exercise:

- **Position:** after RRF fusion over the top-30 union, before `limit` truncation. The reranker scores `(query, title + "\n" + body)` pairs; final order is reranker score descending, RRF score as tiebreak. It re-orders candidates only — never adds or removes any, so caps, scope filtering, and visibility are untouched.
- **Invocation — importance flag, not always-on:** `search` gains `rerank?: bool`, defaulting from per-space config `rerank.default` (ships `false`). Rationale: on baseline-class CPU-only hardware, always-on reranking multiplies search p95 for every trivial lookup, while the *calling model* knows which lookups are load-bearing — the tool description says so plainly ("set `rerank: true` when precision matters more than ~100–300ms"). Deployments with headroom — Apple-silicon (MPS/CoreML) or GPU hosts — flip `rerank.default: true` for always-on. A third mode (auto-rerank when the vector and lexical legs disagree — low top-k overlap is a cheap ambiguity signal) is deferred until `dedup_log`-style evidence exists to tune it.
- **Never in the dedup path.** Dedup bands (`t_high`/`t_low`) are calibrated on embedding cosine similarity; a cross-encoder emits scores on a different scale, and injecting them would silently invalidate band calibration. The reranker is a read-side precision tool only.
- **Models** (per-instance, one loaded model, same policy as embeddings; all Apache-2.0, local, ONNX INT8 export):
  - **Default (CPU-class hosts): `jina-reranker-v1-turbo-en`** — 38M params, 6-layer distilled cross-encoder, ~92–95% of base-class quality on BEIR; tens of ms for 30 short memory-node pairs on CPU. Beats size-peer MiniLM (`ms-marco-MiniLM-L-6-v2`) at the same speed class.
  - **Middle: `bge-reranker-base`** (278M) — the standard CPU-only recommendation where a few hundred ms is acceptable.
  - **Quality (GPU-class hosts): `bge-reranker-v2-m3`** (568M, multilingual) — the current open-weight consensus default; on plain CPU it costs seconds for top-30 and is therefore not the shipped default. Apple-silicon hosts sit in between: unified memory + MPS runs the base or v2-m3 tiers at interactive latency, making `rerank.default: true` a reasonable posture there.
  - License note for the product: Jina's *v2* reranker is CC-BY-NC — not usable; v1 turbo/tiny and all BGE rerankers are Apache-2.0.
- **Config keys:** `rerank.enabled`, `rerank.model`, `rerank.default`. When enabled, the budget table gains a row: `search (rerank: true)` p50 < 250ms / p95 < 600ms on CPU-class with the default model, enforced by `bench.py` like everything else.

**Adoption trigger unchanged:** precision complaints in real use, or any space > 20k active nodes. Until then the slot ships dark — the `rerank` parameter is added to the wire contract at adoption time (it is *not* in [07](07-implementation-contracts.md) yet, deliberately, to keep the v1 fixture surface minimal).

## The briefing engine

The reference-server world has no answer to "what should the agent know at session start" except *dump everything*. Engraphy's answer is a core engine driven by **pack-declared sections**:

```yaml
# in pack.yaml
briefing:
  sections:
    - {name: standing_decisions, type: decision, status: active,
       include_linked: {edge: verified_by, direction: out}}   # pull each decision's checks
    - {name: due_commitments, type: commitment,
       where_attr: {key: next_due, before: "+3d"}, order_by_attr: next_due}
    - {name: active_goals, type: goal, where_attr: {key: status, equals: active}}
    - {name: relevant, semantic: true, types: [pattern, preference], top_k: 5}
    - {name: open_errors, type: error, recent: 14d, without_edge: {edge: derived_from, direction: in}}
  footer: {inbox_pending_count: true}
```

`briefing(scope, hint)` executes the sections (the `semantic: true` section runs hybrid search over `hint`, subject to the relevance floor below), returns them named and ordered. Section grammar is small and closed, like the attr-spec: type+status filters, one attr predicate, one edge-presence/absence predicate, linked-node inclusion, semantic top-k, recency. The example pack's briefing reproduces a full `memory_briefing` exactly — that was the proof the grammar suffices; the starter pack's briefing is three sections (due commitments, preferences, recent notes).

**The semantic-section relevance floor** (July 2026 revision; Devon's call at the E1 briefing gate, design by Fable). A briefing *pushes* unsolicited context; `search` *answers* a question the caller asked. That asymmetry means briefing — unlike search — must itself judge what is worth an agent's context window: a pure top-N vector leg returns the N nearest nodes *whatever their distance*, so at small candidate counts a `semantic: true` section would pad itself with off-topic memories, and a padded section is strictly worse than a short one (the context window is a budget the server respects — [Goals](#goals)). Rule: the section drops vector-leg candidates whose query↔document similarity is **below `briefing.semantic_floor`** (per-space config key, default `0.50`; `>=` survives, the bands' own boundary convention) *before* RRF fusion; lexical-leg hits are never floor-dropped — the hint's own words occurring in a node is an affirmative relevance signal, and exact-identifier recall is precisely what the lexical leg exists to catch. Net effect, stated once: **a node surfaces in a semantic section only if it clears the cosine floor or lexically matches the hint.** A section this empties is returned empty (`nodes: []`) — by design, not as a degenerate case. Plain `search` is deliberately **not** floored: its caller asked, sees `score`/`similarity?` in the envelope, and judges relevance itself, and its shipped acceptance behavior does not change. Mechanically the floor is an optional parameter on the one shared hybrid implementation (briefing sets it; search never does), so the two paths still cannot drift on leg SQL or fusion arithmetic. The default is calibrated the same way the dedup bands were: fixture-measured against the pinned model (query↔document cosines pinned ±0.02); if the default misbehaves against the real distribution, that is a QUESTIONS.md entry, never a nudge. Per-*section* floor overrides in the pack grammar are deliberately deferred (the grammar stays closed) until a pack proves the need.

This keeps the engine mechanism-only (it doesn't know what a "decision" is) while giving every pack a real session-start story — the strongest single differentiator against the reference server.

## Traversal

The traversal (recursive CTE, path-tracked cycle guard, `direction ∈ out|in|both`, `max_depth ≤ 4`, `limit ≤ 50`, hydration resolves merge chains, depth annotations) adds three generalizations: edges are walked only when **both endpoints are readable** by the requesting principal ([06](06-teams-and-sharing.md)); `bidirectional` edge types (from the registry) are always walked both ways, and all queries are space-pinned. The SQL shape, inlined here (July 2026) so this repo is self-sufficient:

```sql
WITH RECURSIVE walk AS (
  SELECT e.src_id, e.dst_id, e.type, 1 AS depth,
         ARRAY[e.src_id, e.dst_id] AS path
  FROM edges e
  WHERE e.space_id = %(space)s
    AND e.src_id = %(start)s AND e.type = ANY(%(edge_types)s)
  UNION ALL
  SELECT e.src_id, e.dst_id, e.type, w.depth + 1, w.path || e.dst_id
  FROM edges e JOIN walk w ON e.src_id = w.dst_id
  WHERE e.space_id = %(space)s
    AND w.depth < %(max_depth)s
    AND NOT e.dst_id = ANY(w.path)            -- cycle guard
    AND e.type = ANY(%(edge_types)s)
)
SELECT * FROM walk LIMIT %(limit)s;
```

`direction='in'` swaps the join column; `'both'` runs a UNION of the two shapes (each still path-guarded); registry-`bidirectional` types are walked both ways regardless of `direction`. The walk is totally ordered before its `LIMIT` — `ORDER BY depth, src_id, dst_id, type` (an unordered `LIMIT` is nondeterministic, and the envelope is wire surface; same posture as RRF's tie-breaks). `limit` caps **walk rows**; node order in the envelope is `(depth, id)`. Nodes are hydrated in a second query (summary envelopes by default — [07](07-implementation-contracts.md)); merged nodes resolve to canonical (two walk ids resolving to one canonical dedupe to the minimum depth); per-node `depth` = minimum hops from start. The both-endpoints readability rule falls out of the edges RLS policy composing with the node policies ([implementation/visibility-and-rls-plan.md](implementation/visibility-and-rls-plan.md)) — the CTE needs no visibility logic of its own.

## Deduplication write path

The core write pipeline — restated here as the product's canonical spec since this is the flagship behavior:

```
every write: embed(title + "\n" + body)
  → top-5 same-type candidates, scope set (writer-READABLE scopes only — 06),
    status='active', SAME SPACE ONLY
  → best similarity s (thresholds per space, defaults for bge-small):
       s ≥ 0.95        AUTO-MERGE   absorb: addendum if novel (Jaccard < 0.8),
                                    links re-pointed, explicit merge report
       0.80 ≤ s < 0.95 PENDING      no row created; payload parked (24h TTL);
                                    caller must resolve_duplicate(distinct|merge)
                                    with candidates in hand; 'distinct' auto-adds
                                    an edge of the LITERAL type `relates_to` to the
                                    nearest candidate (when the pack defines one;
                                    skipped gracefully otherwise), and a still-mid-
                                    band re-check collapses to INSERT — only a fresh
                                    ≥ t_high hit overrides the human's call
       s < 0.80        INSERT       clean insert
  → dedup_log row for every encounter (the threshold-tuning dataset)
```

Merge mechanics, canonical-chain resolution on all reads, and threshold governance (config-table values, reviewed against `dedup_log` outcomes after ~3 months of a space's use) carry over unchanged. The critique's "nothing at the data layer prevents duplicate entities" is answered at the only layer that can do it honestly: the write path, with the model consulted **only** in the band where automation would be a lie.

**What auto-merge cannot see: contradictions (measured 2026-07-21, first `dupstream` run — ruling in DECISIONS-DELTA).** AUTO-MERGE's premise is that ≥ `t_high` means *the same fact restated*. Cosine similarity is symmetric — it measures aboutness, not agreement — so a contradiction, which is lexically near-identical to the fact it overturns ("Priya *no longer* works as a paediatric nurse"), typically scores **above** `t_high` and is absorbed into the very node it contradicts: 28 of 36 contradiction pairs across three seeds in the first measured run. The confirm band never fires for these writes — they score above it, on a premise negation breaks. Consequences, stated exactly: the new information survives at best as an addendum (`get`-only on the wire, never re-embedded — search and briefing keep returning the old body as the current fact); the contradicted node stays `active` and canonical; and when the wording is near-verbatim (Jaccard ≥ 0.8 against the canonical-plus-addenda corpus) the incoming body is **dropped entirely**, reported only as `addendum_added: false`.

The engine does not attempt to detect this, deliberately: agreement-vs-negation is a judgment call, judgment belongs to the calling agent (this doc's own non-goals), and rule 4 forbids LLM calls in the engine — while every non-LLM signal considered was rejected as brittle or worse (see the 2026-07-21 DECISIONS-DELTA entry). The engine's obligations are instead: **(1) visibility** — the `merged` envelope carries the full canonical node, `addendum_added`, and (July 2026 revision) an `instruction` telling the caller what to check; **(2) an atomic repair verb** — `supersede(old_id=canonical.id, …)` makes the correction current and flips the stale fact, and works exactly as well after an absorbing merge as before it; **(3) governance** — a contradiction-sensitive space can raise `dedup.t_high` (per-space config) to push near-verbatim negations down into the confirm band, paying more confirmations for every true restatement; that is a per-space, `dedup_log`-informed decision, not a default change. The caller contract, one sentence: **on `outcome: "merged"`, compare your write against the canonical body — if it contradicts or updates rather than restates it, call `supersede`; the merge stored your text as an addendum at most, never as the current fact.**

Import mode is the sharp edge of this: AUTO-MERGE absorbs silently and nobody is in the loop, so a contradiction-heavy backfill loses its corrections unless the export carries supersede intent (the bench harness's `LLMExtractor` emits it; a raw dump does not). `ImportSummary` reports `merged_addendum_dropped` so total-loss absorption is at least countable, and the review CSV covers only the confirm band — it cannot catch this by design.

## Resonance reports

New here, generalizing the `memory_log_error` recurrence report:

**Every successful write returns a resonance report**: the top-3 similar existing nodes (writer-readable scopes, any type, ≥ 0.75) with their one-hop link summaries — the readability bound means a report can never leak a teammate's private memory ([06](06-teams-and-sharing.md)). Cost: the dedup query already ran; widening it to any-type is one more index scan.

Why this is core: the agent that just wrote "deploy failed because the migration wasn't run" *immediately sees* the six-month-old error it rhymes with and its downstream decision — recurrence detection, cross-referencing, and "you already know something about this" fall out of one mechanism, for every pack. A pack alias (e.g. the example pack's `log_error`, [03](03-api-auth-and-tenancy.md)) is just `write` with a preset type whose resonance report the protocol tells the agent to read carefully.

## The inbox

Carried over structurally (automatic dumb capture ≠ memory; deliberate promotion runs the full dedup pipeline; 14-day nag surfaced in briefing footers; discarded rows purged after 30 days). Generalized: `POST /inbox` accepts `{kind, payload, scope?}` per space token; *what* captures is the client's business (a hook-capable harness uses harness hooks; a Desktop-only user has no hooks — see the recap pattern in [04](04-operations-and-governance.md)).

Semantics pinned at the 2026-07-17 inbox gate (Devon): **promotion is authoring, not forwarding** — the reviewer supplies every write field (`type`, `scope`, `title`, `body`, `attrs`, `links?`); the captured `kind`/`payload` are opaque metadata, never parsed and never the source of the node type — structure is added at promote time, which is the whole point of the staging queue. The **footer nag counts only pending rows older than 14 days** (it is a nag about aged backlog, not a live capture counter — a fresh capture must not inflate it). Listing is capped (default 25) and **oldest-first** — triage drains the top of the backlog. The 30-day purge of discarded rows is the operator's scheduled job, like the pending-writes TTL sweep — no in-engine sweeper ships in v1, and nothing relies on either sweep for correctness.

## Bulk import and backfill

The critique's "backfilling old chats" problem, made a first-class feature — because dedup makes backfill *safe*:

- `engraphy-admin import --space X --scope Y file.jsonl` — lines of `{type, title, body, attrs, links?}` (the agent, or a one-off script, produces this from exported conversations; extraction quality is the exporter's problem, idempotency is Engraphy's).
- Runs the standard write pipeline in batches with two import-mode changes: PENDING-band items are **not** parked for interactive resolution — they're written to a `review_queue` report (CSV: incoming, candidate, similarity) for a human/agent pass afterwards; AUTO-MERGE absorbs silently. So an import can never spray near-duplicates, and re-running the same import is a no-op (everything hits ≥ 0.95).
- Resonance reports are **not computed** during import (July 2026 revision): resonance is a write-time report for an interactive caller, and import discards its envelopes by construction — computing a per-item read-only report nobody reads would roughly double per-item DB cost for zero information. Write-side state — node, edges, `dedup_log`, `audit_log` — remains byte-identical to a normal write; import mode still only ever suppresses reports, never mechanics.
- Rate: embeddings dominate; ~50ms/item ≈ 70k items/hour on modest hardware — no batch-API complexity needed at personal scale.
- Import is admin-CLI only, not an MCP tool (large payloads, human-supervised by nature, and it must never be reachable through an agent's token).

## Performance model and budgets

The honest comparison against the reference `server-memory`, stated so nobody optimizes the wrong thing:

**Raw per-call latency is not where the win is.** A flat-JSON substring scan over a few thousand entities in a warm stdio subprocess costs ~1ms; nothing networked beats that number, and we don't try. The win is measured at the **session level**, in three currencies:

1. **Token economy.** `read_graph` on a 2,000-entity graph dumps tens of thousands of tokens into the model's context: seconds of ingest latency per turn, real cost, degraded reasoning. Engraphy's worst legal read returns ≤ 25 capped nodes. Fewer, more relevant tokens is the dominant "speedup" — it shows up as faster *model turns*, not faster queries.
2. **Retrieval quality.** A paraphrase miss costs an entire re-derivation of something already known (minutes); a false shared-word match costs a wrong premise. Hybrid+RRF buying both directions for ~30ms of server work is the best latency trade in the system.
3. **Call count.** One `briefing` replaces ~5 searches; resonance rides on `write` for free; traversal is one call, not N `open_nodes` chains. Session protocols make ~2–4 memory calls per session, not dozens.

**Server-side budgets** (enforced by a benchmark script in CI against a 10k-node seeded space; p50/p95 on **baseline-class hardware** — a CPU-only ~4-core small VM or decade-old desktop, the deliberately pessimistic product floor; any modern host, Apple-silicon Mac, or GPU box beats it comfortably):

| Operation | p50 | p95 | Dominant cost |
|-----------|-----|-----|---------------|
| `search` | < 175ms | < 300ms | Query embedding (~20–80ms CPU) — the HNSW top-30 at ≤100k rows is 1–5ms and FTS 1–10ms; fusion is arithmetic |
| `briefing` | < 300ms | < 700ms | N section queries + one embedding (sections run concurrently) |
| `write` (incl. dedup + resonance) | < 250ms | < 600ms | One embedding + two index scans + transaction |
| `traverse` (depth 4) | < 50ms | < 150ms | Indexed CTE |

*`search` p50 recalibrated 120 → 175ms (2026-07-19; evidence and rationale in DECISIONS-DELTA.md). `bench.py`'s first enforcing CI runs — on GitHub-hosted runners, which are exactly the "baseline-class" CPU-only floor this table targets — measured `search` p50 at 121–138ms across runs (p95 150–175ms, always well under the 300ms budget), with the whole runner ~1.2–3× slower on a loaded attempt. The embedding-dominated `search` op sits right at the original 120ms floor on shared runners with variable load, so its p50 budget was raised to clear the observed ceiling with headroom; p95 (300ms) is unchanged and remains the hard tail bound. The other three ops passed with large margins. Calibrated on limited data — tighten toward the true runner p50 as more CI runs accrue.*

**Transport overhead, quantified:** Streamable HTTP with connection keep-alive costs one RTT per call. Tailnet direct on LAN: 1–5ms. Tailnet direct remote (phone on cellular): 20–80ms; DERP-relayed fallback: 50–150ms. Cloud VM: 20–100ms. Worst realistic case ≈ 150ms per call — against LLM turn times of 1–5s and 2–4 calls per session, transport is **< 5% of perceived latency**. Conclusion: HTTP costs nothing that matters and buys every-device/every-account access; the design spends its latency budget where it pays (embedding quality), not on shaving RTTs.

Implementation notes that keep the budgets honest: HTTP keep-alive mandatory in shipped client configs; the embedding model loaded once at boot (no per-call load); briefing sections run **sequentially in pack order inside one transaction** as shipped (one snapshot for sections, footer, recall, and audit; the envelope's section order is the output contract either way — concurrency on the pool remains available as a budget lever if the <2s @ 5k gate ever needs it); the benchmark script (`engraphy/tests/bench.py`) runs in CI and fails the build on budget regression.

## State-of-the-art audit

Where Engraphy sits against the current graph/vector-memory landscape — adopted, deliberately deferred with triggers, or deliberately skipped. "Cutting edge" here means the techniques with demonstrated retrieval wins, not novelty for its own sake:

| Technique | Position |
|-----------|----------|
| Hybrid dense+lexical with RRF | **Adopted** — current consensus best practice for this scale; tuning-free by design |
| HNSW ANN (pgvector) | **Adopted.** DiskANN-class indexes (pgvectorscale/StreamingDiskANN) deferred — their wins materialize beyond ~500k vectors; trigger: any space's active nodes exceed 200k |
| Matryoshka embeddings | **Adopted** (July 2026 revision above): modern MRL model truncated to 384 — near-full-dim quality at small-index cost, and a free future knob (retruncate higher without re-choosing models) |
| Vector quantization (halfvec/binary) | Skipped — meaningless below ~100k vectors; revisit with the DiskANN trigger |
| Cross-encoder reranking (over the fused top-30 → top-k) | **Deferred with a fully-designed slot** — see [The rerank slot](#the-rerank-slot-designed-july-2026-adoption-still-trigger-gated): model tiers chosen (jina-v1-turbo default on CPU, bge-reranker-v2-m3 on GPU), importance-flag invocation (`rerank: true`), dedup path explicitly excluded. Trigger unchanged: precision complaints in real use, or any space > 20k nodes |
| Write-time semantic dedup with forced-choice handshake | **Ahead of the field** — most published agent-memory systems dedup never, or lossily at read time. This plus registry-enforced typing is Engraphy's actual differentiation |
| Typed, schema-enforced graph | **Ahead** — GraphRAG/Zep-class systems use LLM-extracted, untyped or loosely-typed graphs; enforcement-at-write is rare and is what makes the graph *queryable by contract* |
| Bi-temporal knowledge (Graphiti/Zep-style valid-from/valid-to, edge invalidation) | **Partially covered, consciously**: `status` transitions, `supersedes` chains, addenda timestamps, and never-delete give "what did I believe and when" for the cases a personal agent hits. Full bi-temporality (query-as-of-date) deferred — real modeling cost, no consumer yet; trigger: a pack genuinely needs as-of queries |
| GraphRAG-style community detection + hierarchical summaries | Deferred — its value is corpus-level QA over huge graphs; at personal/team scale, `briefing` + scoped search cover the need. Revisit only if spaces reach ~50k+ nodes *and* corpus-level questions become common. Also: summaries require generation, and the engine's no-LLM rule ([Goals](#goals)) means this would live in a client-side ritual, not the engine |
| Self-editing agentic memory loops (MemGPT/Letta-class) | Deliberately out of engine scope — that's the *protocol* layer (a client's session protocol). The engine provides the primitives (resonance, handshake, inbox) that make such loops safe |

Net position, stated plainly: Engraphy runs the 2025–26 consensus retrieval stack executed properly, is genuinely ahead on write-path integrity (dedup + schema enforcement), and defers the big-corpus techniques with explicit numeric triggers instead of pretending a personal memory needs them today.

## File reference

| File | Contents |
|------|----------|
| `engraphy/core/search.py` | Hybrid search + RRF |
| `engraphy/core/briefing.py` | Section grammar interpreter |
| `engraphy/core/traverse.py` | Recursive CTE wrapper |
| `engraphy/core/dedup.py` | Bands, merge mechanics, pending/resonance |
| `engraphy/core/embedding.py` | bge-small in-process |
| `engraphy/admin/import_.py` | Bulk import + review_queue report |
| `engraphy/tests/test_{search,briefing,traverse,dedup,import}.py` | Below |

## Testing and validation

The full test suite (band behavior at synthetic similarities, recurrence, hybrid-search both-directions cases, traversal caps/cycles, supersede atomicity, inbox lifecycle, instructive error strings), plus:

| New test | Assert |
|----------|--------|
| Briefing grammar | Every section construct against fixtures; the example pack's briefing byte-compares to its committed expected fixture (`fixtures/briefing/`) — the fixture is committed here, so the check runs standalone |
| Resonance | Any-type report on plain writes; ≥ 0.75 floor respected; one-hop summaries correct |
| Dedup space isolation | Identical text in two spaces: **no** cross-space candidate ever appears |
| Dedup visibility isolation | Identical text in two members' private scopes: no co-candidacy; in a shared scope: normal handshake, second writer gets it ([06](06-teams-and-sharing.md)) |
| Import idempotency | Import file twice → second run creates zero rows, all absorbed |
| Import review queue | Seeded 0.85-similarity pairs land in the CSV, not the DB |

## Acceptance criteria

- [ ] Search: paraphrase fixture found by vector leg alone; shared-word decoy fixture ranked below true match after fusion (both critique failure-directions demonstrated fixed).
- [ ] Briefing under both shipped packs returns correct sections in < 2s at 5k nodes.
- [ ] Dedup band tests + space-isolation test pass; a deliberate double-write produces one node and an explicit merge report.
- [ ] A 1,000-item synthetic backfill imports idempotently with a correct review queue.
- [ ] Zero dump-everything code path exists (grep + API-surface review).
- [ ] `bench.py` budgets green in CI at 10k seeded nodes; the transport-overhead numbers spot-verified once from a phone on cellular.

## Deferred work

Relevance ranking beyond RRF (recency/recall_count boosts — data first); MCP-exposed guided-import for small batches; per-scope thresholds (per-space has not yet proven insufficient); consolidation-assist queries (stale decisions, fact-growth reports) as briefing-grammar extensions.
