# Implementation Plan — Dedup Write Path and Transactionality

The integrity kernel: the single pipeline every write travels (tool `write`, aliases, `resolve_duplicate`, `supersede`, inbox promotion, bulk import). Its hard parts are not the band arithmetic — they're the **concurrent-duplicate race**, **merge mechanics that can't half-apply**, and **pending resolution against a world that changed since parking**. This plan fixes all three.

**Normative inputs:** [07 §Exact formulas / §Canonical tool I/O](../07-implementation-contracts.md), [02 §Deduplication write path / §Resonance](../02-retrieval-and-dedup.md), [06 §Dedup under visibility](../06-teams-and-sharing.md)
**Fixtures:** `dedup_cases.yaml`, `jaccard_cases.yaml` (starters committed)

---

## Pipeline shape

```
                       ┌──────────── OUTSIDE any transaction ────────────┐
                       │ 1. pydantic validation (type, scope, attrs      │
                       │    mirror — friendly errors)                    │
                       │ 2. embed(title + "\n" + body)   (~20–80ms CPU)  │
                       └─────────────────────────────────────────────────┘
BEGIN  (GUCs set by the wrapper — see visibility plan)
  3. pg_advisory_xact_lock( hashtextextended(space_id || ':' || type, 0) )
  4. candidates: top-5 same-type, scope-set = target ∪ readable-ambient,
     status='active', ORDER BY embedding <=> $q  LIMIT 5
  5. band = f(max similarity)          -- ≥.95 merge | [.80,.95) pending | <.80 insert
  6. branch:
       INSERT  → insert node; insert links (rule-checked); …
       MERGE   → jaccard novelty → maybe append addendum (jsonb attrs.addenda);
                 attach request links to canonical (skip UNIQUE conflicts);
                 error-type: always add happened_at addendum entry
       PENDING → insert pending_writes row (payload + embedding + candidates snapshot,
                 expires_at = now()+'24h'); NO node row
  7. dedup_log row (every encounter, all bands)
  8. audit_log row
COMMIT
  9. resonance query (read-only, separate statement, RLS-filtered,
     excludes the written/canonical node id)                     → response envelope
```

## Decision: why an advisory lock, and exactly which one

**The race:** two devices write the same lesson within one embedding-latency window; both run step 4, neither sees the other (neither committed), both INSERT → permanent duplicate — the exact failure dedup exists to prevent, triggered by the multi-device usage the product exists to serve.

**Decision: serialize writes per (space, type) with `pg_advisory_xact_lock(hashtextextended(space_id || ':' || type, 0))`** taken *before* the candidate query. Transaction-scoped (auto-released on commit/rollback/crash — no leak path), key collisions across the 64-bit hash space are harmless (spurious serialization, never spurious sharing). Cost analysis: writes within one (space, type) become sequential; at tens of writes/day/space with ~10ms transactions, contention is unmeasurable. Rejected: `SERIALIZABLE` isolation (retry loops in every caller for a problem a lock removes), unique-index-on-embedding tricks (similarity isn't equality), fuzzy uniqueness at insert (the candidate query *is* the check — it just needs mutual exclusion). **The embed step must stay outside the transaction** — holding a lock across a 50ms model call would serialize on the slow part; embedding is pure, so early evaluation is free.

## Merge mechanics (step 6-MERGE, exact order)

All inside the one transaction, on the **canonical-resolved** candidate (chase `canonical_id` first — merging into a merged node is trap #1):

1. Jaccard novelty (tokenizer + `J < 0.8` per [07](../07-implementation-contracts.md#exact-formulas)) against `body + all existing addenda`; if novel, append `{merged_at, source_client, author_principal, body}` to `attrs.addenda` (jsonb `||` array append; create array if absent).
2. `error`-type special case: append the incident record regardless of novelty verdict *if* `attrs.happened_at` differs from canonical's (a re-occurrence is data even when the wording is identical) — one addendum, not two, when both rules fire.
3. Attach the request's `links` to the canonical node: each edge inserted with `ON CONFLICT (src_id, dst_id, type) DO NOTHING`; count attached vs skipped for the envelope.
4. `updated_at` touch fires via trigger; recall stats untouched (a merge is a write, not a recall).

**Node-merge (from `resolve_duplicate(merge)`) additionally** re-points the loser's existing edges — the insert-select-delete pattern, because `UPDATE` cannot `ON CONFLICT`:

```sql
INSERT INTO edges (space_id, src_id, dst_id, type)
  SELECT space_id, $canonical, dst_id, type FROM edges WHERE src_id = $loser
  ON CONFLICT (src_id, dst_id, type) DO NOTHING;        -- and the mirror for dst_id
DELETE FROM edges WHERE src_id = $loser OR dst_id = $loser;
UPDATE nodes SET status='merged', canonical_id=$canonical WHERE id=$loser;
```

Self-edge guard: skip rows where the re-point would create `src = dst` (the loser was linked to the canonical itself) — delete only.

## Pending resolution against a changed world

`pending_writes` rows outlive their transaction by up to 24h; **everything may have changed**. `resolve_duplicate` re-validates inside its own locked transaction:

| Changed since parking | Behavior |
|----------------------|----------|
| Candidate got merged | Chase `canonical_id`; proceed against the canonical |
| Candidate archived/superseded | `merge` → `ENGRAPHY_PENDING_EXPIRED`-class error naming the reason, instructing a fresh `write` (world moved; re-judge). `distinct` → proceed (insert), `relates_to` edge to the *nearest still-active* candidate or none |
| Pack tightened; parked attrs now invalid | Re-run attr validation at resolve; failure → `ENGRAPHY_VALIDATION` with the worklist message — a parked write never bypasses current schema |
| Scope became unwritable for the author | `ENGRAPHY_SCOPE_UNKNOWN` (not-found semantics) |
| TTL passed | `ENGRAPHY_PENDING_EXPIRED`; a nightly sweep deletes expired rows (sweep is idempotent; resolution checks `expires_at` itself and never relies on the sweep) |
| `distinct` path | Insert **re-runs the full band check** (another writer may have inserted the twin meanwhile — the advisory lock only protects concurrent, not sequential, races). A ≥ 0.95 hit at resolve-time converts the `distinct` into a merge-with-notice in the envelope |

## Supersede atomicity

`supersede(old_id, …)` is one transaction: validate old node (readable, writable scope, status `active`, same type as replacement — cross-type supersession is a modeling error, rejected); run the write pipeline **with `old_id` excluded from the candidate set** (the replacement is *supposed* to be ~0.9-similar to what it replaces — trap #2, fixture-covered); insert `supersedes` edge; flip old to `status='superseded'`. Kill-mid-call leaves the old node untouched — asserted by the crash test.

## Traps (fixture- or test-covered, every one)

1. **Merge into merged** — canonical chase before compare/merge; chain cap 10 with `ENGRAPHY_INTERNAL` beyond (doctor flags chains > 3).
2. **Supersede self-collision** — old_id exclusion above.
3. **Embedding inside the lock** — CI grep: no `embed(` call inside a `transaction()` block.
4. **Addenda unbounded growth** — no hard cap (data loss is worse); `doctor` flags nodes with > 20 addenda as consolidation candidates.
5. **Resonance self-hit** — exclude written/canonical id; fixture asserts.
6. **Import mode** — same pipeline, two flags: PENDING → `review_queue` CSV row instead of parking; MERGE silent. No third code path — the flags gate steps 6-PENDING and the envelope only. Step 9 (resonance) is skipped entirely under import (July 2026 revision): it is a post-COMMIT read-only report with no consumer there; durable write-side state stays byte-identical to a normal write.
7. **Dedup candidates under visibility** — candidate scope-set is writer-*readable* only ([06](../06-teams-and-sharing.md)); the two-members-private-twins fixture asserts no co-candidacy.
8. **`links` in a PENDING write** — parked with the payload, applied on resolution, re-rule-checked then (the edge rules may also have changed).

## Test plan

| Test | Assert |
|------|--------|
| Band fixtures | The three bands at pinned-model similarities; boundary values exactly at 0.80 / 0.95 |
| **Race test** | Two connections, same novel text, barrier-released simultaneously: exactly one node, second write returns `merged`; repeat 100× |
| Crash test | `kill -9` injected between steps 6 and COMMIT (via a test hook): zero partial state — no node without dedup_log is acceptable *[sic: both or neither]* |
| Merge mechanics | Addenda novelty both ways; error-type re-occurrence; link attach counts; loser edge re-point incl. self-edge guard |
| Pending world-change | Every row of the table above |
| Supersede | Atomicity + self-exclusion + cross-type rejection |
| Import | Idempotent re-run; review-queue routing; 1k-item throughput sanity |

## Build order

1. `jaccard.py` + fixtures (pure function first — zero dependencies).
2. Band selection + candidate query against seeded embeddings (`dedup_cases`).
3. The transaction script for INSERT band end-to-end (simplest branch) + crash test harness.
4. MERGE branch + mechanics tests.
5. PENDING branch + `resolve_duplicate` + world-change table.
6. Advisory lock + race test.  7. `supersede`.  8. Import-mode flags.  9. Resonance envelope.
