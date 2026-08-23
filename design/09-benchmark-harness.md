# Engraphy — Benchmark Harness

Engraphy has never been measured against anything. The market it enters — Mem0, Zep, Cognee, Letta — is measured almost entirely by two public suites (LoCoMo, LongMemEval) that assume a memory system is a box you feed a transcript and then interrogate. Engraphy is not that box: its write path is typed, scope-gated, and dedup-banded, and a confirm-band write is not durable until an agent resolves it. Nothing about that survives contact with "feed a transcript" without a deliberate shim. This document specifies that shim, the shared machinery beneath it, and — the harder half — the discipline that keeps the shim from becoming the thing being measured.

**Status:** Living document
**Last updated:** July 2026
**Scope:** The benchmark harness: corpus IR, ingest adapter, retrieval strategies, answer extraction, LLM-as-judge scoring, the three metrics (accuracy, tokens, latency), the custom duplicate-stream benchmark, module layout, and neutrality safeguards.
**Out of scope:** Any change to `engraphy/` engine behavior. The harness is a *consumer* of the engine and may not modify it, tune it, or ship a code path that a real deployment would not run. BEAM is deferred (see [Deferred work](#deferred-work)).
**Audience:** The implementing model, and any reader auditing a published number.

---

## Table of contents

1. [Goals](#goals)
2. [Decision record: the harness lives outside the engine package](#decision-record-the-harness-lives-outside-the-engine-package)
3. [The shared core / shim boundary](#the-shared-core--shim-boundary)
4. [Data flow](#data-flow)
5. [Ingest: conversational turns into a typed, dedup-gated write path](#ingest-conversational-turns-into-a-typed-dedup-gated-write-path)
6. [Retrieval: a question into search, briefing, or traverse](#retrieval-a-question-into-search-briefing-or-traverse)
7. [Answer extraction and judging](#answer-extraction-and-judging)
8. [Metrics: accuracy, tokens, latency](#metrics-accuracy-tokens-latency)
9. [The duplicate-stream benchmark (`dupstream`)](#the-duplicate-stream-benchmark-dupstream)
10. [Isolation: spaces, scopes, and the bench pack](#isolation-spaces-scopes-and-the-bench-pack)
11. [Neutrality safeguards](#neutrality-safeguards)
12. [Module and directory layout](#module-and-directory-layout)
13. [Acceptance criteria](#acceptance-criteria)
14. [Open questions](#open-questions)
15. [Deferred work](#deferred-work)

---

## Goals

### Primary

- **Produce a defensible number against the public suites** — LoCoMo first (1,540 questions, multi-session dialogue, and the only public suite whose multi-hop category rewards graph traversal), then LongMemEval (500 questions, six categories, the more carefully constructed set, and the only one whose knowledge-update category maps onto supersede chains).
- **Measure all three metrics on every run** — accuracy (binary LLM-as-judge, per category), **tokens returned into the agent's context per answer**, and latency (end-to-end and per-stage). Token and latency are first-class results, not a footnote. Accuracy-per-token is reported as its own figure.
- **Measure the differentiators the public suites do not reward** — dedup quality, via a custom duplicate-heavy ingest benchmark that is ours to define.
- **Make the adapter auditable.** Every published number carries a manifest that states exactly what the adapter did.

### Secondary

- Bound and *report* adapter-introduced variance rather than pretending it is zero.
- Give the engine a repeatable end-to-end workload at a scale the unit tests never reach.

### Non-goals

- **Winning the temporal categories.** Engraphy is not temporal-first; supersede yields current-vs-superseded, not point-in-time query. See [Decision record: temporal is measured, not chased](#decision-record-temporal-is-measured-not-chased).
- **A tuned score.** Any knob that would not ship in a default deployment is out of bounds, even when it would help. This is the whole ballgame — see [Neutrality safeguards](#neutrality-safeguards).
- Benchmarking competitors ourselves. We publish Engraphy's numbers and cite theirs; re-running four vendors' harnesses is a different project with a different failure mode.

---

## Decision record: the harness lives outside the engine package

**The harness is a top-level `bench/` package, not `engraphy/bench/`, and `engraphy/**` may never import it.**

`IMPLEMENTER.md` rule 4 is *"No LLM calls in the engine. No dump-everything code path. Both are grep-audited."* The harness needs three LLM roles (extractor, reader, judge) and is, by construction, a thing that pulls a lot of content out of the store. Both of those are correct for a benchmark and forbidden in the engine. Putting the harness inside the engine package would put an `anthropic` import inside the audited tree and force the rule to grow an exception; a rule with an exception is a rule that erodes.

`bench/` imports `engraphy.core.*` as a library — the same functions the MCP dispatchers call. The dependency arrow points one way only, and a new CI guard (`scripts/check_engine_does_not_import_bench.py`) proves it.

**Rejected:**
- `engraphy/bench/` — puts LLM clients in the audited tree; see above.
- A separate repository — the harness must move in lockstep with engine behavior (band thresholds, envelope shapes, `_node_envelope` stripping `addenda`). A cross-repo pin would be stale within a phase, and the manifest's `engine_git_sha` would stop meaning anything.
- Reusing `engraphy/tests/bench.py` — that is the latency-budget CI gate over a synthetic 10k-node corpus. It shares the `time.perf_counter` idiom and nothing else. Keeping them separate preserves the CI gate's job of failing fast on a regression.

Dependencies land in a new `[project.optional-dependencies] bench` extra, so `pip install -e '.[dev]'` remains exactly what it is today and the engine's runtime dependency list does not grow.

---

## Decision record: in-process against `engraphy.core`, with an HTTP validation subset

**The harness calls `engraphy.core` functions in-process. It does not drive the MCP server for full runs.**

In-process is the *real* pipeline, not a bypass: the same `dedup.write` with the same advisory lock, the same band arithmetic, the same attr-spec trigger, and the same RLS — because `db.transaction()` sets the `engraphy.space_id` / `engraphy.principal` GUCs identically no matter who calls it. The only things skipped are bearer resolution, rate limiting, and JSON serialization over the wire.

Pure over-HTTP would be marginally more faithful and is impractical: the shipped rate limits are 60 reads and 30 writes per minute per token (`engraphy/server/auth.py`), so a single 1,540-question LoCoMo run plus its ingest would take days. Raising the limits for the benchmark would itself be a non-shipping configuration.

**Therefore, both:** full runs go in-process, and a small `--transport=http` validation subset (default 50 questions) runs against a real server with limits raised, purely to prove the two paths agree. The manifest records which transport produced the numbers. If the subset's accuracy diverges from the in-process run on the same questions, that is a harness bug and the run is void.

**Rejected:** in-process calls to `engraphy.server.tools.*` dispatchers instead of `engraphy.core.*` — tempting for envelope fidelity, but the dispatchers require an `AuthContext` and add nothing the core call doesn't already do. Instead the harness serializes the core's return value with the same `json.dumps` shape the server would emit, which is what token counting needs anyway (see [Token accounting](#token-accounting)).

---

## The shared core / shim boundary

This is the most important line in the architecture, so it is drawn with two explicit interfaces and nothing else crossing it.

**The rule:** a benchmark shim may *translate* and *score*. It may not ingest, retrieve, answer, count, or time. Every shim is a file that turns a vendor's JSON into `Corpus` and turns a graded result into that suite's reporting categories. If a shim ever needs to influence how a question is retrieved, that is a signal the shared core is missing a *declared strategy* — add the strategy to the core and select it in the run config, where it is visible to an auditor.

### Interface 1 — `Corpus` (loading boundary)

Every benchmark lowers to one intermediate representation. Defined in `bench/core/corpus.py`:

```python
@dataclass(frozen=True)
class Turn:
    speaker: str                 # normalized to "user" | "assistant" | a named participant
    text: str
    timestamp: str | None        # RFC-3339 if the suite provides one

@dataclass(frozen=True)
class Session:
    session_id: str
    timestamp: str | None
    turns: list[Turn]

@dataclass(frozen=True)
class Haystack:
    haystack_id: str             # -> one scope; the isolation unit
    sessions: list[Session]

@dataclass(frozen=True)
class Question:
    question_id: str
    haystack_id: str
    text: str
    category: str                # suite-native category, verbatim, never remapped
    gold_answer: str
    evidence: list[str]          # session/turn ids where the suite says the answer lives
    abstain_expected: bool = False   # LongMemEval's abstention questions

@dataclass(frozen=True)
class Corpus:
    name: str
    haystacks: list[Haystack]
    questions: list[Question]

class BenchmarkLoader(Protocol):
    name: str
    def load(self, path: Path) -> Corpus: ...
```

Category strings are carried through **verbatim**. The harness never renames a suite's category to something more flattering, and never merges two categories into one bucket.

### Interface 2 — `Scorer` (scoring boundary)

```python
class Scorer(Protocol):
    def grade(self, q: Question, answer: str, judge: Judge) -> Verdict: ...
```

The default `LLMJudgeScorer` in the shared core handles every suite. A shim overrides `grade` only where the suite's own protocol demands it — LongMemEval's abstention questions are graded on whether the system correctly declined, which is a different question from "is this answer right". `dupstream` overrides it entirely because it has synthetic ground truth and needs no judge at all.

### What lives where

| Concern | Shared core (`bench/core/`) | Shim (`bench/adapters/<suite>.py`) |
|---|---|---|
| Parse vendor JSON | — | **yes** |
| Turns → typed nodes | **yes** (extractors) | no |
| Write through dedup | **yes** | no |
| Confirm-band resolution | **yes** (declared policy) | no |
| Question → retrieval | **yes** (declared strategies) | no |
| Answer extraction | **yes** (one fixed prompt) | no |
| Judging | **yes** | override only for abstention / synthetic truth |
| Token + latency capture | **yes** | no |
| Category naming | pass-through | **yes** (verbatim from source) |
| Report assembly | **yes** | no |

Target ratio is roughly 80/20, and the shim files are expected to be under 150 lines each. **A shim that grows past ~250 lines is a design smell** and should be read as the shared core lacking a declared capability.

---

## Data flow

```
                     ┌─────────────── SHIM ──────────────┐
  raw suite JSON ──> │ locomo.py / longmemeval.py /      │ ──> Corpus
                     │ dupstream.py (generator)          │
                     └───────────────────────────────────┘
                                                              │
  ╔══════════════════════════ SHARED CORE ═══════════════════ ▼ ══════════╗
  ║                                                                       ║
  ║  INGEST (once per haystack, into its own scope)                       ║
  ║    Session ─> windowing ─> Extractor ─> [NodeDraft, EdgeDraft]        ║
  ║                              │                                        ║
  ║                              ├── VerbatimExtractor  (no LLM, floor)   ║
  ║                              └── LLMExtractor       (typed, headline) ║
  ║                                     │                                 ║
  ║    NodeDraft ─> embed_document() ─> dedup.write()  [REAL pipeline]    ║
  ║                 (outside txn)         │                               ║
  ║                                       ├─ inserted   ──> record        ║
  ║                                       ├─ merged     ──> record        ║
  ║                                       └─ needs_confirmation           ║
  ║                                             └─> ConfirmPolicy         ║
  ║                                                 └─> resolve_duplicate ║
  ║    supersede candidates ─> dedup.supersede()  [knowledge-update]      ║
  ║                                                                       ║
  ║  RETRIEVE (per question)                                              ║
  ║    Question ─> RetrievalStrategy ─> {search | briefing | traverse}    ║
  ║                                          │                            ║
  ║                                          └─> Retrieval(envelope, ...) ║
  ║                                                                       ║
  ║  ANSWER                                                               ║
  ║    Retrieval.envelope ─(json.dumps, the only context)─> Reader ─> str ║
  ║                                                                       ║
  ║  JUDGE                                                                ║
  ║    (question, gold, answer) ─> Judge ─> Verdict{correct: bool}        ║
  ║                                                                       ║
  ║  METER wraps every stage: tokens + perf_counter                       ║
  ╚═══════════════════════════════════════════════════════════════════════╝
                                        │
                       runs/<run_id>/{manifest.json, results.jsonl, report.md}
```

---

## Ingest: conversational turns into a typed, dedup-gated write path

This is where a benchmark harness usually cheats, so it is specified in the most detail.

### Windowing

Extraction operates on a **session** at a time, in chronological order, with the previous session's extracted node titles supplied as context (titles only — never bodies, and never the raw prior transcript). Sessions longer than the extractor's window are split into turn windows of 40 turns with a 4-turn overlap. Order matters and is preserved: knowledge-update questions are only answerable if the store saw the old fact before the new one.

### `Extractor` — the confound, made visible

```python
@dataclass(frozen=True)
class NodeDraft:
    local_id: str                # extractor-local; resolved to a real node id after write
    node_type: str
    title: str
    body: str
    attrs: dict
    supersedes_local_id: str | None = None   # or a resolved node id
    provenance: list[str] = field(default_factory=list)  # session/turn ids

@dataclass(frozen=True)
class EdgeDraft:
    src_local_id: str
    dst_local_id: str
    edge_type: str

class Extractor(Protocol):
    name: str
    def extract(self, window: ExtractWindow) -> ExtractResult: ...   # nodes + edges
```

Two implementations ship, and **both are run and both are reported**:

| | `VerbatimExtractor` | `LLMExtractor` |
|---|---|---|
| LLM used | none | Claude, one call per window |
| Node type | every turn → one `note` | chosen from the bench pack's registry |
| `attrs` | `{}` | filled per the type's `attr_spec` |
| Edges emitted | **none** | `involves` / `references` / `relates_to` |
| Supersede detected | never | yes, against prior-session titles |
| Role | the **floor** — what the store does with zero adapter judgment | the **headline** — comparable to how Mem0/Zep/Letta ingest |

The gap between the two rows *is* the adapter's contribution, stated numerically instead of argued about. This is the harness's answer to "are we measuring the memory or the adapter?": we measure both, and print the difference.

Note what the floor costs, because it is the point: `VerbatimExtractor` emits no edges, so `traverse` has nothing to walk and multi-hop accuracy under it is expected to be near the search-only baseline. That is not a bug in the floor — it is the demonstration that graph structure has to be written before it can be traversed.

The `LLMExtractor` is constrained hard: it is given the bench pack's node types and attr specs and asked to emit conformant drafts; its prompt is a committed, hashed file (`bench/prompts/extract.md`); it is **suite-agnostic** (the same prompt runs for LoCoMo, LongMemEval, and dupstream) and it never sees a question. Any extraction prompt that mentions a benchmark, a category, or a question format is a neutrality breach.

**Source-text retention (added 2026-07-22, `retain_source_text`, recorded in the manifest).** The first LoCoMo run found the verbatim floor *beating* typed extraction on single-hop (63% vs 57%): typed extraction paraphrases, so a node loses the speaker's exact words and an exact-wording lookup stops matching it. A node's search vector and embedding are built from `title + body`, so the fix is to keep the verbatim source turns **in the body**: the extractor cites the turn ids each memory rests on (`source_turn_ids`, resolved deterministically against the window — the model names the turns, the harness copies their exact text, nothing is re-summarised), and that text is appended under a `Source (verbatim):` marker. The node keeps its type, title, attrs and edges *and* becomes word-searchable. **Why this is not tuning to single-hop:** it is a general recall property — any exact-phrasing query benefits, it is uniform across every category, and it is what a real deployment wanting both structure and verbatim recall would store. It is motivated by the single-hop finding, which is disclosed, but the mechanism is general, not test-shaped. Guarded against the two ways it could cheat: it never appends text a turn did not contain (a cited id absent from the window resolves to nothing), and it skips a quote already present in the body, so it adds only words the typed body dropped rather than double-counting. It does **not** collapse the two arms into one — verified: the LLM node is a *single* typed node carrying a summary plus the quote, whereas the floor emits one raw untyped note per turn; the LLM store stays smaller (no node inflation), it just stops discarding the wording.

**Engine-interaction check (offline, 2026-07-22).** A larger body shifts the node's embedding, so before committing to this the effect on dedup and retrieval was measured against the real 111-node conv-26 store (appending real turns, no LLM, no full run). Embedding drift is modest — cosine(body, body+source) median 0.95, min 0.875. **Dedup banding is not degraded:** of the 200 most-similar node pairs, 199 stay in the same band and *zero* real merges (≥0.95) are broken, so source text does not silently split duplicates. Retrieval ranking does move (11 of 30 top-5 slots across six probe queries, one query fully reshuffled) — but that check appended *mismatched* turns as a stress test; real retention appends the node's own cited turns, which drift the embedding toward the node's own topic rather than randomly, so the live effect is smaller and can go either way. The top-k shift is a watch-item for the full run, not a blocker; the banding result is the reassurance that mattered.

### Writing

Per draft, in order:

1. `embedding.embed_document(title + "\n" + body)` — **outside** any transaction, matching `server/tools/write.py` and the AST guard.
2. `dedup.write(pool, space_id, principal, node_type, scope_id, title, body, attrs, vec, source_client="engraphy-bench", links=[...])`. No `import_mode`. No `thresholds` override. No `resonance_floor` override. The bands are the shipped 0.95 / 0.80.
3. Branch on `outcome`:
   - `inserted` / `merged` — record the outcome and the resolved id; map `local_id → node_id`.
   - `needs_confirmation` — hand the pending to the **ConfirmPolicy** (below), which calls `resolve_duplicate`. The write is not durable until this happens.
4. If the draft carries `supersedes_*`, resolve the target to a node id and call `dedup.supersede` instead of `write`. If the target cannot be resolved, downgrade to a plain write and **count it** — `supersede_target_unresolved` is a reported number, because it is the honest measure of how much of the knowledge-update story is extraction versus engine.

Edges are attached via `write`'s `links` argument where both endpoints already exist, and via a second `link()` pass for edges whose destination is written later in the same session.

### `ConfirmPolicy` — and why it is a product call

The confirm band (similarity in `[0.80, 0.95)`) parks the payload in `pending_writes` and returns `needs_confirmation`. **Nothing is stored until an agent resolves it.** A conversational corpus hits this band constantly — paraphrase is what conversation *is* — and the duplicate-stream benchmark is built to hit it deliberately.

```python
class ConfirmPolicy(Protocol):
    name: str
    def resolve(self, pending: PendingWrite) -> Literal["distinct", "merge"]: ...
```

The policy is a pluggable strategy, named in the manifest and swappable per run. **Two implementations ship** *(ruled July 2026, Devon — see `QUESTIONS.md` "bench-ingest")*:

| Policy | Behavior | Measurement effect |
|---|---|---|
| `AlwaysDistinct` | every pending → `distinct` | the **reproducible primary**: protects recall, store grows, in-band dedup never fires. Deterministic, no LLM, no cost. |
| `LLMAdjudicate` | one Claude call per pending, given the incoming payload and the candidates | the **realistic arm**: what a well-behaved agent actually does with a `needs_confirmation` envelope. Adds an LLM confound and a call per pending. |

**`AlwaysMerge` is not implemented — not as a primary, and not as a comparison arm.** *(Ruled July 2026, Devon.)* It is the one policy that can genuinely destroy facts (every paraphrase-band pair collapses, including pairs that are merely similar), and it is the most obviously self-flattering choice available: it would manufacture a dedup number by deleting the evidence that would contradict it. A benchmark that ships it invites exactly the "claimed vs observed" attack this document exists to pre-empt. Shipping the code at all would make it one config flag away from a published number, so it is not written.

Regardless of policy, **confirm-band hit-rate is a first-class reported number** on every run.

**Does the confirm band cost recall under `AlwaysDistinct`? Investigated 2026-07-22 — no, and the worry was overstated.** The concern was that a 95%-confirm-band ingest parks almost every write and stores nothing. That is what happens to a *bare* pending write, but the harness is not a bare client: `AlwaysDistinct` resolves every `needs_confirmation` in-line via `resolve_duplicate("distinct")`, which re-enters `_locked_core` with `collapse_pending_to_insert=True` and lands the node. Measured on the Opus run: of **1,378** pending writes, **1,375** resolved straight to `inserted` and **0** to `merged`; the only 3 that did not land were attr-validation `CheckViolation`s (a `date` attr that was not a date — an extraction fault, already counted, nothing to do with the band). So the confirm band drops **zero** unique facts under this policy. The reported 95% confirm-band *rate* is real and worth knowing — it is the round-trip an agent would have to make — but it is not lost recall, because the harness makes that round-trip. **No recall-repair change was implemented, because there was no recall handicap to repair;** implementing one would have manufactured a boost against a problem that does not exist, which is exactly the flattery this document forbids.

**What the surviving pairing measures.** `AlwaysDistinct` and `LLMAdjudicate` differ in exactly one thing: whether the uncertain zone is adjudicated or simply stored separately. So the comparison answers a genuinely useful question — **does adjudicating the confirm band beat just storing everything?** If `LLMAdjudicate` wins on store growth and token cost without losing accuracy, the confirm band earns its keep. If it does not, the band is costing agents a round trip for nothing, and that is a real finding about a shipped design. `report.py` builds this pairing as a named comparison rather than leaving it for a reader to assemble.

#### Measured result (dupstream, July 2026)

**`LLMAdjudicate` did not beat `AlwaysDistinct`.** Reported as measured, including the direction we did not expect.

| Policy | precision (cross-class) | precision (incl. contradictions) | recall | store nodes | contradictions merged | probe |
|---|---|---|---|---|---|---|
| `AlwaysDistinct` (3 seeds) | 0.996 | 0.897 | 0.996 | 25 | 10/12 | 1.00 |
| `LLMAdjudicate` (1 seed) | 0.977 | 0.866 | 1.000 | 23 | 11/12 | 1.00 |

Adjudication produced a *smaller* store (23 vs 25 nodes; 5.22× vs 4.62× reduction against the no-dedup control) — but bought part of that by **destroying one more contradiction**, and scored worse on both precision figures. Probe recall was identical at 1.00, so the extra merging did not improve retrievability; it only removed rows.

**What this does not support:** one seed and twelve adjudications, against three seeds for the primary. A two-node difference and one contradiction is well inside what should be called noise. This is a signal worth investigating, **not a result to publish**, and it is not grounds to retire the confirm band.

`AlwaysDistinct` therefore remains the reproducible primary: deterministic, free, no LLM confound, and on current evidence no worse.

**Rejected: `import_mode=True` as the bulk path.** It is the obvious-looking choice (it is literally the bulk-load flag) and it is wrong here. In import mode a confirm-band write goes to a review-queue CSV rather than being parked — the fact never lands in the store at all. That is a systematic recall loss concentrated on exactly the paraphrase-heavy, update-heavy questions the benchmarks score. Using it would understate Engraphy and misattribute the cause.

---

## Retrieval: a question into search, briefing, or traverse

```python
@dataclass
class Retrieval:
    envelope: dict          # exactly what a real agent would receive
    stages: list[StageTiming]
    strategy: str

class RetrievalStrategy(Protocol):
    name: str
    async def retrieve(self, q: Question, ctx: RunContext) -> Retrieval: ...
```

A strategy is **fixed for a whole run and named in the manifest.** It is never selected per question, per category, or per suite — that would be tuning to the answer key.

| Strategy | Composition | What it exists to measure |
|---|---|---|
| `SearchOnly` | `search(scope, query=q.text, limit=10, detail="full")` | the baseline every vendor reports |
| `SearchThenTraverse` | `search(limit=10, detail="full")` → for **every** seed hit, `traverse(start_id, direction="both", max_depth=2, detail="summary")` → merge | **multi-hop.** Traverse needs a seed id; search supplies it. **Width-matched to `SearchOnly`** — see below. |
| `BriefingThenSearch` | `briefing(scope, hint=q.text)` → `search` to fill remaining budget | **token efficiency.** See below. |

**`SearchThenTraverse` is width-matched to `SearchOnly` (revised 2026-07-22).** The first version seeded at `limit=5` and walked only the top 3 hits, while the baseline searched `limit=10`. So the multi-hop comparison confounded two things — "does the graph help?" and "does a narrower search hurt?" — and the first run's multi-hop *regression* was therefore uninterpretable: it could have been the missing five search results, not the graph. The seed search now uses the **same `limit` and `detail` as `SearchOnly`**, and every seed is walked, so the two arms differ in exactly one thing: whether the graph neighbours of the search hits are added on top. Confirmed on the real store — the width-matched walk adds 24–39 summary-detail neighbours a plain top-10 search never returned. Width-matching is a fairness control, the opposite of tuning; if the isolated graph contribution turns out to be zero or negative, that is a real and reportable finding about the product, not a reason to keep adjusting until it wins.

**The token-efficiency subtlety, stated plainly because it is easy to get wrong:** the relevance floor is a *briefing* property. `search()` applies no floor at all — it returns a fixed count at full detail. So the "relevance floor and capped sections win on tokens" claim is only observable through a briefing-based strategy. It is therefore included as a first-class strategy.

**What we must not do:** add a floor to `search()` to make the numbers better. That is precisely the non-shipping tuning the fairness controversy is about. If briefing wins on accuracy-per-token, that is a real result about a real shipped feature; if it loses, we report that.

`traverse` returns `summary` detail by default (no bodies), which is deliberate and kept: it is the engine's own answer to a 50-node walk of 8,000-character bodies, and overriding it for a better score would be tuning.

---

## Answer extraction and judging

**Reader.** One Claude call. Its entire context is: a fixed system prompt (`bench/prompts/read.md`, hashed into the manifest), the question, and `json.dumps(retrieval.envelope)` — **nothing else**. It has no access to the corpus, the gold answer, the category, or any prior question. It is instructed to answer only from the provided memory and to say exactly `INSUFFICIENT` when the memory does not contain the answer (which is what makes LongMemEval's abstention category scorable).

**Judge.** One call to a **different vendor's** model (Gemini free tier — see §Provider routing), binary. Given the question, the gold answer, and the system's answer; returns `{correct: bool, reason: str}`. It never sees the retrieval envelope, the strategy, or the extractor — it cannot be biased toward a configuration it can't observe. Judge prompt is `bench/prompts/judge.md`, hashed into the manifest.

A **judge-agreement calibration** runs before any published number: 100 stratified items are graded twice (self-consistency) and against a committed human-labeled subset. Judge instability above 3% is reported alongside the accuracy figure, because an accuracy delta smaller than judge noise is not a result.

### Provider routing, and why the judge is a rival's model

*(Added July 2026, during implementation.)* Engraphy has no budget for API spend, so every role runs on a free route. That constraint produced one genuine improvement and one honest degradation.

| Role | Provider | Model | Why |
|---|---|---|---|
| Extractor | Claude CLI (`claude -p`), on the operator's subscription | `claude-opus-4-8` | Decides what enters memory at all; a weak extractor caps every downstream number, so it gets the strongest model |
| Reader | Claude CLI, same route | `claude-opus-4-8` | Must be the same class of model an agent would really use |
| Adjudicator | Claude CLI, same route | `claude-opus-4-8` | Confirm-band policy; needs no separate vendor |
| Judge | Gemini free tier (default) | `gemini-flash-lite-latest` | **Cross-vendor by design** — see below |

**Model ids are pinned by full id, never by CLI alias** *(2026-07-22)*. Verified live: `--model opus` resolved to a *Haiku* id, because the CLI runs a second model for its own internal work and `_resolved_model` reports whichever emitted the most output tokens — for a short reply that is sometimes the internal model. `--model claude-opus-4-8` resolves to `claude-opus-4-8` exactly. Aliases are convenient and wrong here; the whole point of pinning is that the recorded model is the model that ran.

**The judge is deliberately a different vendor from the system under test.** A harness that grades Engraphy's answers with Anthropic's model invites the obvious objection that it marked its own homework, and no amount of prompt discipline answers that objection from the inside. A rival's model grading the results removes it structurally. That it is also free is a happy coincidence, not the reason — **this arrangement should be kept even if funding appears.**

**Run-level deviation, recorded (2026-07-22):** a run may route the judge through Claude Sonnet (`--judge claude`, `run.CLAUDE_JUDGE_MODEL = claude-sonnet-5`) to escape the Gemini free tier's per-model daily cap. This is same-vendor grading and it is a deviation from the rule above, permitted for an **internal baseline** because the judge's task is binary answer-matching against a supplied gold answer — low bias-risk, since it scores whether two short facts agree, not prose quality. The Sonnet judge is deliberately a lighter model than the Opus extractor/reader: the grade does not need Opus, and a lighter judge conserves plan headroom on the longest sequential pass. `ROLE_MODELS["judge"]` stays Gemini so the cross-vendor posture remains the default and the guard test keeps enforcing it; the deviation lives in the run config and is written into the manifest's `judge_neutrality`. **For any eventual published number, the cross-vendor judge is the posture to return to.**

**Never introduce `ANTHROPIC_API_KEY`.** The CLI subprocess is launched with that variable actively *stripped*, not merely unset: if it reaches the CLI, Claude Code authenticates by API key and bills credits instead of the subscription — the precise outcome the routing exists to avoid. `GEMINI_API_KEY` is read from the repo's gitignored `.env` **by absolute path**, never from the process environment, and a miss names the exact file read (there are two copies of this repo on the operator's machine; a key saved into the stale mirror is otherwise indistinguishable from a key never saved).

**Gemini free-tier constraints, verified live rather than read:** ~10 requests/minute, Flash and Flash-Lite only (Pro left the free tier in April 2026). Quota is **per model** — `gemini-2.0-flash` returned a real 429 while `gemini-flash-latest` served the identical request. The client rate-limits with backoff and distinguishes a per-day 429 from a transient one, so exhaustion is a clean resumable stop rather than a run that dies half-finished.

**Correction (2026-07-22, measured by exhausting it mid-run): the daily allowance is not ~1,500, and it varies by an order of magnitude between models.** Both this document and the harness carried "~1,500/day" until a run needed 858 grades and discovered otherwise:

| Model | Measured requests/day | Fit for a benchmark judge |
|---|---:|---|
| `gemini-flash-latest` (→ `gemini-3.6-flash`) | **20** | unusable |
| `gemini-flash-lite-latest` (→ `gemini-3.5-flash-lite`) | **500** | usable; now the pinned judge |
| `gemini-2.0-flash` | 0 when probed (already exhausted) | — |

The quota id is `GenerateRequestsPerDayPerProjectPerModel-FreeTier` — per project, **per model**, so switching model buys a fresh allowance and no amount of client-side rate limiting buys more of the same one. The judge is therefore pinned to `gemini-flash-lite-latest`. Judging is a binary comparison against a supplied gold answer, which a Lite model does well, and judge instability is measured per run rather than assumed either way.

**The consequence for planning is that the schedule is denominated in days, not hours.** At 500 grades/day, a three-arm full-suite LoCoMo run needs ~3,400 judge calls ≈ **7 days** of free-tier budget. The checkpointing is therefore load-bearing rather than a nicety: the run is *designed* to be stopped by quota and resumed the next day.

**Model ids are pinned to the rolling `-latest` aliases.** `gemini-2.5-flash` returns `404 — no longer available to new users` on `generateContent`.

### The recurring hazard: checks that pass while proving nothing

The `gemini-2.5-flash` failure is worth generalising, because it is **the fourth instance of one pattern in this project** and the pattern is what keeps costing time:

1. A restore path sat green in CI for weeks while broken.
2. A golden wire fixture pinned an exchange the server has always refused.
3. The bench suite's DB fixtures passed only because an earlier session had warmed the `engraphy_app` role — on a fresh database they failed.
4. **The dead Gemini model id still answers `countTokens`** — so a construction-time check and a token-count probe both passed, and only the first real *generation* failed.
5. **`smoke_live` made four judge calls and passed, against a 20/day cap.** Four calls cannot distinguish a 20/day allowance from a 1,500/day one. The check answered "can this model reply at all"; the run needed "can it reply 858 times", and nothing in the suite could tell those apart until a real run tried.

The shape each time: **a check that exercises a neighbouring capability and is mistaken for evidence about the one that matters.** Counting tokens for a model is not evidence you can generate with it; a fixture parsing is not evidence the server accepts it; a suite passing is not evidence it is self-contained; and a call succeeding is not evidence a thousand calls will.

Instance 5 also exposed the failure the hazard *causes*, which is worse than the hazard itself. When the judge began returning 429s, `phase_judge` recorded each failure as a verdict of `correct=False` and checkpointed it — so 60 of the first 62 rows became permanent wrong answers that a resume would never retry. Left running, the harness would have produced a fully-manifested LoCoMo accuracy figure, complete with Wilson intervals and prompt hashes, that measured a rate limit. **A harness must never persist a failed measurement as a measurement.** Failed grades are now dropped rather than stored, the row stays ungraded and is counted as such, and five consecutive judge failures stop the run.

The defence is not more checks — it is asking, of any green check, *what exactly would have to be broken for this to go red?* If the answer is "something other than the thing I care about", the check is decoration. This is why the harness's own claims are verified by execution against the real endpoint, and why `count_tokens` on the CLI client **raises** rather than returning a number that would look like a measurement.

**There is no temperature to pin** *(corrected July 2026, during implementation)*. `temperature`, `top_p` and `top_k` were removed on Opus 4.8/4.7 and return HTTP 400 — the standard benchmark-harness reflex of "set temperature to 0 for reproducibility" is not merely discouraged, it fails the call. This document's original draft asserted temperature-0 judging; that was not implementable and is corrected here rather than quietly dropped.

The consequence is that **determinism is measured, not assumed**, which the calibration run above already does. Two things partly substitute for it: structured output via `output_config.format` removes format variance from the sample space (most of what temperature pinning bought), and every LLM role's model id and effort level are pinned in the manifest. Residual sampling variance is real, is reported, and is the reason no accuracy delta smaller than the measured judge instability is claimed as a result.

---

## Metrics: accuracy, tokens, latency

All three are captured by one wrapper (`bench/core/meter.py`) so that no stage can be measured differently from another.

### Accuracy

Binary per question; reported per suite-native category and in aggregate. Reported with a 95% Wilson interval — LoCoMo's smaller categories are a few dozen questions and a point estimate there is not a claim.

### Token accounting

**The headline token number is: tokens in the serialized retrieval envelope handed to the reader, per question.** That is the operational meaning of "tokens returned into the agent's context" — it is what a real agent pays to have Engraphy's memory in front of it.

Rules, so the number is not quietly gamed:

- Counted on `json.dumps(retrieval.envelope)` — the exact bytes a client receives, including keys and structure, not just body text.
- The reader's system prompt and the question are counted **separately** as `overhead_tokens` and never folded into the memory number.
- **Ingest-side tokens** (extractor calls, confirm adjudication) are accounted separately as `ingest_tokens`, reported per haystack and amortized per question. They are never netted against retrieval tokens. A system that spends enormous ingest tokens to save retrieval tokens must be visible as such.
- Judge tokens are harness cost and are excluded from every system-cost figure.
- The tokenizer is recorded in the manifest by name and version. Counting uses the Anthropic token-counting API with a local deterministic fallback; a run may not mix the two, and the manifest says which was used.

Reported: `tokens_p50`, `tokens_p95`, `tokens_mean` per category, and **accuracy-per-1k-tokens** as its own headline figure.

### Latency

`time.perf_counter()`, matching `engraphy/tests/bench.py`'s idiom. Per-stage timings are captured for: `embed`, `search`, `traverse`, `briefing`, `write`, `resolve_duplicate`, `reader`, `judge`.

**End-to-end system latency = embed + retrieval + reader.** The judge is excluded (it is the harness, not the system) and reported separately so the exclusion is visible rather than assumed. Ingest latency is reported per haystack, not per question. p50 and p95 for everything; means are not reported alone.

---

## The duplicate-stream benchmark (`dupstream`)

No public suite tests this, and it is where embed-and-append systems have no defense. It is ours to define, which means it must be defined conservatively enough to be credible to a hostile reader.

### Generation

A generator (not an LLM at scoring time — see below) produces a stream of facts organized into **labeled equivalence classes**. Each class is one underlying fact with *N* surface forms:

| Variant kind | Example transformation | Expected band |
|---|---|---|
| `exact` | byte-identical restatement | merge (≥0.95) |
| `near` | punctuation, filler, contraction changes | merge |
| `paraphrase` | restructured sentence, synonyms, same fact | **confirm band** — the interesting case |
| `elaboration` | same fact plus a genuinely new detail | confirm band; merge should produce an addendum |
| `contradiction` | same subject, incompatible value ("prefers tea" → "prefers coffee") | supersede, not merge |
| `distinct` | different fact, lexically similar subject | insert — a merge here is a **precision failure** |

Classes are generated with an LLM offline and **committed as a fixture** with their labels, so scoring is deterministic and re-runnable without an LLM. Stream order is seeded and recorded; three seeds are run and the spread is reported, because order affects which member of a class becomes canonical.

### Metrics and pass criteria

Scored against synthetic ground truth — **no LLM judge for this benchmark.**

| Metric | Definition | Pass criterion |
|---|---|---|
| **Dedup precision** | 1 − (cross-class merges / total merges) | ≥ 0.98 — a cross-class merge destroys a fact and is the expensive error |
| **Dedup recall** | within-class merges / within-class merge opportunities | ≥ 0.85 |
| **Store growth** | `count(active nodes)` / `count(unique classes)` | ≤ 1.5× at 10 variants per class |
| **Post-dedup retrieval** | accuracy of one probe question per class after ingest | ≥ the accuracy of the same probes against a no-dedup control corpus |
| **Contradiction handling** | fraction of `contradiction` pairs ending as a supersede chain | reported, not gated (extraction-dependent) |
| **Contradiction destruction** *(added 2026-07-21)* | contradiction pairs auto-merged into the fact they overturn / contradiction pairs total; equivalently reported as `precision_incl_contradictions` = 1 − (fact-destroying merges / total merges), fact-destroying = cross-class + contradiction | **first-class reported, not yet gated** — a gate is set only after the supersede-aware arm (below) establishes the achievable ceiling; until then any published `dupstream` figure MUST present this beside dedup precision, with the mechanism stated (cosine is agreement-blind) |

The **no-dedup control** is the comparison that makes the benchmark mean anything: the same stream written into a second scope with every class member forced to insert (via a test-only threshold override, which is legitimate *as a control arm* and is labeled as such in the manifest — it is never the measured arm). Growth and token cost are reported as the ratio between the two arms. That ratio is the claim: *this is what dedup buys you.*

Pass criteria above are **initial** and will be re-baselined once measured; they are stated now so the first run can fail honestly rather than being graded after the fact.

### First measured run (2026-07-21) and the recorded metric decision

`runs/published/dupstream-2026-07-21.json` (three seeds, live Postgres, real embeddings, `AlwaysDistinct`). Against the pre-registered gates: dedup precision (cross-class) **0.988 / 1.000 / 1.000 — PASS**; recall **PASS**; growth 1.04–1.13× (store reduction 4.62× vs the control arm) — **PASS**; probe parity 1.00 in both arms — **PASS**. The run also computed the stricter `precision_incl_contradictions`: **0.884–0.914 (mean 0.897)** — 28 of 36 contradiction pairs were auto-merged into the very fact they overturn, because a contradiction is lexically near-identical to its target and cosine similarity cannot distinguish agreement from negation.

**The definitional decision, recorded here per this document's own no-quiet-edit rule:** the pre-registered cross-class gate stands unchanged and *passed* — it measured what it claimed to measure. Contradiction destruction is not folded into it retroactively (that would be regrading after the fact, in the unflattering direction but no more legitimate for it); it becomes the first-class reported metric above instead, ungated until the supersede-aware arm establishes a defensible bar. Neither number may be published without the other. The product-side ruling on the finding itself — caller-responsibility contract, merged-envelope `instruction`, import caveat — is in DECISIONS-DELTA (2026-07-21) and [02](02-retrieval-and-dedup.md) §What auto-merge cannot see.

### The supersede-aware arm (specified 2026-07-21, unbuilt)

`dupstream` gains a third labeled arm measuring the **designed repair path's ceiling**: contradiction variants carry supersede intent (from the fixture's own labels — legitimate in a synthetic-truth benchmark exactly as the no-dedup control arm is legitimate: *labeled, and never the measured headline*), so ingest routes them via `dedup.supersede` instead of plain `write`. Expected: contradiction destruction ≈ 0, supersede chains verifiable, precision/growth otherwise comparable. The naive arm remains the headline — it measures what an uninstructed caller gets; the pairing measures what following the merged-envelope instruction buys, which is the honest form of the "caller responsibility" claim: a responsibility the benchmark can show being met is a contract; one it cannot is an excuse. Manifest records the arm as `contradiction_arm: naive | supersede_aware`.

---

### First LoCoMo measurement (2026-07-22) — a subset baseline, not a score

`runs/published/locomo-3conv-2026-07-22/`. Three conversations (conv-26, conv-30, conv-49 — 63 of the suite's 272 sessions, 1,297 turns, 500 questions), `AlwaysDistinct`, `SearchOnly`, in-process. Ingest cannot be subsetted — a question is unanswerable until its whole conversation is stored — so restricting to whole conversations is the only subsetting that saves anything.

**The dataset holds 1,986 questions, not the 1,540 usually quoted**; the difference is exactly the 446 adversarial ones. Any comparison against a published figure must use the excl-adversarial denominator, and the report prints both.

Accuracy, paired on the 108 questions graded in both arms (conv-26; the judge's daily quota interrupted grading of the second arm):

| category | verbatim floor | LLM extractor | gap |
|---|---|---|---:|
| overall (excl. adversarial) | 36.1% [28–46] | 44.4% [35–54] | +8.3 |
| single-hop | 80.8% [62–91] | 69.2% [50–84] | **−11.5** |
| temporal-reasoning | 10.8% [4–25] | 40.5% [26–57] | **+29.7** |
| multi-hop | 28.1% [16–45] | 34.4% [20–52] | +6.3 |
| open-domain-knowledge | 38.5% [18–64] | 30.8% [13–58] | −7.7 |

Two results run against expectation and are recorded because they do:

- **The floor beats typed extraction on single-hop** (80.8% vs 69.2%). Verbatim keeps the speaker's own words, so a direct fact lookup matches the wording the question was written from; extraction paraphrases and loses the handle. The floor is not a strawman.
- **Typed extraction is worth ~30pp on temporal reasoning** (10.8% → 40.5%), the category this document names as a known weakness and declines to chase. The gain is attributable to `occurred`-style attrs the LLM extractor fills and the verbatim floor cannot. Temporal remains the weakest category in absolute terms and no bi-temporal work is implied.

The cost side moved much further than accuracy, and is the more useful finding: the LLM extractor stored **231 nodes against the floor's 1,278** (5.5×), wrote 133 edges against zero, and cut confirm-band traffic from **95% to 45%** of writes — under `AlwaysDistinct` the floor parks 19 writes in 20 pending an agent round-trip before anything is durable. It paid 47 minutes of ingest against 5.

**What this run does not support.** Judge self-consistency was never measured (quota died first), so no delta here is claimed as significant. The `SearchThenTraverse` multi-hop arm was answered but never graded, so the multi-hop-vs-traverse question that motivated choosing LoCoMo is still open. Retrieval payload was ~6.2 KB either way, because `search()` returns a fixed count at full detail and applies no relevance floor — the floor is briefing-only, `BriefingThenSearch` was not run, and **this run therefore says nothing at all about token efficiency**. Absolute accuracy (36–45%) sits well below published LoCoMo figures; with one conversation in the pairing, no judge-noise figure, and a reader that abstains on ~20% of answerable questions, that gap is not yet attributable.

---

## Isolation: spaces, scopes, and the bench pack

- **One space per run.** The space id embeds the run id, so a re-run never contaminates a prior one and a partial run can be dropped wholesale.
- **One scope per haystack.** LoCoMo's conversations and LongMemEval's haystacks each get their own scope. Dedup candidate queries are scope-filtered, so facts from conversation A cannot become merge candidates for conversation B.
- **A dedicated bench pack** (`bench/pack/bench-pack.yaml`) that declares **no ambient scopes.** This is load-bearing: the starter pack declares `personal` as ambient, and `ambient_scope_set_async` would expand every haystack's candidate set to include it — silently leaking dedup candidates across haystacks and corrupting both the dedup and the accuracy numbers.
- The bench pack's node types are a small, honest ontology for conversational fact extraction, derived from the starter pack and extended only where a conversational corpus genuinely needs it. It is committed and validated against `packs/schema.json` like any other pack.
- **A leakage test is an acceptance gate**, not an afterthought: ingest two haystacks containing the same fact, assert two distinct active nodes and that neither appears in the other's search results.

---

## Neutrality safeguards

The "claimed vs observed" controversy is the reason anyone will distrust this document's eventual numbers. Every vendor writes its own harness and every vendor's harness flatters its vendor. These are the specific commitments that make ours checkable.

1. **A frozen run manifest.** Every run emits `manifest.json` recording: engine git SHA, harness git SHA, pack file hash, band thresholds actually in effect (read back from `config`, not asserted), extractor name, confirm policy, retrieval strategy, all three prompt file hashes, model ids and versions for extractor/reader/judge, tokenizer id, transport, seeds, and dataset file hash. **A result without its manifest is not publishable.**
2. **No per-question, per-category, or per-suite branching** anywhere in the shared core. One strategy, one prompt set, one policy per run. Enforced by review and by the shim line-count smell test.
3. **No non-shipping tuning.** No threshold overrides, no `search` relevance floor, no rerank that isn't implemented in the engine, no bumped limits, no detail-level overrides chosen to help. The single exception is `dupstream`'s no-dedup **control arm**, which is explicitly labeled and is never the measured arm.
4. **The extractor gap is published.** Verbatim and LLM extractor runs are both reported. The delta is the adapter's contribution and we state it rather than hiding it.
5. **Adapter variance is measured.** Three seeds per configuration; the spread is reported next to the mean. An improvement smaller than the seed spread is not claimed as an improvement.
6. **Judge noise is measured** and reported alongside accuracy (see [judging](#answer-extraction-and-judging)).
7. **Negative results ship.** Temporal categories, and any category where Engraphy underperforms, are reported at the same prominence as the wins. A report that omits a category is void.
8. **Raw artifacts are committed** for every published run: `results.jsonl` carries per-question retrieval envelope hash, answer, verdict, tokens, and timings, so a third party can recompute every aggregate.

### Decision record: temporal is measured, not chased

**Engraphy will report the temporal categories and is expected to underperform Zep on them. We do not build for them inside the harness.** *(Ruled July 2026, Devon: option (a) — no bi-temporal work now. The measured gap decides later whether a buildout is useful and necessary. See `QUESTIONS.md` "bench-temporal".)*

Zep's bi-temporal model stores fact validity intervals and answers point-in-time queries directly. Engraphy's supersede gives current-vs-superseded — a node is active or it is not — which answers "what is true now" well and "what was true in March" not at all. Both LoCoMo and LongMemEval score temporal categories.

The harness could paper over this: extract timestamps into `attrs` and have the retrieval strategy filter on them. **Rejected** — that is adapter-side capability standing in for engine capability, which is exactly the confound the whole document exists to prevent. It would produce a number Engraphy cannot reproduce in a real deployment.

So: temporal is reported honestly as a known weakness, and the measured gap becomes the input to a product decision about whether bi-temporal validity belongs on the roadmap. Whether that decision should be made *before* the first published run is [an open question](#open-questions).

---

## Module and directory layout

Following repo convention: package at top level, tests inside the package, module docstrings citing this document's sections.

```
bench/
  __init__.py
  core/                      # THE SHARED CORE — ~80%, benchmark-agnostic
    corpus.py                # Corpus IR + BenchmarkLoader protocol  (interface 1)
    extract.py               # Extractor protocol, VerbatimExtractor, LLMExtractor
    ingest.py                # windowing, write loop, ConfirmPolicy, supersede resolution
    retrieve.py              # RetrievalStrategy protocol + the three strategies
    answer.py                # Reader
    judge.py                 # Judge + Verdict + calibration
    score.py                 # Scorer protocol (interface 2) + LLMJudgeScorer
    meter.py                 # token counting, perf_counter staging, StageTiming
    llm.py                   # the ONLY Anthropic client; retries, temp 0, usage capture
    space.py                 # per-run space/scope provisioning, pack apply, teardown
    report.py                # aggregation, Wilson intervals, report.md
    run.py                   # orchestrator + manifest
  adapters/                  # THE SHIMS — ~20%, thin
    locomo.py
    longmemeval.py
    dupstream.py
  dupstream/
    generate.py              # offline class generator (LLM, run once)
    fixtures/classes.jsonl   # committed labeled equivalence classes
  prompts/
    extract.md  read.md  judge.md  adjudicate.md      # hashed into the manifest
  pack/
    bench-pack.yaml          # no ambient scopes
  tests/
    test_corpus.py test_extract.py test_ingest.py test_retrieve.py
    test_meter.py test_score.py test_isolation.py test_dupstream.py
  README.md                  # how to run, how to read a manifest
scripts/
  check_engine_does_not_import_bench.py    # CI guard — the dependency arrow
runs/                        # gitignored except committed published runs
```

`bench` gets a console script `engraphy-bench` and a `[bench]` optional-dependency extra.

**Testing note (repo convention):** the LLM roles are behind the `llm.py` seam, so `test_extract.py`, `test_ingest.py`, and `test_score.py` run with a recorded/stub client and need no network. Only `test_isolation.py` and `test_ingest.py` need the database, and they use the existing `pool` fixture idiom and roll back.

---

## Acceptance criteria

Shared core first, in increments, each with tests:

- [ ] `Corpus` IR + both loaders round-trip their suites; question counts match published figures exactly (LoCoMo 1,540; LongMemEval 500).
- [ ] `VerbatimExtractor` ingests one LoCoMo conversation end-to-end through `dedup.write` with no `import_mode`, no threshold override, and a recorded band distribution.
- [ ] Isolation gate: the cross-haystack leakage test passes; the bench pack declares no ambient scopes and this is asserted, not assumed.
- [ ] `meter.py` token counts are byte-reproducible for a fixed envelope; a golden fixture pins the count.
- [ ] All three retrieval strategies run against a seeded corpus; `SearchThenTraverse` demonstrably walks edges (asserted by a test that fails if the graph is edgeless).
- [ ] `LLMExtractor` emits drafts conformant to the bench pack's attr specs, verified by the same `attr_spec` validator the engine uses — a draft that would be rejected at write time is caught in extraction tests.
- [ ] Judge calibration run committed; judge instability figure reported.
- [ ] `dupstream` classes committed; precision/recall/growth computed against the no-dedup control arm.
- [ ] First full LoCoMo run with a complete manifest and committed `results.jsonl`.
- [ ] CI guard proves `engraphy/**` does not import `bench`.

---

## Open questions

The two product calls this document originally raised were **both ruled on 2026-07-21 by Devon** and are resolved in `QUESTIONS.md`:

1. **Confirm-band resolution policy** — modular strategy; ship `AlwaysDistinct` (primary) and `LLMAdjudicate`; **`AlwaysMerge` is not implemented at all**. Report the pairing as the "does the confirm band earn its keep" comparison.
2. **Temporal scope** — option (a): no bi-temporal work now, publish the weakness, let the measured gap inform a later decision.

Also settled: **the headline number uses `LLMExtractor`** (like-for-like with competitors, all of whom use LLM extraction), with `VerbatimExtractor` published beside it as the floor.

Still genuinely open, to be answered by measurement rather than debate:

- **Do the published LoCoMo and LongMemEval file schemas match what the loaders encode?** The loaders are written against the datasets' published structure and validated against committed miniature fixtures. They parse **strictly** and fail loudly on an unrecognized shape rather than silently mis-parsing — but the first run against a real download is the proof, and the question stays open until then.
- **What are honest `dupstream` pass criteria?** The stated thresholds (precision ≥0.98, recall ≥0.85, growth ≤1.5×) are pre-registered so the first run can fail honestly. They will be re-baselined once measured, and the re-baseline must be a recorded decision, not a quiet edit.
- **Does the confirm band earn its keep?** See above — this is now a designed output of the harness, not a question to be settled beforehand.

---

## Deferred work

- **BEAM** — a third public suite. The `BenchmarkLoader` protocol is the entire integration surface; adding it later is a shim file, which is the design working as intended.
- **Cross-encoder rerank** — specified in [02](02-retrieval-and-dedup.md#ranking) and not implemented. When it lands it becomes a fourth strategy, not a modification to an existing one.
- **Competitor re-runs** — we cite published numbers rather than re-running four vendors' harnesses.

---

## File reference

| File | Contents |
|---|---|
| `bench/core/corpus.py` | `Corpus` IR, `BenchmarkLoader` protocol — shim boundary 1 |
| `bench/core/score.py` | `Scorer` protocol — shim boundary 2 |
| `bench/core/ingest.py` | Window → extract → write loop, `ConfirmPolicy`, supersede resolution |
| `bench/core/retrieve.py` | The three declared retrieval strategies |
| `bench/core/meter.py` | The three metrics; the only place tokens are counted or clocks are read |
| `bench/core/llm.py` | The `LLMClient` protocol, `StubLLM`, and the per-role model record |
| `bench/core/providers.py` | The only LLM clients in the repo: Claude CLI (subscription) and Gemini (free tier) |
| `bench/smoke_live.py` | Live one-call-per-role verification against both real providers |
| `bench/pack/bench-pack.yaml` | Bench ontology; **no ambient scopes** |
| `bench/dupstream/fixtures/classes.jsonl` | Committed labeled equivalence classes |
| `scripts/check_engine_does_not_import_bench.py` | CI guard: the dependency arrow points one way |
