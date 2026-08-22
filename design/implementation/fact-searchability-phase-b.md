# Phase B — merge-link write path (`same_topic`), addenda promotion, collapse exemption, dual-index CI

**Normative parent:** `design/analysis/fact-searchability-model.md` (§2, §4, §6,
§7 Phase B; rulings Q1/Q2/Q8 folded 2026-07-28). This spec is the component plan
for Phase B, at the same normative level as the other
`design/implementation/*-plan.md` docs: follow the build order, make every
listed trap a passing fixture, and route ambiguity through `QUESTIONS.md`
instead of code — never invent silently, never weaken a fixture to pass it.
**Phase A result (context):** the config stopgap un-hid 38→2 absorbed facts and
recovered 11/61 previously-unsearchable questions, 0 attributable regressions —
the mechanism is proven; Phase B builds the real thing in the engine.

## Base branch and topology (read first)

Phase B is engine work that must also touch the read-time collapse (item 4),
which lives only on the `feature/reader-context-tightening` lineage
(`engraphy/core/search.py`: `_near_dup_pairs` / `_collapse_by_pairs`, commit
`10254ff`), not on `phase/e3` or this design branch. **Implement on a branch cut
from `feature/reader-context-tightening`** (a strict descendant of `phase/e3`
carrying the real engine + bench + renderer). If Devon designates a different
integration branch, the only requirement is: the collapse code must be present,
or item 4's changes land as part of porting it. Verify with
`git merge-base --is-ancestor phase/e3 <base>` and a grep for
`_collapse_by_pairs`.

## Scope

**In:** pack ontology additions (`same_topic`), `engraphy/core/dedup.py` merge
branch, one DB migration (dedup_log band CHECK), `engraphy-admin addenda promote`,
`engraphy/core/search.py` collapse exemption, wire fixtures + `wire_types.py` +
sentinel for the new envelope, the dual-index CI invariant test, and the Phase B
re-measure configuration.
**Out (explicitly):** attrs/addenda in the embedding or tsvector, any re-embed
of existing rows, dedup-threshold *value* changes, anchors/`anchor_edges`, any
envelope change to `search`/`traverse`/`get` beyond the collapse exemption's
invisibility — those are Phases C–D. The embedded surface does not change in
Phase B; see §5 for what "recalibration" honestly means here.

## Build order (fixtures before code, per IMPLEMENTER.md)

1. Fixture/pack commit: pack YAML additions (§1), wire fixture for
   `merged_linked` (§2.4), dedup fixture cases for the band split (§5.2),
   migration for the `dedup_log.band` CHECK (§2.5). Stop for review.
2. `dedup.py` merge branch (§2) + tests green.
3. Collapse exemption (§4) + tests green.
4. `addenda promote` (§3) + tests green.
5. Dual-index invariant test (§6) — must pass against everything above.
6. Measurement run (§7). DECISIONS-DELTA/QUESTIONS updates throughout (§8).

---

## 1. `same_topic` into the ontology

### 1.1 The edge, defined once

Engine-literal edge type (the third, after `relates_to`-on-distinct and
`supersedes`): *the write path banded these two nodes as the same topic
(≥ t_high) while the novelty gate judged their content distinct — a fact
cluster.* Registry `bidirectional: true` (traversal reaches the cluster from
either end regardless of `direction`); attached direction is always
**member → canonical**. Members and canonicals are always the same node type
(dedup candidates are same-type), so the wildcard rule adds no real surface.

### 1.2 Exact additions per file

Add to **`packs/starter/pack.yaml`** and **`bench/pack/bench-pack.yaml`**
(matching their compact one-line style), in `edge_types`:

```yaml
  same_topic: {description: "Same topic, distinct content: the write path banded these nodes as near-duplicates but kept both because each carries its own information. Attached automatically on merge; walk it to see every stored statement on a topic.", bidirectional: true}
```

and in `edge_rules` (with the comment):

```yaml
  # The merge path attaches same_topic automatically when a near-duplicate
  # carries distinct content (design/analysis/fact-searchability-model.md §2.2);
  # wildcard like supersedes: member and canonical are same-type by construction.
  - {type: same_topic, src: "*", dst: "*"}
```

Add to **`packs/conversational/pack.yaml`** (matching its block style with
schema-comment conventions):

```yaml
  # Attached automatically by the engine's merge path when two writes are
  # near-duplicates (>= t_high) but each carries distinct content — a fact
  # cluster. Never attach it manually; walk it to see every stored statement
  # on a topic. (design/analysis/fact-searchability-model.md §2.2)
  same_topic:
    description: "Same topic, distinct content: these memories were near-duplicates at write time and both were kept. Attached automatically on merge."
    bidirectional: true
```

plus the same wildcard `edge_rules` row. Add to **`packs/pack-template.yaml`**:
the same block with an authoring comment ("keep this in most packs — the merge
path attaches it; without it, merged-but-distinct facts lose their cluster
edge"). **`packs/schema.json`** needs no change (edge_types/edge_rules are
already generic) — verify, don't assume; if the schema enumerates edge names
anywhere, that's a doc bug → QUESTIONS.md.

**`packs/authoring-guide.md`**: a short subsection under the edges guidance:
`same_topic` is engine-attached like `supersedes`; packs should declare it
(type + wildcard rule); omitting it degrades gracefully (facts are still saved
and searchable; only the cluster edge is skipped, and `pack validate` warns).
**`packs/conversational/agent-guide.md`** and **`packs/starter/agent-guide.md`**:
one row in the edge table — `same_topic` | "Same topic, distinct content —
attached automatically when the engine keeps both of two near-duplicates" |
any → any (automatic; don't draw it yourself).

### 1.3 Graceful skip when a pack lacks the rule

At attach time (both the merge path §2 and the promote migration §3), run an
explicit pre-check — a plain lookup, the same shape the trigger itself uses:

```sql
SELECT 1 FROM edge_rules
WHERE space_id = %s AND type = 'same_topic' AND src_type = %s AND dst_type = %s
```

(rules are wildcard-EXPANDED at apply time, so this is exact-match). Row absent
→ **skip the edge**: do NOT insert it (never let the trigger's CheckViolation
abort a legal write), set `cluster_edge_added: false` in the envelope, still
write the `dedup_log` row (band `merge_linked` — the authoritative membership
record survives without the edge). **No fallback to `relates_to`** (ruled,
Devon 2026-07-28). Pre-check, not try/catch: a CheckViolation inside the write
transaction would poison it.

**`pack validate` warning:** add a warning-channel helper in
`engraphy/admin/packs.py` alongside the existing `check_pack_format()` precedent
(warn, don't refuse): if `edge_types.same_topic` is missing, or present without
a covering rule, emit `"pack does not declare same_topic (+ wildcard rule): the
merge path will save both near-duplicates but cannot link them; clusters lose
their graph edge"`. Surfaced by the CLI on both `pack validate` and
`pack apply`, exactly as format warnings are.

---

## 2. Merge-link in `engraphy/core/dedup.py`

### 2.1 What changes, precisely

All changes are inside the existing single write transaction; the advisory
lock, candidate query, band arithmetic, PENDING branch, `_do_insert`, crash
seam, and resonance step are untouched.

**`_locked_core`, `band == "merge"` branch** (currently: resolve canonical →
`_do_merge`): after `_resolve_canonical`, compute the novelty verdict *before*
deciding which merge to do:

1. Load the canonical's body + `attrs.addenda` bodies (as `_do_merge` does
   today), **plus the bodies of its existing `same_topic` peers** — one query:
   edges where `type='same_topic'` and the canonical is either endpoint, join
   to nodes for `body` (status-unfiltered: a superseded member's content still
   counts as "already represented"). Parent §2.3 step 2: without member bodies
   in the corpus, a fact restated after being merge-linked once would be judged
   novel again and spawn a duplicate member.
2. `novel = is_novel(incoming_body, corpus)` — the existing Jaccard 0.8 bar,
   unchanged (parent Q3, default (a) stands).
3. **`novel == False` → absorb, exactly today's `_do_merge`** (including the
   `error`-reoccurrence addendum special case, which now applies only on this
   path — a reoccurrence with non-novel wording is a provenance event, not a
   distinct fact). Envelope `outcome: "merged"`, unchanged shape. To avoid
   computing the corpus twice, pass the precomputed verdict/corpus into
   `_do_merge` (refactor its internal recomputation away; behavior identical).
4. **`novel == True` → merge-link (`_do_merge_link`, new):**
   - INSERT the incoming as its own row via the existing `_do_insert` (same
     title/body/attrs/embedding/provenance as any insert — the member keeps its
     own attrs, which the absorb path used to discard). Scope = the *request's*
     `scope_id`, as any insert; the canonical was a dedup candidate, hence
     writer-readable, so the edge below satisfies the cross-scope
     read-both/write-one rule by construction.
   - Attach the request's `links` to the **member** (it exists now; the
     omitted-endpoint convention maps to the member, matching the INSERT
     branch, not the absorb branch's canonical mapping).
   - Attach `same_topic` member→canonical, subject to the §1.3 pre-check;
     `ON CONFLICT DO NOTHING` (idempotent under retries).
   - Envelope (new, wire-normative for this phase; 07 amendment at fold-back):

     ```jsonc
     { "v": 1, "outcome": "merged_linked",
       "node":      { …full write.node envelope of the NEW member… },
       "canonical": { "id", "type", "scope", "title" },   // summary stub, no body
       "similarity": 0.97,                // vs the banded candidate, 2dp as today
       "cluster_edge_added": true|false } // false = pack lacks the rule (§1.3)
     ```
   - `dedup_log`: band `merge_linked`, `node_id` = member, `candidate_id` =
     banded candidate (pre-canonical-resolution, as today), similarity as
     today. `audit_log` picks up `outcome`/`node_id` from the envelope
     mechanically — verify the `node_id_for_log` variable is set to the member.

### 2.2 Callers and interactions (each is a test row)

- **`resolve_duplicate(resolution='merge')`** goes through `_do_merge` today;
  it now goes through the same split. An explicit merge with novel content
  returns `merged_linked` — the caller's intent ("these belong together") is
  honored *as a link*, and I1 ("no write reduces findable facts") outranks the
  absorb reading. The `dedup_log` row this path writes keeps its literal
  band value in sync with the outcome (`merge` → absorbed, `merge_linked` →
  linked). Envelope discloses which happened.
- **`supersede`** (ruled, Q2): `_locked_core` returning `merged_linked` is now
  a *success* precondition — the replacement exists as `envelope["node"]`.
  Accept `outcome in ("inserted", "merged_linked")`: insert the `supersedes`
  edge (src = the member/replacement, dst = old_id), flip old to
  `superseded`, return the envelope + `"superseded"`. The
  `SupersedeUnresolvedBandError` guard narrows to `outcome == "needs_confirmation"`
  (the PENDING sub-case — still fail-closed, still open in QUESTIONS.md;
  update that entry's text to its narrowed scope, don't delete it).
- **`import_mode`**: silent-is-report-only, unchanged in spirit: the
  merge-link mechanics run in full (member row, edge, dedup_log); the envelope
  collapses to `{"v": 1, "outcome": "merged_linked"}`. **Import idempotency
  now reads:** re-importing the same file, each line bands ≥ t_high against
  its own prior row (member or canonical), is judged non-novel against the
  cluster corpus, and absorbs with no addendum — second run still creates zero
  rows. This is the updated meaning of design/02's "re-running the same import
  is a no-op"; assert it in the import test.
- **`resolve_duplicate(resolution='distinct')`** is unchanged (its
  collapse-to-insert already preserves the row; its `relates_to` edge stays —
  do not migrate it to `same_topic`: distinct-by-human-call and
  linked-by-band are different assertions).
- **Pending-band writes** are unchanged; a parked payload that resolves
  `merge` enters the split above.

### 2.3 What is deleted

The absorb path's data loss, and nothing else: after this change there is NO
code path that takes a Jaccard-novel body and stores it only as an addendum.
Grep-auditable: the only `addenda.append` sites are the non-novel absorb branch
(reoccurrence case included) — add
`scripts/`-style CI grep only if a second site ever appears (don't build the
grep speculatively).

### 2.4 Wire surface

New fixture `fixtures/wire/write_merged_linked.json` (or the existing write
fixture file's convention — follow it) pinning the §2.1 envelope byte-exactly,
including a `cluster_edge_added: false` variant. Extend
`engraphy/server/wire_types.py` and `engraphy/core/sentinel.py` with the new
outcome per their internal conventions (read them first; they are recent and
their own docstrings are normative for how an outcome is added). The `write`
tool description gains one sentence: "a `merged_linked` outcome means your
memory was similar to an existing one but carried new information — both are
kept and linked."

### 2.5 Migration

`engraphy/db/migrations/00NN_dedup_log_band_merge_linked.sql` (next free number):
drop and re-add the `band` CHECK as
`CHECK (band IN ('merge','pending','insert','merge_linked','merge_linked_promoted'))`.
Additive; down-migration restores the old constraint (and must fail loudly if
new-value rows exist — the standard dbmate down posture in this repo's
migrations; copy the local idiom).

---

## 3. `engraphy-admin addenda promote` — recovering buried facts in existing stores

New admin CLI command (`engraphy/admin/` — local-CLI-only like `import`, never an
MCP tool): `engraphy-admin addenda promote --space X [--scope Y] [--dry-run]`.

**Selection:** active nodes in the space (optionally one scope) whose
`attrs.addenda` is a non-empty array.

**Per node, in one transaction (embeddings computed OUTSIDE it, trap 3):**

1. Build the running corpus: canonical `body` + bodies of existing
   `same_topic` peers (idempotency belt-and-braces; the marker below is the
   real guarantee).
2. Iterate `addenda` **in array order** (merge order). For each addendum `a`:
   - Skip if `a.promoted_to` is set (idempotency marker) — but still append
     `a.body` to the corpus.
   - Skip (leave as addendum) if `not is_novel(a.body, corpus)`; append its
     body to the corpus.
   - Otherwise **promote**: INSERT a member node —
     `type`/`scope_id` = canonical's; `title` per the rule below; `body` =
     `a.body`; `attrs` = `{}` plus `happened_at` if the addendum carries it
     (the error-reoccurrence shape); embedding =
     `embed_document(title + "\n" + body)` (precomputed); provenance columns
     from the addendum record (`author_principal`, `source_client`,
     `source_session`), `created_at` = `a.merged_at` (explicit value — the
     admin connection may set it; the row's age is the merge's age, not the
     migration's). Then: `same_topic` edge member→canonical (§1.3 pre-check;
     count skips for the report), `dedup_log` row (band
     `merge_linked_promoted`, node_id = member, candidate_id = canonical,
     similarity NULL), and set `a.promoted_to = <member id>` in the stored
     addenda array **in the same transaction** — marker and row commit or
     roll back together, which IS the idempotency guarantee: re-running the
     command skips every marked addendum, so a re-run after any crash
     promotes exactly the unmarked remainder and a full re-run is a no-op.
     Append `a.body` to the corpus.
3. The addendum is retained (marked, never deleted) — no-hard-deletes; `get`'s
   merge history stays truthful and now points at where the fact went.

**Title rule (parent Q4 — resolved by this spec, veto-able):** deterministic
first-sentence derivation, no LLM (engine no-LLM rule): take `a.body` up to and
excluding the first sentence terminator (`. `, `? `, `! `, or newline);
whitespace-strip; if the result exceeds 200 chars, hard-truncate to 199 + `…`;
if shorter than 3 chars (degenerate body), fall back to the canonical's title
truncated to 188 + ` — addendum`. Mediocre titles on migrated rows are
accepted; the body (searchable) is what matters.

**No banding during promotion:** the band outcome is pre-decided
(`merge_linked_promoted`); running the write pipeline would re-derive ≥ t_high
against the very canonical and loop. The promote INSERT is direct, like
import's mechanics, with `dedup_log`/`audit_log` rows written explicitly
(audit action `addenda_promote`).

**`--dry-run`** prints the would-promote count and per-node detail, writes
nothing. Report (both modes): nodes scanned, addenda seen, promoted, skipped
non-novel, skipped already-marked, edges skipped for missing rule.

**Tests:** novel addendum → promoted row + edge + marker + dedup_log; non-novel
→ untouched; re-run → zero new rows; crash between nodes (kill seam) → re-run
completes the remainder exactly; pack without the rule → promoted, edge
skipped, reported; `created_at` equals `merged_at`; dry-run writes nothing.

---

## 4. Read-time collapse exemption

In `engraphy/core/search.py` (tightening lineage), the collapse drops a
lower-ranked node that is ≥0.95-similar to a kept higher-ranked one. A member
and its canonical are ≥ t_high similar **by construction**, so without an
exemption the collapse re-hides exactly the fact §2 preserves — the two
features fight.

**Mechanism:** after `_near_dup_pairs` computes the ≥-threshold candidate
pairs and before `_collapse_by_pairs` walks them, remove every pair whose two
ids are joined by a **declared-distinct edge**: one query over `edges` for
`type IN ('same_topic', 'relates_to')` with both endpoints in the pair set
(both orientations; `same_topic` is registry-bidirectional anyway). Pairs
surviving the filter collapse as before.

- `same_topic`: the engine itself declared the pair distinct at write time.
- `relates_to`: included deliberately and flagged here, not silently — the
  engine attaches it on a human/agent `distinct` resolution (the same
  "declared distinct" assertion), and a hand-drawn `relates_to` between two
  ≥0.95 texts is likewise an explicit statement that both belong. One
  DECISIONS-DELTA line records the widening.
- Filtering by *edge existence*, not by `dedup_log`, is the point of the
  dedicated type (parent §2.5): one indexed lookup, no log join.
- Tests: canonical + member both surface in one search (fixture where both
  match the query); an unlinked ≥0.95 pair still collapses (the mechanism
  keeps its purpose); a `relates_to`-joined pair survives.

---

## 5. Band recalibration — what it honestly means in Phase B

**Phase B changes no embedded surface**, so the similarity *distribution* does
not move and no threshold value changes. Anyone "recalibrating" numbers in this
phase is tuning — refuse. The real obligations:

1. **Semantics note, folded at review:** `t_high` no longer gates data
   survival, only cluster granularity (parent §6). design/02's dedup section
   gets that sentence at fold-back, not silently now.
2. **Bench measurement config reverts to engine defaults** (`t_high` 0.95 —
   parent Q6, resolved by this spec, veto-able): Phase A's 0.98 override
   deliberately *bypassed* the merge band; keeping it would measure the
   pending path, not merge-link. The Phase B run drops `--space-config`
   entirely (manifest shows defaults). A priori, fixed before the run.
3. **Fixtures, not values:** extend `fixtures/dedup_cases.yaml` with
   merge-band split cases (novel → `merge_linked`, non-novel → `merged`,
   reoccurrence → `merged` + addendum) at the already-baselined pinned
   similarities — `scripts/baseline_dedup_fixtures.py` re-runs only if new
   *text* pairs are added, and existing pinned values are never edited.
4. **Phase C owns the real recalibration** (surface change): procedure is
   pinned now, a priori — re-run the baseline script against the new surface,
   pinned ±0.02; if the 0.95/0.80 defaults misbehave against the new
   distribution, that is a QUESTIONS.md entry, never a nudge. Phase B merely
   restates this so nobody front-runs it.

---

## 6. CI dual-index invariant test — "can I find what I just stored?"

New `engraphy/tests/test_dual_index_invariant.py`, running against the real DB +
pinned model like the other E1 tests. This is invariant I4 as a regression
wall — the test whose absence let the addenda leak ship.

**Workload:** one space per shipped pack (starter + the example pack + the
conversational pack — "both packs" discipline extended to the pack this work
targets), exercising every write branch with **nonce-bearing bodies** (each
fact's body carries a unique token, e.g. `zq7xk-<n>`, so the lexical leg makes
findability deterministic and reader-independent): plain insert; absorb
(non-novel twin — synthetic `thresholds=BandThresholds(...)` as the existing
dedup tests do); merge-link (novel twin ≥ t_high); pending → distinct;
pending → merge (novel and non-novel variants); supersede clean; supersede
into the merge band; `update` text change; `update` attrs-only; import-mode
merge-link.

**Assertions:**

- **(a) Embedding integrity:** for every active node written, recompute
  `embed_document(title + "\n" + body)` and assert cosine ≥ 0.9999 against
  the stored vector (catches any path that writes text without its embedding;
  becomes the Phase C guard for the surface helper).
- **(d) Findability — the headline:** for every *fact* the workload stored
  (each nonce), `search(space, nonce)` surfaces the node that carries it, or
  its canonical, in `results`. In particular the merge-link twin's nonce finds
  the MEMBER row — the exact query shape that failed silently before Phase B.
- **(c) Graph side:** every `dedup_log` row with band `merge_linked` has a
  matching `same_topic` edge (in the packs that declare it — run one
  workload against a rule-less pack fixture and assert the edge is absent,
  `cluster_edge_added` was false, and the member is still findable);
  depth-1 `traverse` from the canonical returns the member and vice versa.
- **(b) No fact-only-in-addendum:** enumerate every `attrs.addenda` entry in
  the final state; each is either non-novel w.r.t. its node's body+prior
  corpus, or carries `promoted_to`. (Run `addenda promote` in-test on a
  seeded legacy node to cover the promoted arm.)
- **Regression pin:** the pre-Phase-B failure reproduced as a fixture — a
  novel ≥ t_high write whose nonce is then searched — asserted findable; this
  single test failing is the signal the leak has returned.

CI: part of the standard suite (it needs the model + DB already required by E1
tests); no new lanes.

---

## 7. Measurement (closes Phase B)

Re-ingest the 3 diagnostic conversations; arms `llm-conversational:search_only`
+ `llm-starter:search_only`; engine defaults (no `--space-config` — §5.2);
Phase A's prompt/guide edits retained (they are shipped guidance now); same
reader/judge/scoring as matrix-v2. Write-form mechanism checks first, from
ingest.jsonl/dedup_log: `merge_linked` count ≈ Phase A's un-hidden-fact count
(the ~36 that raising t_high exposed), absorbed-with-addendum count near zero
for novel content, node counts up accordingly; then scores vs the Phase A run
AND matrix-v2, per category, against the ±4–5 floor. Report in
`runs/<run-id>/README.md`; result appended to the parent doc's Phase B entry.
Nothing tuned to gold; if the number disappoints, the follow-up is analysis or
Phase C — not a rerun loop.

## 8. Bookkeeping

- QUESTIONS.md: narrow "supersede-nonclean-band" to the PENDING sub-case;
  reference the Q2 ruling.
- DECISIONS-DELTA.md: one line each for — `merged_linked` envelope added ahead
  of the 07 fold-back; collapse exemption includes `relates_to` (the
  deliberate widening, §4); title-derivation rule for promoted members (§3);
  Phase B measurement at defaults (§5.2).
- The parent design doc's §7 Phase B entry gets the completion note + run link
  at review, per the fold-back protocol.

---

## Open-questions review for Phase B (parent §8)

**No question hard-blocks implementation.** Two were resolved *by this spec*
with stated defaults Devon may veto before or during review:

- **Q4 (promoted-member titles) — resolved by spec (§3):** deterministic
  first-sentence derivation, 200-char truncation, canonical-title fallback.
  Veto window: any time before the promote command runs against a real store;
  the bench re-ingest doesn't depend on it (fresh stores have no legacy
  addenda).
- **Q6 (Phase-A threshold retirement) — resolved by spec (§5.2):** the Phase B
  measurement runs at engine defaults so it measures merge-link, not the
  pending path. Veto window: before the measurement run.

**Still open, none blocking Phase B:** Q3 (novelty bar stays 0.8 — the spec
proceeds on option (a); revisiting it is a config-shaped follow-up), Q5 (attr
searchability scope — Phase C), Q7 (anchor bounds — Phase D), Q9 (briefing
anchors — Phase D), Q10 (bench retained-turn text — bench-side audit, touches
no Phase B code path).
