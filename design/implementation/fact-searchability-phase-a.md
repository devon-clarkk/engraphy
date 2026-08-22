# Phase A — fact-searchability stopgap (config + guidance only)

**Normative parent:** `design/analysis/fact-searchability-model.md` (§3.3, §6,
§7 Phase A). This spec is self-contained for an implementer session; read the
parent's §1–§2 for the why.
**Hard scope rule: Phase A touches NO engine code.** `engraphy/core/`,
`engraphy/server/`, `engraphy/db/`, and `engraphy/admin/` are off-limits (one
exception is explicitly NOT granted: do not add engine config keys, do not plumb
`write(thresholds=…)` — `bench/core/ingest.py`'s module docstring forbids the
override on purpose, and the per-space `config` table is the sanctioned path
that `dedup._resolve_config` already reads on every write). Phase A also makes
**no ontology change**: no pack.yaml edits, no new edge types — `same_topic`,
attr-embedding, envelope changes, and the addenda-promote migration are Phases
B–D, not this.

## What Phase A is

A measured stopgap for the largest diagnosed leak (38 distinct facts absorbed
into `get`-only addenda in the diagnostic conversational store; see
`design/analysis/write-path-diagnosis.md`): stop spurious ≥0.95 auto-merges via
per-space threshold config, and stop attr-stranding via extractor guidance. Then
re-ingest the 3 diagnostic conversations and re-measure. It is temporary by
design — Phase B's merge-link makes the threshold a granularity knob rather than
a survival knob, and the Phase A raise is then re-evaluated (parent Q6, open).

## Change 1 — per-space `dedup.t_high = 0.98` for bench run spaces

**Value: `0.98`, chosen a priori, structurally.** The auto-merge band exists for
near-verbatim restatement ("re-telling is free"); the measured failure is that
at 0.95, same-entity *typed* nodes cross the band while carrying distinct facts
(38 vs starter's 3 — a ~10× rate). 0.98 is a near-verbatim bar on this model's
document↔document cosine scale, sits inside the sanctioned per-space governance
range (`0 < t_low <= t_high <= 1`, validated by `_resolve_config`), and is NOT
derived from any gold answer or from tuning against the diagnostic scores. Do
not sweep values; do not pick the value that maximizes the re-measure score —
that is answer-key tuning and voids the result.

**`t_low` is unchanged (0.80).** Former 0.95–0.98 auto-merges therefore land in
the PENDING band. This is safe and is the point:

- The bench harness's default confirm policy resolves every pending as
  `distinct` (`bench/core/ingest.py` — the reproducible primary). A `distinct`
  resolution re-enters the banding core with `collapse_pending_to_insert=True`,
  so the fact **inserts as its own embedded, searchable row** and the existing
  distinct-path behavior attaches a `relates_to` edge to the nearest candidate.
  Net effect: Phase A is a config-only *approximation* of Phase B's merge-link
  (row preserved + link), with `relates_to` standing in until the dedicated
  `same_topic` edge exists.
- A pending that meanwhile acquired a ≥0.98 twin still merges
  (merge-with-notice) — the engine's own rule, untouched.

**Mechanism — bench-side plumbing (bench code is in scope; it is not engine
code):**

1. `bench/core/space.py::provision_run_space` gains an optional
   `space_config: dict[str, object] | None = None` parameter. When set, after
   the pack apply, insert one row per key into `config`:
   `INSERT INTO config (space_id, key, value) VALUES (%s, %s, %s)` (value as
   jsonb, same shape `engraphy-admin config set` writes — see
   `engraphy/admin/cli.py::config_set` for the reference encoding; use
   `ON CONFLICT (space_id, key) DO UPDATE` iff that CLI does). Provisioning
   already runs on the superuser connection and `config` is a non-RLS
   reference table, so no policy work is needed.
2. `bench/core/run.py` gains a run-level flag (e.g. `--space-config
   dedup.t_high=0.98`, repeatable; value parsed as JSON like the admin CLI
   does) passed through to every arm's provisioning **identically** — the
   config is symmetric across arms/packs by construction, so it cannot favour
   one pack (same posture as the ambient-scope strip).
3. **Manifest:** record the applied `space_config` verbatim in the run
   manifest, next to the pack hashes. A run with non-default config must be
   distinguishable from a defaults run forever.
4. Production/interactive spaces are NOT touched. (If Devon wants the raise on
   a real space later, that is one `engraphy-admin config set <space>
   dedup.t_high 0.98` — already shipped; note the ergonomics cost: more
   handshakes per near-duplicate write.)

**Tests (bench suite):** provisioning writes the config rows and the manifest
records them; a seeded write at similarity ~0.96 in a configured space bands
`pending` (not `merge`) while ~0.99 still merges — using the existing
synthetic-similarity test patterns from `engraphy/tests/test_dedup.py` as the
model, but living under `bench/tests/` since the engine is unchanged.

## Change 2 — extractor prompt: restate attr values in the body

Edit `bench/prompts/extract.md`. Add to the **Preserve concrete specifics**
section (after the existing paragraphs), verbatim:

> ## Attributes are also facts — keep them in the body
>
> When you supply a typed attribute (an occupation, a location, a date, a
> relationship), the same information must also appear in the `body` text in
> natural words. Attributes are for filtering and display; the body is what is
> searched. A fact stated only as an attribute is stored but not findable —
> so write "Lucy works as a paediatric nurse in Leeds" in the body even when
> you also set `occupation` and `location`.

And add one sentence to **What to extract** (end of the first paragraph):

> Give each distinct fact its own memory — one whole fact per node, in the
> speaker's own concrete terms — rather than folding several facts into one
> node's attributes.

Nothing else in the prompt changes. Both additions are pack-agnostic (the
starter pack barely uses attrs; the guidance is symmetric and general — it
states the engine's real contract, not a benchmark convention).

## Change 3 — conversational agent guide: same convention for live agents

Edit `packs/conversational/agent-guide.md` §Attributes: append one paragraph:

> **Restate attribute values in the body.** Attributes are filterable fields,
> not searchable text: search covers a node's title and body only. Whatever
> you record as an attribute — an occupation, a location, a date — say it in
> the body too, in natural words, or it will not be findable by search. (The
> engine will make attributes searchable directly in a later version; until
> then the body restatement is the contract.)

This file is agent-facing documentation; no schema or tooling change.

## What Phase A explicitly does NOT do

- No `same_topic` edge type, no pack.yaml/ontology edits (Phase B).
- No merge-link write path, no addenda-promote migration (Phase B).
- No attrs/addenda in the embedding or tsvector, no re-embed, no threshold
  recalibration (Phase C).
- No envelope changes, no anchors, no renderer change (Phase D).
- No engine config keys, no `write(thresholds=…)` plumbing, no changes under
  `engraphy/`.
- The absorbed-addenda already in existing stores stay as they are — Phase A
  only prevents new absorption in configured spaces; recovery of old addenda
  is Phase B's promote migration.

## Measurement (required to close Phase A)

Re-run the diagnostic comparison with the Phase A changes active, nothing else
different:

1. **Corpus:** the same 3 diagnostic conversations as
   `runs/locomo-matrix-v2/` (conv-26, conv-30, conv-49), same question set.
2. **Arms:** `llm-conversational:search_only` (the anchor) and
   `llm-starter:search_only` (the comparator), both with
   `--space-config dedup.t_high=0.98` (symmetric), default confirm policy,
   same reader (opus-4-8), same judge (sonnet-5), abstention-rule scoring —
   the matrix-v2 configuration exactly, plus the two prompt/guide edits.
3. **Baselines:** matrix-v2 anchor 327/500, starter 359/500; noise floor ±4–5
   on this pool (see `design/analysis/locomo-loss-taxonomy.md`).
4. **Write-form metrics first (reader-independent mechanism check), from
   ingest.jsonl/dedup_log per arm:** facts absorbed as addenda (expect
   conversational ≈38 → near 0), pending count and distinct-resolution count
   (expect ≈ the former merges), node count (expect +~30-40 for
   conversational), % nodes carrying attrs, median body length (expect up,
   from attr restatement). If these do not move, the config did not take —
   debug before reading any scores.
5. **Then scores:** per-question head-to-head vs the matrix-v2 anchor;
   per-category deltas (temporal and multi-hop are where the diagnosis says
   the recovery lives). Report the anchor delta against the ±4–5 floor
   honestly — including a null.
6. **Report:** a `runs/<run-id>/README.md` in the house style (config recorded,
   deltas, verdict), and the result appended to the parent doc's Phase A entry.

**Neutrality wall (restated, binding):** the t_high value, the prompt wording,
and the guide wording are fixed by this spec *before* measurement. If the
result disappoints, the follow-up is analysis or the next phase — never a
tweak-and-rerun loop against the same 3 conversations.

## Rollback

Change 1 is one config row per run space (and runs are disposable spaces);
reverting = omitting the flag. Changes 2–3 are documentation/prompt text;
revert by git. Nothing durable in any real space is modified.
