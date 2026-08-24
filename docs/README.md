# Engraphy — Developer Documentation

**Engraphy** is a self-hosted memory engine for AI agents: a typed knowledge graph
on Postgres + [pgvector](https://github.com/pgvector/pgvector), with
embedding-native deduplication, hybrid (semantic + lexical) retrieval, real graph
traversal, multi-principal isolation enforced in the database, and a schema
("pack") system that lets one engine serve many differently-shaped memory
applications.

It is built to replace the flat-JSON / stdio / single-user reference MCP memory
server with something that survives concurrency, distance, duplicates, and years.

> **Naming.** The product and the engine are both **Engraphy**, and so is every
> identifier. The Python package, CLI, config environment variables, and MCP
> server name are `engraphy` (`import engraphy`, `engraphy-admin`,
> `ENGRAPHY_DATABASE_URL`, `Server("engraphy")`), and so are the database-level
> names: the `engraphy` database, the `engraphy_app` role, the `engraphy_*()` SQL
> functions, and the `engraphy.*` session settings. Nothing is named `engram` any
> more. A deployment provisioned before migration `0024` carries the old names and
> is upgraded by it; see `COMPATIBILITY.md` for that one-time operator sequence.
> This documentation uses *Engraphy* for the product and `engraphy` when naming
> the code you type.

## What Engraphy gives you

- **A typed memory graph.** Memories are typed **nodes** (a `fact`, an `event`, a
  `person`, a `decision`, …) connected by typed **edges** (`involves`,
  `references`, `same_topic`, `supersedes`, …). Types, their attribute schemas,
  and the edge rules between them are declared per space in a **pack**.
- **Writes that dedup themselves.** Every write is embedded and banded against
  existing memory: a near-verbatim restatement auto-merges, a genuinely new but
  related fact is *merge-linked* (kept as its own searchable node, joined by a
  `same_topic` edge — nothing is silently absorbed), and a borderline case parks
  as a pending duplicate-check verdict for the caller to resolve.
- **Hybrid retrieval.** `search` fuses a vector leg (cosine over embeddings) and
  a lexical leg (Postgres full-text) with Reciprocal Rank Fusion, then walks
  edges with `traverse`. Attribute content is embedded into the searchable
  surface, so a fact stored only in a typed attribute is still findable.
- **Multi-space, multi-principal isolation** enforced by Postgres Row-Level
  Security — not by application checks that can be forgotten.
- **An operator CLI and an MCP tool surface** for everything from bootstrapping a
  space to minting tokens, importing data, and verifying restores.

## Read in this order

| # | Doc | For |
|---|-----|-----|
| 1 | [Architecture overview](01-architecture.md) | Understanding the memory model, the write path, the read path, and the core invariants. |
| 2 | [Setup & install](02-setup.md) | Getting Postgres + pgvector up, running migrations, and serving the MCP endpoint locally end-to-end. |
| 3 | [Build your own pack](03-packs.md) | Declaring node types, edge types, attribute schemas, the `searchable` attr flag, dedup thresholds, and applying a pack. Includes a complete worked example. |
| 4 | [Tool / API reference](04-tools-reference.md) | Every MCP tool a developer calls, with parameters, returns, and a realistic example each. |
| 5 | [Deployment guide](05-deployment.md) | Running Engraphy as a service, auth/scopes, backups, and the admin CLI. |
| 6 | [End-to-end tutorial](06-tutorial.md) | Build a real app on Engraphy: ingest data through the dedup pipeline, then query it. |

## Requirements at a glance

- **Postgres 16** with the **pgvector** extension (the `pgvector/pgvector:pg16`
  image ships both).
- **Python ≥ 3.12**.
- **[dbmate](https://github.com/amacneil/dbmate)** on `PATH` for migrations.
- The embedding model **`nomic-ai/nomic-embed-text-v1.5`** (384-dim), running
  int8-quantized on ONNX Runtime (~131 MB) and baked into the image. Two other
  profiles are selectable with `ENGRAPHY_EMBEDDING_PROFILE`: `onnx-fp32`, whose
  vectors are interchangeable with int8's predecessor, and `legacy-torch`.

See [Setup](02-setup.md) for the exact steps.
