# Retrieval completeness: engine specification

Status: proposed, 2026-09-19. Branch `feat/entity-complete-retrieval`. Not merged.
Measurement and decision rules:
`engraphy-benchmarks` `analysis/2026-09-19-retrieval-completeness-preregistration.md`
and `analysis/2026-09-19-retrieval-completeness-findings.md`.

Two additions, specified separately because they stand on different ground. The
entity roster is an ordinary read-path feature over data the engine already
holds. The source-turn backstop needs the engine to hold something it was
designed not to hold, so it is a decision before it is a feature.

## 1. Entity roster (search option)

### Problem

Hybrid search ranks memories by similarity to a query's wording. A set or
synthesis question ("where has Melanie camped", "what has Evan painted") needs
memories whose wording is unlike the question's, so they fall below the result
cap even when stored. Widening the search does not reach them: both legs are
capped at 30 before fusion, and a 50-wide search completed the evidence for 2 of
12 such multi-hop failures where the roster completed 6.

### Wire

`search` gains two optional arguments, validated in `wire_types.py` and published
in the tool's `inputSchema`:

| argument | type | default | meaning |
|---|---|---|---|
| `entity_roster` | bool | false | add the roster described below |
| `roster_limit` | int, 1 to 100 | 100 | cap on roster entries |

The response envelope gains two keys, present only when `entity_roster` is true.
Both are additive; envelope `v` stays 1 and existing clients are unaffected.

- `entities`: the registry names the query mentions, in match order.
- `entity_roster`: summary node envelopes (`id`, `type`, `scope`, `title`,
  `attrs`, `status`, `author`, `created_at`, plus `similarity`), never `body`.

The default stays false until the staged ten-conversation run measures it (section
4). Flipping the default is a separate, recorded decision.

### Algorithm

Inside the read transaction `search` already opens, after fusion:

1. **Registry.** Titles of active `person` and `thing` nodes in the resolved scope
   set. A name is the title up to its first `,` or `(`, trimmed, at least two
   characters, starting with an upper-case letter. Proper nouns only, so a thing
   titled "pottery class" never turns a query about pottery into a roster query.
2. **Match.** A name matches when it occurs in the query as a whole word,
   case-sensitively.
3. **Membership.** Active nodes in the resolved scope set, excluding `person` and
   `thing` nodes, excluding the ids already in `results`, whose title or body
   names any matched entity. **Text only: no edge is read.** Graph traversal was
   measured as a net loss on multi-hop, and an `involves` edge is only as
   complete as the extractor that wrote it.
4. **Order and cap.** By cosine distance to the query embedding already computed
   for the vector leg, ties by id; cut at `roster_limit`.

The prototype matched membership with `~*` and `\m...\M` word boundaries, a
sequential scan. The shipped query should use the indexed `search` tsvector
(`search @@ phraseto_tsquery('simple', name)` over a `simple`-config expression
index, so names are not stemmed). That swap must be proven equivalent: a test
asserting identical member sets to the regex on a fixture store, and a re-run of
`bench.completeness_recall` on the definitive store with identical coverage.

### Semantics

- RLS: runs in the same transaction as the legs, so an unreadable scope
  contributes neither registry names nor roster entries.
- Recall statistics: roster entries are **not** bumped. The caller was shown a
  title, not the memory; `bump_recall` stays over `results` only.
- Scope `all`: covered by the existing audit row; `detail` of that row gains
  `roster: n`.
- Near-duplicate collapse: not applied to the roster in the prototype. Titles are
  short, and collapse is calibrated on full-document embeddings. Leave off and
  note it.
- Cost: two indexed queries per call with the flag on. Latency to be measured on
  the fixture store and on a 10k-node store before the default can change.

### Tests

Registry parsing; whole-word, case-sensitive matching; membership by text with no
edge rows present; results excluded; cap; deterministic order; an unreadable
scope's person node never yields a name and its memories never appear; no
`body` key in roster entries; `bump_recall` untouched by roster ids; the flag off
returns a byte-identical envelope to today's.

### Skill

`skills/retrieval.md` should say when to ask for the roster: a question about
everything known of a named person or thing. `skills/answer-discipline.md` is not
touched here; how the reader uses a roster title, including when to answer and
when to decline, belongs to the reader workstream.

## 2. Source-turn backstop (decision required first)

### Problem

Typed extraction keeps what the extractor judges durable. On the definitive run,
34 of 58 single-hop failures and 12 of 37 multi-hop failures had evidence that no
stored memory quotes: small details ("a rainbow sidewalk", "put a GPS sensor on
his keys") that were said once and never extracted. No read-path change can
retrieve a fact that was never written.

### Why this is a decision, not a feature

`design/01-core-data-model.md` lists storing transcripts as a non-goal: nodes
hold distilled knowledge. A turn layer reverses that. It also changes what
Engraphy holds about a user from what an agent chose to remember to everything
that was said, which has retention, export and deletion consequences.

### Option A: an episode layer

- Table `episodes (space_id, scope_id, id, session_id, turn_index, speaker, text,
  occurred_at, embedding, search tsvector, created_at)`, RLS identical to
  `nodes`, HNSW and GIN indexes as on `nodes`.
- A write tool `record_turns(scope, session_id, turns[])`, batched, embedding
  outside the transaction as the node write path does.
- A `search` option `source_turns: 0..10`: hybrid over `episodes` in the same
  scope set with the node legs' caps and RRF k, skipping turns quoted verbatim in
  a returned memory body; returned as `source_turns` in the envelope.
- Per-space opt-in (`config` key `episodes.enabled`), a retention setting, and
  inclusion in export, backup and scope deletion.
- The ingest pipeline and any client that already has the transcript write
  turns; an agent that only writes distilled memories is unaffected.

### Option B: extract more

Change the extractor so small, specific details become their own memories.
Stays inside the data model. Unmeasured here, costs a full re-ingest to measure,
and every extractor prompt change carries the risk of being shaped by the
benchmark it is measured on.

### Recommendation

Decide A or B on product grounds before any code. The measured value of A is in
the findings document; B has no measurement yet.

## 3. Harness (this branch)

`bench/core/completeness.py` and `bench/completeness_recall.py` are the
prototype and the LLM-free instrument; `render_envelope` renders the two new
sections. When the engine option lands, the harness strategy should call
`search(entity_roster=true)` rather than its own SQL, so the benchmark measures
the shipped path.

## 4. Composition with the staged run

The staged ten-conversation, three-run configuration (`config/locomo-next.json`,
engine PR #27) measures `search_only:k=25` and must stay exactly that as its
primary arm. The roster composes as a second arm over the same ingested store
(arms share ingest, so the added cost is reading and judging only), once the
engine option exists on a commit that also carries PR #27:

- arm `llm-conversational:search_only:k=25:roster`, arm id
  `llm-conversational/search_only/always_distinct/k25/roster`;
- decision rules fixed before launch, as in the preregistration;
- the source-turn arm only if option A is chosen and built.

Nothing from this branch should enter that run as harness-only code, because a
harness-only retrieval is a configuration no deployment can reproduce.
