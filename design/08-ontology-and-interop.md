# Engraphy — Ontology and Interop (OWL / RDF / SHACL)

What a W3C-standards layer buys Engraphy, what it would cost, and the shape of the answer: **RDF as a projection, SHACL as the exported contract, OWL as documentation — never as the engine.**

**Status:** Proposal — investigation record, July 2026. Not yet locked; nothing here changes docs 01–07. Decision records below are recommendations awaiting Devon's sign-off.
**Scope:** RDF/Turtle export, pack→OWL+SHACL compilation, vocabulary alignment annotations
**Out of scope:** Storage engine (01 — unchanged), retrieval (02 — unchanged), any network-facing surface (03 — this layer is admin-CLI only)

---

## Why investigate this at all

Engraphy's ambition is a standalone product. Two product-level pressures point at the semantic-web stack:

1. **Lock-in is the first objection a serious adopter raises.** "My agent's memory lives in your Postgres schema" is a worse pitch than "your memory exports losslessly to W3C-standard Turtle that Protégé, Oxigraph, or any triple store can read."
2. **Packs are ontologies in the plain sense** — named classes, typed relations with legal src/dst combinations, attribute constraints. The semantic-web world spent twenty years standardizing exactly this vocabulary. Ignoring it entirely means reinventing serializations nobody else can read; adopting it wholesale means inheriting semantics that fight the engine's core promise.

The investigation's job is to find the line between those two.

## Decision record: OWL is not a validation language — SHACL is the analog of packs

The pivotal technical fact, stated once so nobody re-litigates it: **OWL operates under the open-world assumption with no unique-name assumption. It is an inference system, not a schema enforcer.**

Concretely, against Engraphy's flagship behaviors:

| Engraphy behavior (pack + trigger) | What an OWL reasoner does with the same situation |
|---|---|
| Missing required attr → INSERT rejected, key named | Nothing is wrong: the value exists but is unknown (open world) |
| Edge `derived_from: pattern → person` violates edge_rules → rejected | `rdfs:range` axiom *infers* the person is also an error — no rejection, a new "fact" |
| `closed: true` — unknown attr key rejected | No such concept; anyone may assert any property |
| Two near-identical nodes → dedup handshake | No unique-name assumption: two URIs may silently denote one thing, or not |

So "adopt OWL for schema enforcement" is a category error. The W3C stack's own answer to closed-world validation is **SHACL** (Shapes Constraint Language) — and Engraphy's registries + attr-spec + triggers already *are* a minimal SHACL-class validator, with a deliberately smaller grammar that a plpgsql trigger can interpret (01: "the subset is the product").

**Decision: the engine's enforcement stack (registries, attr-spec, triggers, FKs) is not replaced or augmented by any RDF technology. Instead, every pack compiles mechanically to a SHACL shapes file + an OWL ontology, so the *outside world* can validate and understand Engraphy exports with standard tools.** The pack remains the single source of truth; the TTL artifacts are generated, never hand-edited.

## Decision record: projection, not migration

Considered and rejected: making Engraphy RDF-native (Postgres → Jena/Fuseki, Oxigraph, GraphDB, or Postgres-backed triple tables).

- The locked stack (01) carries the product: pgvector hybrid search + RRF, RLS as isolation backstop, the dedup write path as one transaction. Triple stores have no mature equivalent of any of the three; rebuilding them there is a rewrite with negative feature delta.
- Open-world semantics at the core would fight enforcement-at-write — the differentiator the SOTA audit (02) says Engraphy is *ahead* on.
- Personal/team scale (≤ ~100k nodes) gets nothing from triple-store strengths (federated SPARQL, billion-triple inference).

Also rejected: **Turtle as the pack authoring format.** Pack authors are Devon and weaker implementing models; YAML validated by one JSON Schema beats Turtle + OWL parsing for that audience, and OWL's expressivity would mostly have to be *refused* at validation to preserve the attr-spec ceiling. TTL is an output format, not an input format, in v1.

## The mapping

### Instance data → RDF (TriG/Turtle)

- **Node URI:** `urn:uuid:<id>` — standard, honest (no fake resolvable domain).
- **Class:** pack namespace, capitalized type — `pk:Error` where `@prefix pk: <https://engraphy.dev/pack/example/1#>`. Pack name + version in the namespace: type semantics are pack-versioned.
- **Core properties** (`@prefix eg: <https://engraphy.dev/ns/1#>` — a small, versioned vocabulary shipped as `engraphy/ontology/engraphy.ttl`): `rdfs:label` = title; `eg:body`; `eg:status`; `eg:scope`; `eg:mergedInto` (canonical chain); `eg:recallCount`.
- **Attrs → datatype properties** in the pack namespace: `pk:severity "high"`, `pk:happened_at "2026-07-06"^^xsd:date`.
- **Edges → object properties:** `pk:derived_from`; `bidirectional` types are declared `owl:SymmetricProperty` in the generated ontology.
- **Provenance → PROV-O** (the one external vocabulary adopted wholesale): `prov:wasAttributedTo` (author principal), `prov:generatedAtTime`, `eg:sourceClient`, `eg:sourceSession`; supersession → `prov:wasRevisionOf` on the replacement node.
- **Scopes → named graphs:** default export is **TriG**, one named graph per scope, graph-level triples carrying visibility/ambience/owner. `--format ttl` flattens to one graph with `eg:scope` per node. Named-graph-per-scope means a partial export (one scope) is just a subset of graphs, and visibility survives the projection.
- **Embeddings are not exported.** They are model-specific index state, meaningless outside the instance, and regenerable on import. Exports are *knowledge-complete, index-lossy* — stated in the file header comment.

### Pack → OWL + SHACL

`engraphy-admin pack export-ontology pack.yaml -o pack.ttl` emits, deterministically:

- Node type → `owl:Class` (+ `rdfs:comment` from description).
- Edge type → `owl:ObjectProperty`; `edge_rules` → `rdfs:domain`/`rdfs:range` as `owl:unionOf` over the rule rows — emitted as *documentation axioms*, with the honest caveat above that OWL treats them as inference licenses; the enforcing versions live in the SHACL shapes.
- Attr-spec → SHACL `sh:NodeShape` per type: `sh:datatype` (string/int/number/bool/date→`xsd:*`), `sh:in` for enums, `sh:minCount 1` for required, `sh:maxLength 2000` for strings, `sh:closed true` + `sh:ignoredProperties` (the core `eg:` properties) for `closed`. The one conditional form compiles to `sh:or` (¬condition ∨ key-present) — expressible in core SHACL, no `sh:sparql` needed.
- Edge rules → `sh:property` shapes with `sh:class` on the object, per src type.

The example and starter packs are the proof cases, same as they were for the pack format itself (01).

### Vocabulary alignment: `maps_to`

Optional, additive pack.yaml key on node and edge types:

```yaml
node_types:
  person:
    description: "…"
    maps_to: "foaf:Person"     # emits pk:Person rdfs:subClassOf foaf:Person
edge_types:
  involves:
    maps_to: "schema:about"    # emits rdfs:subPropertyOf
```

`rdfs:subClassOf`/`rdfs:subPropertyOf`, deliberately **not** `owl:equivalentClass` — subsumption is a safe claim, equivalence is a strong one. Allowed prefixes are a closed list (`schema:`, `foaf:`, `prov:`, `skos:`, `dcterms:`) validated by `pack validate`; requires one additive change to `packs/schema.json`. Zero engine behavior attaches to `maps_to` — it exists only in generated TTL, which is exactly the boundary rule (mechanisms in core, opinions in packs; interop annotations are opinions *about* opinions).

## Surface

Everything here is **admin CLI only**, mirroring import's rationale (03: large payloads, human-supervised, never reachable through an agent's token):

| Command | Does |
|---|---|
| `engraphy-admin export --space X [--scope Y] [--format trig\|ttl]` | Instance data projection, per the mapping above |
| `engraphy-admin pack export-ontology <pack.yaml \| --space X> -o out.ttl` | OWL + SHACL from a pack file or an installed pack |

No new server code path. No SPARQL endpoint (see deferred — it is `read_graph` wearing a standards costume unless capped into uselessness).

Round-trip note: import *from* RDF is deferred, but the export format is designed so a trivial TTL→JSONL script feeds the existing import pipeline — and dedup already makes any reimport idempotent (02). That property is what makes export more than an escape hatch: it's a migration and backup format with a re-entry door.

## Testing and validation

House rules apply — fixtures first, golden and byte-exact:

| Test | Assert |
|---|---|
| Pack→TTL golden fixtures | Starter and example packs compile to committed `pack.ttl` files, byte-exact (deterministic ordering: types alphabetical, prefixes fixed) |
| Shapes are real | In CI: a seeded space exports; **pyshacl** validates the export against the generated shapes → conforms. A deliberately corrupted export (missing required attr, illegal edge) → violation naming the same key the trigger would name |
| Round-trip idempotency | Export → TTL→JSONL script → import into a fresh space → zero review-queue rows beyond expected, node/edge counts equal |
| External-tool smoke | Generated ontology opens in Protégé without profile errors; export loads into Oxigraph and a hand-written SPARQL query returns the Error→Pattern→Decision chain (manual, once per format change) |
| Isolation | Export honors `--scope`; a scope-restricted export contains zero triples from other scopes (including in edge subjects/objects) |

## Acceptance criteria

- [ ] Both shipped packs compile to OWL+SHACL; pyshacl conformance green in CI.
- [ ] 1k-node seeded space exports as TriG and TTL; loads clean into Oxigraph.
- [ ] Corrupted-export SHACL test proves the shapes enforce what the triggers enforce.
- [ ] `maps_to` schema change is additive: every pre-existing pack file still validates.
- [ ] Grep + surface review: no RDF code path reachable from the MCP server process.

## Deferred work

| Item | Trigger |
|---|---|
| RDF/TTL **import** | A real external dataset someone wants to load (until then: TTL→JSONL + existing import) |
| Read-only SPARQL endpoint | A real non-MCP consumer with a query need `search`+`traverse` can't express; must arrive with caps + audit story, or not at all |
| Type hierarchies in packs (`parent:` on node_types) | A real pack needs subsumption (e.g. querying `event` to get `error`s). Note: the answer would be one registry column + scope-set-style expansion in queries — *not* an OWL reasoner |
| `owl:equivalentClass` alignment | Someone demonstrates a consumer that needs equivalence and accepts its inferential consequences |
