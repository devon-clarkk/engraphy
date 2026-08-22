# Reader context-tightening — steps 1–4 (feature/reader-context-tightening)

Builds on the reader-fix work. Branch is off `experiment/reader-fixes`, which is a
strict descendant of `phase/e3` (verified: `git merge-base --is-ancestor
phase/e3 experiment/reader-fixes` = true) and therefore carries phase/e3's **real**
search engine (not main's stub) plus the bench harness and the prose renderer
(`8be1c0d`). Branching literally off `phase/e3` was not possible: phase/e3 tracks
no `bench/` at all, so it can neither host the renderer nor run the measurement.

Neutrality throughout: every threshold/parameter is set from first principles, not
by looking at which value flips which gold question.

## Step 1 — renderer (merged)
Present as the branch base (`8be1c0d`): the reader receives clean, rank-ordered
prose instead of `json.dumps`. No change needed here.

## Step 2 — per-fact provenance line
`render_envelope` now emits one compact line per fact:
`(recorded <date> · by <author> · in <scope>)`, from the node's `created_at`
(readable date), `author`, and `scope`. `RENDER_FORMAT_VERSION` → `rendered_prose_v2`.

- **Faithful:** only fields the node actually carries are rendered; an all-absent
  node yields no line; an unparseable date is kept raw, never guessed.
- **Honest labelling:** `created_at` is the *ingest* time, so it is labelled
  `recorded`, never `occurred` — the event's own date lives in the body/`attrs`
  (e.g. `occurred_on`), which are rendered separately and untouched.
- **General:** when/who/where a memory was recorded is standard provenance for
  reasoning about recency and source; identical treatment for every node, no
  question-specific logic.

## Step 3 — read-time near-duplicate collapse
After RRF fusion, before the `limit` cut, near-identical active nodes collapse to
the highest-ranked instance (`engraphy/core/search.py`; default-on, disableable via
`collapse_near_dupes=False`). search() and briefing's semantic sections both get it.

- **Threshold = 0.95, and where it comes from:** the dedup **merge band**
  (`dedup.t_high`, design/02) — the write path AUTO-MERGES at ≥ t_high because that
  is its *calibrated* "same fact restated" bar, on the same document↔document cosine
  scale the retrieved embeddings live on. Read-time collapse reuses that exact bar.
  **Not** a number chosen against any benchmark. Conservative by construction: at
  0.95 only near-verbatim restatements collapse, so a genuinely distinct fact is
  never dropped.
- **Why duplicates reach read time at all:** write-time dedup is scope-filtered
  (cross-scope paraphrases co-exist) and the confirm band can resolve `distinct`.
- **Mechanism:** `_near_dup_pairs` computes ≥-threshold pairs in-DB via pgvector
  `<=>`; `_collapse_by_pairs` (pure, unit-tested) walks rank order and drops a node
  only if it duplicates an already-kept higher-ranked one. Reorder-free removal:
  never adds, never reorders, only removes proven restatements.

## Step 4 — rerank + top-k: evaluated, left as-is (a clean negative)

**Node-distance rerank: OFF.** Grounded in the reranking experiment's own
rank-of-gold measurement (`experiment/reranking`, commit `93845a1`), which is
**reader-independent** — it measures whether the gold node's *rank* moves, so the
reader fixes (steps 1–3) cannot change its verdict:

- Net null-to-negative over 127 reader-miss questions: recall@5 −0.024, recall@10
  −0.008, recall@1/@3 unchanged.
- 81% of golds unmoved; of those **92% were already rank ≤ 3** — structurally out
  of node-distance's reach (seeds are frozen at distance 0).
- Of the 24 that moved: 11 improved, 13 regressed (mean −0.07). Category texture:
  single-hop weak-positive (+0.46), **multi-hop clear-negative (−0.58)** —
  promoting reachable neighbours demotes unreachable co-evidence — temporal/ODK flat.

Since the gold is already at the top of the list for the reader-miss cases, and the
renderer already presents that ordering cleanly, reranking has nothing to lift and a
real multi-hop downside. Left off; a clean negative, exactly as permitted.

The rerank machinery is nonetheless brought onto this branch as an **available,
default-off** lever: `engraphy/core/rerank.py` (`rerank_fuse` — RRF across N rank-lists,
pure — plus the node-distance signal) with its DB-free fixture test
(`test_rerank.py` + `rerank_cases.yaml`, 8/8 passing without a database). It is
deliberately **not wired into `search`/`hybrid_fuse`'s default path**: enabling it
would contradict the null-result evidence above, and the `search.py` hook on
`experiment/reranking` also conflicts with step 3's `hybrid_fuse` change. So the lever
exists for a future explicit opt-in, but ships off. `test_rerank_node_distance.py`
(the DB-backed graph test) stays on `experiment/reranking`, since it depends on that
hook, not on this branch.

**Top-k: unchanged (baseline `limit=10`).** Cutting it lower is either a recall
gamble (on questions whose gold ranks 6–10) or answer-key tuning (justified only by
"gold is usually top-3", which is reading the key). The *principled* tightening is
step 3: read-time dedup removes proven restatements so each of the 10 slots carries
a distinct fact, improving precision-per-slot **at held recall** — no floor added to
`search` (design/09 neutrality forbids that), no arbitrary cap.

## Measurement (done — 2026-07-27)
Same 81 reader-miss questions (gold WAS retrieved) + 25 currently-correct control as
the render-only baseline. Retrieval **re-run live** against the 5433 stores (dedup on,
render v2), reader=opus-4-8, judge=sonnet-5, abstention-rule scoring, sequential/
checkpointed. Full engraphy+bench suite green first: **1075 passed, 3 skipped**.

**Result: full 1–3 stack = 11/81 flips wrong→right, vs render-only 12/81 — flat.**
- Churn is LLM re-answer noise, not a stack effect: 9 flipped in both, 2 new, 3 lost
  (e.g. one temporal answer that hedged-correctly before now says INSUFFICIENT; one
  single-hop reworded past the judge — the referent-resolution `conv-26:q144` "the
  son"←"the kids" case flips in both).
- **0 regressions / 25 control held** (9 abstention questions all correctly declined).
  Steps 2–3 are *safe*: provenance doesn't distract, dedup drops no needed node.
- **Dedup fired on 10/106 queries** (surfaces a distinct node a near-dup was crowding
  out, backfilling to keep count) but drove **0 flips**. At the conservative 0.95
  merge band these single-conversation LoCoMo scopes rarely hold ≥0.95 co-active
  paraphrases — write-time dedup already caught them. Corpus-specific null, not a
  broken mechanism; a duplicate-heavy / cross-scope store would exercise it.
- **Provenance produced 0 temporal flips (0/23)** — its most plausible beneficiary —
  and net flat overall. No measurable contribution here.

**Verdict:** on the reader-miss flip metric, provenance + dedup **did not earn their
keep** — the renderer (step 1) is the load-bearing change (~12/81 either way). They
remain **safe, principled, general** improvements (honest source/recency context;
correct dedup on ~9% of queries), so they stay in — but no benchmark lift is claimed
from them. An honest null increment, reported as such (neutrality: nothing tuned).
