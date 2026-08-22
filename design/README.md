# Engraphy — A Memory Engine for Agents

Design documents for **Engraphy**: a standalone, self-hosted memory engine for AI agents — typed knowledge graph, embedding-native deduplication, hybrid retrieval, real graph traversal, multi-principal isolation, and schema enforcement at the data layer. It is modelled on human associative memory: memories are typed, linked, deduplicated on meaning, and recalled by relevance rather than by filename or timestamp. It was built as a standalone engine because a genuinely good memory system for AI agents is worth building properly, and the reference implementations available (notably `@modelcontextprotocol/server-memory`) don't solve the problems that matter.

**Status:** Living document set — design complete; repo scaffolded (stubs + golden starter fixtures + packs); implementation phases E0+ ready for handoff
**Last updated:** July 2026
**Scope:** The core engine — storage, schema machinery, retrieval, dedup, API, tenancy, operations, roadmap
**Out of scope:** Any specific agent's memory *semantics*. An opinionated type system (Error→Pattern→Decision→Check etc.) lives in a **pack** — see the example pack shipped with the repo; Engraphy is the machine it runs on.

---

## The name

**Engraphy** (n.): the hypothesized physical trace a memory leaves in neural tissue — the term coined by Richard Semon alongside *mneme* and *engram*. It says exactly what the product is: a durable memory substrate, modelled on how human memory actually stores and recalls. It's short, greppable, and brandable. Considered and passed over: *Mneme* (too obscure), *Anamnesis* (nobody can spell it), *Trace* (ungoogleable).

Naming inside the product: one server hosts **spaces** (hard-isolated trust boundaries — a person, a household, or a team); a space contains **principals** (its members) and **scopes** (contexts like projects, each with an owner and a visibility level); scopes contain **nodes** and **edges** whose types come from a **pack** (a versioned, declarative schema the space installs). Nodes are colloquially "memories"; we resist calling them engraphys — the product is the Engraphy, singular.

## Why this exists — the reference-server critique, answered structurally

Every row is a design commitment traceable to a document here:

| Reference `server-memory` failure | Engraphy's structural answer | Doc |
|---|---|---|
| Local-only stdio subprocess, one machine, one account | Streamable HTTP MCP server; per-device bearer tokens; multi-space tenancy **and in-space teams** — phones, desktops, different humans, and shared team memory on one server, local or cloud | [03](03-api-auth-and-tenancy.md), [06](06-teams-and-sharing.md) |
| Flat JSON file, no concurrency protection, silent corruption | Postgres 16, MVCC, every tool call one transaction; concurrent-writer torture test in CI | [01](01-core-data-model.md) |
| No dedup at the data layer; model does manual search-and-judge | Embedding on every node (server-side, local model); three-band write path: auto-merge / forced-choice handshake / clean insert. The model judges only when judgment is needed, with evidence presented | [02](02-retrieval-and-dedup.md) |
| Flat text search — misses paraphrases, false-matches on shared words | Hybrid retrieval: pgvector cosine + Postgres FTS, fused with RRF — both failure directions covered by the opposite leg | [02](02-retrieval-and-dedup.md) |
| "Graph" with no traversal: `read_graph` dumps everything; multi-hop = model chains `open_nodes` calls | Recursive-CTE `traverse` (one call, depth ≤ 4, cycle-safe, hard caps); **no dump-everything endpoint exists** | [02](02-retrieval-and-dedup.md) |
| Naming-tag conventions (`MM:`, `AL:`) as a workaround for cross-area false matches | Scopes are first-class rows, filtered in SQL; six work areas = six scopes, isolation by query shape not by string prefix; one scope may be marked *ambient* (always co-searched) | [01](01-core-data-model.md) |
| Nothing enforces schema; model must follow SKILL.md perfectly forever | Registry-driven enforcement **in Postgres triggers**: unknown type, illegal edge, missing/invalid attribute → the INSERT fails, regardless of model behavior | [01](01-core-data-model.md) |
| Manual capture; depends on a human running recap prompts consistently | Inbox staging (automatic dumb capture endpoint + deliberate promotion), resonance report on every write, dedup makes re-capture harmless; scheduled-recap guidance for hook-less clients | [02](02-retrieval-and-dedup.md), [04](04-operations-and-governance.md) |
| Backfilling old chats is unsupported and risky | Bulk import pipeline: batched writes through the same dedup machinery — duplicates absorb instead of accumulate, so backfill is idempotent | [02](02-retrieval-and-dedup.md) |
| Depends on a vendor's phone↔desktop conversation sync (observed to fail) | **No dependency on vendor thread sync, ever.** Continuity lives in Engraphy; any device with a token gets the same memory | [03](03-api-auth-and-tenancy.md) |
| Community reference implementation, no governance | First-party product discipline: semver, plain-SQL migrations with a boot version gate, pack versioning, CI test matrix, release checklist | [04](04-operations-and-governance.md) |

## The layering: engine vs pack

The engine ships **mechanisms**; a pack ships **opinions**:

```
A pack (per space, declarative)
  node/edge types · edge rules · attr specs · briefing spec · tool aliases
  + a client's own session integration (hooks, protocol)
  ---------------------------------------------------------------
Engraphy engine (this repo)
  spaces · scopes · registry-driven schema enforcement
  embeddings · dedup · hybrid search · traversal · inbox
  audit · tokens · MCP server
  + packs/starter (shipped example pack — the plain default)
```

The boundary rule, stated once: **Engraphy ships mechanisms; packs ship opinions.** If a feature requires knowing *what* a type means, it belongs in a pack. If it works for any type system (dedup, search, traversal, tenancy, audit), it belongs in core. Packs are **declarative data, not code** in v1 ([01](01-core-data-model.md)) — a deliberate constraint that keeps the core testable and packs reviewable.

One deployment, multiple humans: space `nova` runs a rich custom pack; a second person's lightweight assistant runs in space `alex` with the starter pack — same server, same Postgres, hard isolation ([03](03-api-auth-and-tenancy.md)). A space can also be a **team**: multiple principals sharing one graph under scope-level visibility — private personal memory, team-readable professional memory, shared read-write project contexts ([06](06-teams-and-sharing.md)). Deployable locally (a self-hosted, tailnet-only posture) or on a cloud VM for teams ([04](04-operations-and-governance.md)).

## Document index

| Doc | Contents | Key decisions |
|-----|----------|---------------|
| [01-core-data-model.md](01-core-data-model.md) | Spaces, scopes, registry tables, node/edge DDL, the attr-spec language, trigger enforcement, RLS | Registries-as-data replace enums (multi-space schemas without DDL); a small declarative attr-spec instead of JSON Schema; RLS on as defense-in-depth |
| [02-retrieval-and-dedup.md](02-retrieval-and-dedup.md) | Hybrid search, briefing engine, traversal, dedup bands, resonance reports, bulk import | Briefing is core but pack-driven (declarative sections); every write returns a resonance report; import = the dedup pipeline in batch |
| [03-api-auth-and-tenancy.md](03-api-auth-and-tenancy.md) | MCP tool surface, tokens/roles, isolation model, rate limits, audit | MCP-first (REST deferred); a token *is* the (space, principal) identity — cross-space requests are inexpressible; instance admin local-CLI-only, day-to-day team admin via role-gated tools |
| [04-operations-and-governance.md](04-operations-and-governance.md) | Deployment profiles (local/overlay + cloud), backups, migrations (core + pack), versioning, release discipline | One instance per trust community, run anywhere — Docker/compose for cloud, TLS refusal on plaintext public binds; a managed multi-instance SaaS platform stays a non-goal; pack upgrades validate existing rows or quarantine them |
| [05-roadmap.md](05-roadmap.md) | Build phases with acceptance criteria, including first-deployment adoption, the second-space proof, and the team pilot | Multi-space and sharing are each proven by real humans, not test fixtures |
| [06-teams-and-sharing.md](06-teams-and-sharing.md) | Principals, scope visibility (private / team-read / team-write), grants, personal scopes, edge/dedup privacy rules, space administration | Sharing lives *inside* a space (spaces stay hard walls); visibility on scopes not nodes; edges visible only when both endpoints are readable; per-reader ambience; 2–25 members honestly bounded |
| [07-implementation-contracts.md](07-implementation-contracts.md) | The determinism kit: exact formulas, canonical wire shapes, error codes, pack JSON Schema, golden-fixture mandate, deviation protocol | Normative — wins over prose in 01–06; fixtures are written before code; implementers record deviations rather than inventing silently |
| [08-ontology-and-interop.md](08-ontology-and-interop.md) *(proposal — not yet locked)* | RDF/Turtle export, pack→OWL+SHACL compilation, `maps_to` vocabulary alignment | RDF as projection, never as engine; SHACL (not OWL) is the standards analog of pack enforcement; export is knowledge-complete/index-lossy; admin-CLI only |
| [09-benchmark-harness.md](09-benchmark-harness.md) | Benchmark harness: corpus IR, ingest adapter, retrieval strategies, judging, the three metrics, the custom duplicate-stream benchmark, neutrality safeguards | Lives in a top-level `bench/` package so the engine's no-LLM rule keeps no exceptions; a shared core with two thin shim boundaries (`Corpus`, `Scorer`); ingest goes through the real dedup pipeline, never `import_mode`; two extractors are always run and the gap between them *is* the adapter's measured contribution; temporal is measured, not chased |
| [implementation/](implementation/) (3 plans) | Deep plans for the three concentrated-risk components: attr-spec interpreter, visibility/RLS, dedup write path | Normative at 07's level: exact algorithms, authoritative skeletons, enumerated trap lists (each trap fixture-covered), per-component build order |

## Standalone by construction

This repo is buildable, testable, and deployable on its own. Every normative input — the formulas, wire shapes, SQL, the pack schema, the golden fixtures, and a self-contained example pack (`engraphy/tests/fixtures/packs/example-pack.yaml`) — is committed here. Build a pack, mint tokens, point a client at an MCP URL, and you have a running memory engine.
