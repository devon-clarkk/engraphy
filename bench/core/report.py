"""Aggregation and the written report (design/09 §Metrics, §Neutrality).

Every aggregate in `report.md` is recomputable from the committed
`results.jsonl` by a third party who does not trust this file -- that is
neutrality item 8, and it is the reason the per-question rows carry the envelope
hash, the payload size and the stage timings rather than just a verdict.

Four rules this module enforces on itself, because each is a way a benchmark
report flatters its author without lying:

1. **Every category is printed, including the bad ones.** A report that omits a
   category is void (design/09 §Neutrality item 7). Temporal reasoning is
   expected to be a weakness and is printed at the same size as the wins.
2. **Every accuracy carries a Wilson interval.** On a subset the intervals are
   wide, and a point estimate with a wide interval behind it is not a claim.
3. **The denominator is stated.** LoCoMo's published 1,540 excludes its 446
   adversarial questions; this file holds 1,986. Any comparison against a
   published number that does not say which denominator it used is meaningless,
   so both are printed.
4. **A delta is printed against judge instability.** The extractor gap is the
   headline comparison and it is a difference of two noisy numbers; if it is
   smaller than the measured disagreement rate of the judge, the report says so
   instead of claiming it.
"""

from __future__ import annotations

import json
import pathlib
from collections import defaultdict

from bench.core.meter import percentile, wilson_interval

__all__ = ["aggregate", "render_report", "load_rows", "render_failures"]

# The figure the literature quotes for LoCoMo, and what it excludes. Printed
# rather than assumed: a reader comparing against a published number needs to
# know which of the two denominators produced ours.
PUBLISHED_LOCOMO_QUESTIONS = 1540
LOCOMO_FILE_QUESTIONS = 1986
LOCOMO_ADVERSARIAL_QUESTIONS = 446


def load_rows(path: pathlib.Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def aggregate(rows: list[dict]) -> dict:
    """Per-arm, per-category accuracy / payload / latency.

    Structured as {arm_id: {"overall": {...}, "categories": {cat: {...}}}} so
    the renderer never has to recompute anything and the same numbers can be
    dumped to JSON for a reader who wants them without the prose.
    """
    by_arm: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_arm[r["arm"]].append(r)

    out: dict = {}
    for arm, arm_rows in sorted(by_arm.items()):
        cats: dict[str, list[dict]] = defaultdict(list)
        for r in arm_rows:
            cats[r["category"]].append(r)
        out[arm] = {
            "overall": _bucket(arm_rows),
            # The denominator the literature uses: LoCoMo's published 1,540
            # excludes the adversarial questions, so an Engraphy number compared
            # against a published one must exclude them too.
            "overall_excl_adversarial": _bucket(
                [r for r in arm_rows if not r.get("abstain_expected")]
            ),
            "categories": {c: _bucket(rs) for c, rs in sorted(cats.items())},
        }
    return out


def _bucket(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    correct = sum(1 for r in rows if r.get("correct"))
    lo, hi = wilson_interval(correct, n)
    payload = [r.get("envelope_bytes", 0) for r in rows]
    # End-to-end system latency = retrieval + reader. The judge is the harness,
    # not the system, and is reported separately so the exclusion is visible
    # rather than assumed (design/09 §Latency).
    e2e = [
        r.get("retrieval_seconds", 0.0) + r.get("reader_seconds", 0.0)
        for r in rows
    ]
    judge_s = [r.get("judge_seconds", 0.0) for r in rows]
    return {
        "n": n,
        "correct": correct,
        "accuracy": round(correct / n, 4),
        "wilson_95": [round(lo, 4), round(hi, 4)],
        "abstained": sum(1 for r in rows if r.get("abstained")),
        "reader_errors": sum(1 for r in rows if r.get("error")),
        "judge_errors": sum(1 for r in rows if r.get("graded_by") == "judge_error"),
        "payload_bytes_p50": int(percentile(payload, 50)),
        "payload_bytes_p95": int(percentile(payload, 95)),
        "payload_bytes_mean": int(sum(payload) / n),
        # Reported per 1k bytes because the token headline is unavailable on a
        # free-tier route; the ratio is what the metric is for and a ratio is
        # tokenizer-independent.
        "accuracy_per_1k_bytes": round(
            (correct / n) / (sum(payload) / n / 1000), 4
        ) if sum(payload) else None,
        "e2e_seconds_p50": round(percentile(e2e, 50), 3),
        "e2e_seconds_p95": round(percentile(e2e, 95), 3),
        "judge_seconds_p50": round(percentile(judge_s, 50), 3),
    }


def _pct(x) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def _acc_cell(b: dict) -> str:
    if not b.get("n"):
        return "—"
    lo, hi = b["wilson_95"]
    return f"{_pct(b['accuracy'])} [{100*lo:.0f}–{100*hi:.0f}]"


def _gap_pp(a: dict, b: dict) -> str:
    """Signed percentage-point gap b−a, or `—` when either bucket is empty.

    An empty bucket (`{"n": 0}` from `_bucket`) has no `accuracy` key, which is
    exactly what a partially-graded run produces -- so the gap must degrade to a
    dash rather than KeyError the whole report at the end of a long grade."""
    av, bv = a.get("accuracy"), b.get("accuracy")
    if av is None or bv is None:
        return "—"
    return f"{100 * (bv - av):+.1f}"


def render_report(manifest: dict, rows: list[dict], agg: dict) -> str:
    """The written report. Prose is deliberately plain and states weaknesses in
    the same voice as strengths."""
    L: list[str] = []
    A = L.append

    run_id = manifest.get("run_id", "?")
    A(f"# LoCoMo baseline — run `{run_id}`")
    A("")
    A(f"Generated {manifest.get('generated_at', '?')} · "
      f"engine `{manifest.get('engine_git_sha', '?')[:12]}` · "
      f"harness `{manifest.get('harness_git_sha', '?')[:12]}`")
    A("")

    # ---------------------------------------------------------------- scope
    A("## What this run is, and is not")
    A("")
    hs = manifest.get("haystacks", [])
    A(f"This is a **subset baseline**, not a published LoCoMo score. It covers "
      f"{len(hs)} of the dataset's 10 conversations ({', '.join(hs)}) and every "
      f"question attached to them.")
    A("")
    A("Ingest is the dominant cost and it cannot be subsetted: a question is "
      "unanswerable until its entire conversation is in the store, so sampling "
      "questions across all ten conversations would have cost the full ingest and "
      "bought only narrower intervals. Restricting to whole conversations buys a "
      "real reduction in cost; the price is that the intervals below are wide and "
      "several per-category cells rest on a few dozen questions. **Treat every "
      "figure here as a baseline to improve against, not as a claim.**")
    A("")
    A(f"**The denominator matters.** The file holds **{LOCOMO_FILE_QUESTIONS} "
      f"questions, not the {PUBLISHED_LOCOMO_QUESTIONS} usually quoted**; the "
      f"difference is exactly the {LOCOMO_ADVERSARIAL_QUESTIONS} adversarial "
      "questions, which the published figure excludes. Both denominators are "
      "reported below. A comparison against anyone's published LoCoMo number must "
      "use the excl-adversarial row.")
    A("")
    A("LoCoMo is licensed **CC BY-NC 4.0**. Internal measurement is squarely "
      "within that; whether these numbers may appear in commercial marketing is a "
      "live question for the project owner and is not settled here.")
    A("")

    # ---------------------------------------------------------------- headline
    A("## Accuracy")
    A("")
    A("Point estimate with a 95% Wilson interval in brackets. Every category the "
      "suite defines is printed, including the ones Engraphy is expected to lose.")
    A("")
    arms = sorted(agg)
    cats = sorted({c for a in arms for c in agg[a]["categories"]})
    A("| arm | n | overall | excl. adversarial | " + " | ".join(cats) + " |")
    A("|---|---:|---|---|" + "|".join(["---"] * len(cats)) + "|")
    for arm in arms:
        a = agg[arm]
        cells = [_acc_cell(a["categories"].get(c, {})) for c in cats]
        A(f"| `{arm}` | {a['overall']['n']} | {_acc_cell(a['overall'])} | "
          f"{_acc_cell(a['overall_excl_adversarial'])} | " + " | ".join(cells) + " |")
    A("")
    A("Per-category question counts:")
    A("")
    A("| arm | " + " | ".join(cats) + " |")
    A("|---|" + "|".join(["---:"] * len(cats)) + "|")
    for arm in arms:
        A(f"| `{arm}` | " + " | ".join(
            str(agg[arm]["categories"].get(c, {}).get("n", 0)) for c in cats) + " |")
    A("")

    # ------------------------------------------------------------ fair scoring
    A("## Fair scoring (strict / superset-normalized / partial-credit)")
    A("")
    A("The strict column above is the binary LLM judge. The failure analysis found "
      "it marks substantively-correct answers wrong in three mechanical ways: a "
      "**superset** (the answer contains the gold plus extra correct detail), a "
      "**multi-part** gold scored with no partial credit, and **format/date/number** "
      "differences of the same fact. A deterministic, LLM-free pass "
      "(`bench.core.scoring`) re-scores those cases. It is a conservative lower "
      "bound — no stemming, and it refuses any date match that names a different "
      "day or a different relational reference — so it under-counts rather than "
      "inventing flips. **Strict is not replaced; all three are shown.**")
    A("")
    A("| arm | strict | superset-normalized | partial-credit | rule flips |")
    A("|---|---|---|---|---:|")
    for arm in arms:
        f = _fair_scores(rows, arm)
        A(f"| `{arm}` | {_pct(f['strict'])} | {_pct(f['fair'])} "
          f"(+{100*(f['fair']-f['strict']):.1f}) | {_pct(f['partial'])} | {f['flips']} |")
    A("")
    A("`superset-normalized` = strict OR the rule pass judged it correct (superset "
      "covered / format-normalised). `partial-credit` gives a multi-part gold "
      "coverage k/N instead of 0/1. The wider effect (fuzzy paraphrase the rule "
      "won't touch) is left to the LLM judge under the updated rubric "
      "(`bench/prompts/judge.md`), which takes effect on the next graded run.")
    A("")

    # ------------------------------------------------------------ pack gap (headline)
    arm_vs = _find_arm(agg, "verbatim-starter", "search_only")
    arm_ls = _find_arm(agg, "llm-starter", "search_only")
    arm_lc = _find_arm(agg, "llm-conversational", "search_only")

    if arm_ls and arm_lc:
        A("## The pack gap — the headline")
        A("")
        A("Both arms use the LLM extractor and plain `search_only`; the only thing "
          "that differs is the **pack** — the ontology the extractor writes "
          "against. `starter` is the general-purpose pack (note/person/preference/"
          "commitment/project_ref); `conversational` is the purpose-built pack "
          "(person/thing/fact/event/preference/decision/opinion, with knows/works_at "
          "edges). Both are shipped packs, applied as a real deployment would, with "
          "ambient scopes stripped identically for haystack isolation (manifest "
          "`packs`). So this gap is the ontology's contribution, isolated.")
        A("")
        A(_pairwise_gap(agg, rows, manifest, arm_ls, arm_lc, "starter", "conversational"))
        A("")

    # ------------------------------------------------------------ extractor gap
    if arm_vs and arm_ls:
        A("## The extractor gap")
        A("")
        A("design/09 §Neutrality item 4: the verbatim floor and the LLM extractor "
          "(both on the starter pack) over the same questions. The floor is what "
          "the store does with **zero adapter judgment** — one turn, one untyped "
          "note, no attrs, no edges. The gap is the adapter's contribution.")
        A("")
        A(_pairwise_gap(agg, rows, manifest, arm_vs, arm_ls, "verbatim floor", "llm"))
        A("")
        A(_extractor_cost_table(agg, manifest, arm_vs, arm_ls))
        A("")

    # ------------------------------------------------------------ retrieval comparison
    rc = _retrieval_comparison(agg, rows, manifest, "llm-conversational")
    if rc:
        A("## Retrieval comparison (on the best pack)")
        A("")
        A("All three strategies read the **same** conversational-pack store, so a "
          "difference is the retrieval composition and nothing else. "
          "`search_then_traverse` is width-matched to `search_only` (same seed "
          "search, then a walk of every seed), so its delta isolates the graph "
          "contribution; `briefing_then_search` adds the relevance-floored briefing "
          "sections. If a richer strategy does not beat plain search, that is a real "
          "finding reported straight.")
        A("")
        A(rc)
        A("")

    # ---------------------------------------------------------------- tokens
    A("## Payload size and latency")
    A("")
    A("**The token headline metric is unavailable on this run and is reported as "
      "absent rather than estimated.** Counting Claude tokens needs the "
      "`count_tokens` API, which needs an API key; this harness routes Claude "
      "through the CLI on a subscription precisely so that no API credit is spent, "
      "and the CLI's own usage numbers measure Claude Code's injected system "
      "prompt (9–15k cache-creation tokens per call), not the memory payload. "
      "Gemini's tokenizer is available but counting a Claude payload with a Google "
      "tokenizer is the same objection that ruled out tiktoken. So the payload is "
      "reported in **bytes of the serialized envelope** — the exact bytes handed "
      "to the reader, hashed per row. Byte *ratios* between arms are "
      "tokenizer-independent, so comparisons survive; an absolute "
      "\"N tokens per answer\" claim does not and is not made.")
    A("")
    A("| arm | payload bytes p50 | p95 | mean | acc / 1k bytes | e2e s p50 | e2e s p95 |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for arm in arms:
        o = agg[arm]["overall"]
        A(f"| `{arm}` | {o['payload_bytes_p50']} | {o['payload_bytes_p95']} | "
          f"{o['payload_bytes_mean']} | {o['accuracy_per_1k_bytes']} | "
          f"{o['e2e_seconds_p50']} | {o['e2e_seconds_p95']} |")
    A("")
    A("End-to-end system latency is retrieval + reader. The judge is excluded — it "
      "is the harness, not the system — and its own p50 is reported in the JSON "
      "aggregate so the exclusion is visible rather than assumed. These latencies "
      "are dominated by the reader, which is a CLI subprocess spawn per question "
      "and is not how a deployed agent would call a model; the retrieval-only "
      "stage timings in `results.jsonl` are the figure comparable to Engraphy's own "
      "latency budget.")
    A("")

    # ---------------------------------------------------------------- judge
    cal = manifest.get("judge_calibration") or {}
    A("## Judge noise")
    A("")
    if cal.get("instability") is not None:
        inst = cal["instability"]
        A(f"The judge graded {cal['items_graded_twice']} stratified items twice and "
          f"disagreed with itself on {cal['disagreements']} of them — **instability "
          f"{100*inst:.1f}%**. Any accuracy delta below that is noise, and this "
          "report does not claim one.")
    else:
        A("Judge self-consistency was not measured on this run. Without it, no "
          "delta below a few points can be honestly distinguished from sampling "
          "noise, and none is claimed.")
    A("")
    provider = manifest.get("judge_provider", "gemini")
    if provider == "claude":
        A("**This run's judge is same-vendor (Claude), a deliberate deviation from "
          "design/09's cross-vendor rule** — taken to escape the Gemini free tier's "
          "per-model daily cap (measured at 500/day) that stopped the previous run "
          "half-graded. The full rationale is in the manifest's `judge_neutrality`. "
          "In short: the task is binary answer-matching against a supplied gold "
          "answer, which is low bias-risk for same-vendor grading — the judge is "
          "not scoring prose, only whether two short facts agree — and this is an "
          "internal baseline, not a publishable number. Cross-vendor judging "
          "remains the recommended posture for any eventual published result, and "
          "the Gemini path is retained for it. The judge still sees only the "
          "question, the gold answer and the candidate: never the envelope, the "
          "strategy or the extractor, so it cannot condition on the arm it grades.")
    else:
        A("The judge runs on Gemini — deliberately a different vendor from the "
          "system under test, so the 'graded its own homework' objection is removed "
          "structurally rather than by assertion. It sees only the question, the "
          "gold answer and the candidate answer: never the envelope, the strategy "
          "or the extractor, so it cannot condition on the arm it is grading.")
    A("")
    A("**Significance of the gaps rests on the instability figure above, and that "
      "is what this run set out to pin down.** The other half of design/09's "
      "calibration — agreement against a *human*-labelled subset — measures a "
      "different thing (whether the judge is *accurate*, not whether it is "
      "*stable*) and remains outstanding: it needs labels from an actual person, "
      "which the harness cannot manufacture. A stratified sample is emitted to "
      "`calibration_to_label.jsonl` for that purpose. Until it is labelled, the "
      "judge's absolute accuracy is unverified; its self-consistency is measured.")
    A("")

    # ---------------------------------------------------------------- ingest
    A("## Ingest")
    A("")
    ing = manifest.get("ingest", [])
    if ing:
        A("| haystack | extractor | drafts | inserted | merged | merge-linked | "
          "confirm-band | resolved→insert | resolved→merge | edges | "
          "supersede unresolved | s |")
        A("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for s in ing:
            A(f"| {s['haystack_id']} | {s.get('extractor','?')} | {s['drafts']} | "
              f"{s['inserted']} | {s['merged']} | {s.get('merged_linked', 0)} | "
              f"{_pct(s['confirm_band_rate'])} | "
              f"{s['resolved_to_insert']} | {s['resolved_to_merge']} | "
              f"{s['edges_attached']}/{s['edges_requested']} | "
              f"{s['supersede_target_unresolved']}/{s['supersede_attempted']} | "
              f"{s['total_seconds']:.0f} |")
        A("")
        A("Confirm-band hit rate is a first-class number under every policy "
          "(design/09 §ConfirmPolicy): it is the fraction of writes that a real "
          "agent would have had to come back and resolve before anything was "
          "stored. `supersede unresolved` is the honest measure of how much of the "
          "knowledge-update story is extraction rather than engine — a supersede "
          "whose target the extractor could not point at is downgraded to a plain "
          "write and counted, never credited as a supersede chain.")
    else:
        A("(no ingest statistics recorded)")
    A("")

    losses = _ingest_losses(ing)
    if losses:
        A("### Drafts that never reached the store")
        A("")
        A("A dropped draft is a missing memory, and downstream it is "
          "indistinguishable from weak retrieval — so every one is counted and "
          "attributed here rather than absorbed into the accuracy figure "
          "unexplained. These are **not** engine failures under test; they are "
          "writes the engine correctly refused, and the refusals are informative.")
        A("")
        A(losses)
        A("")

    # ---------------------------------------------------------------- cost
    ext = manifest.get("extrapolation")
    if ext:
        A("## Extrapolating to the full suite")
        A("")
        A(ext)
        A("")

    # ---------------------------------------------------------------- manifest
    A("## Manifest")
    A("")
    A("A result without its manifest is not publishable (design/09 §Neutrality "
      "item 1). Band thresholds below are **read back from the `config` table**, "
      "not asserted from the source, and model ids are the resolved ids the "
      "providers reported rather than the aliases requested.")
    A("")
    A("```json")
    A(json.dumps({k: v for k, v in manifest.items()
                  if k not in ("ingest", "extrapolation")}, indent=2, default=str))
    A("```")
    A("")
    A(f"Raw per-question rows: `runs/{run_id}/results.jsonl` "
      f"({len(rows)} rows). Each carries the retrieval envelope's SHA-256 and byte "
      "size, the answer, the verdict and its grader, and per-stage timings, so "
      "every aggregate above can be recomputed without trusting this file.")
    A("")
    return "\n".join(L)


_LOSS_NOTES = {
    "CheckViolation": (
        "the engine's attr-spec trigger rejected the draft's `attrs` (e.g. a "
        "`date`-typed attribute that was not a date). The extraction schema "
        "constrains which attribute *keys* may appear but not their value "
        "domains, so the database is the first thing to check them. "
        "`extract.validate_against_pack` exists and would catch these before the "
        "write; ingest does not currently call it. **Extraction-side loss.**"
    ),
    "ValidationError": (
        "a cross-type supersede — the extractor tried to replace a node of one "
        "type with a draft of another, which the engine refuses as a modeling "
        "error. **Extraction-side loss.**"
    ),
    "SupersedeUnresolvedBandError": (
        "**the engine refusing correctly, and the most informative loss here.** A "
        "supersede's replacement banded `needs_confirmation` against a node other "
        "than the one being superseded. QUESTIONS.md `supersede-nonclean-band` "
        "**resolved this in 2026-07 as option (a)** — refuse any non-INSERT band "
        "and roll the whole call back, because atomicity is supersede's entire "
        "contract. So this is designed behaviour, not a gap: no half-applied "
        "replacement, no orphaned edge, old node untouched. What the benchmark "
        "adds is evidence about the ruling's *cost assumption*. That ruling was "
        "taken on the basis that a third-node collision \"should be rare\"; this "
        "run hit it three times in a single 19-session conversation, so the "
        "caller round-trip it prices in is more frequent than the reasoning "
        "assumed. Worth revisiting the frequency estimate, not the decision. "
        "(Note also that the raised error's own message still says the plan "
        "\"does not define this case\" — stale text predating the resolution.)"
    ),
}


def _ingest_losses(ing: list[dict]) -> str:
    totals: dict[str, int] = {}
    drafts = 0
    samples: dict[str, str] = {}
    for row in ing:
        drafts += row.get("drafts", 0)
        for key, n in (row.get("write_errors") or {}).items():
            totals[key] = totals.get(key, 0) + n
        for s in row.get("write_error_samples") or []:
            samples.setdefault(str(s.get("error", "")), str(s.get("detail", ""))[:200])
    if not totals:
        return ""

    lost = sum(totals.values())
    L = [f"**{lost} of {drafts} drafts ({lost / drafts:.1%}) were refused at write "
         f"time.** By cause:", ""]
    L += ["| cause | n | what it means |", "|---|---:|---|"]
    for key, n in sorted(totals.items(), key=lambda kv: -kv[1]):
        base = key.split(":")[-1].split("(")[0]
        note = _LOSS_NOTES.get(base, "unclassified — see `write_error_samples` in the manifest.")
        prefix = "on `resolve_duplicate` — " if key.startswith("resolve:") else ""
        L.append(f"| `{key}` | {n} | {prefix}{note} |")
    if samples:
        L += ["", "One recorded example per cause:", ""]
        for key, detail in sorted(samples.items()):
            L.append(f"- `{key}` — {detail}")
    return "\n".join(L)


def _paired_scope_note(pr: list[dict]) -> str:
    """State exactly which conversations and categories the pairing covers.

    A paired subset that happens to sit inside one conversation is a much weaker
    claim than one spread across three, and the reader cannot tell which from an
    accuracy table. So it is spelled out rather than left to be inferred.
    """
    hs: dict[str, int] = {}
    cats: dict[str, int] = {}
    for r in pr:
        if r["arm"].startswith("verbatim"):
            hs[r["haystack_id"]] = hs.get(r["haystack_id"], 0) + 1
            cats[r["category"]] = cats.get(r["category"], 0) + 1
    parts = ", ".join(f"{k} ({v})" for k, v in sorted(hs.items()))
    cat_parts = ", ".join(f"{k} ({v})" for k, v in sorted(cats.items()))
    line = f"Paired questions come from: **{parts}**. Categories: {cat_parts}."
    if len(hs) == 1:
        line += (" Only one conversation is represented, so this comparison "
                 "cannot speak to variation between conversations — a real "
                 "limitation, not a formality.")
    missing = {"adversarial"} - set(cats)
    if missing:
        line += (" The adversarial category is absent from the pairing, so the "
                 "gap below is an excl-adversarial figure by construction.")
    return line


def paired_rows(rows: list[dict], arm_a: str, arm_b: str) -> list[dict]:
    """Rows for questions graded in BOTH arms.

    Required whenever the two arms are not graded to the same depth, which a
    quota-interrupted run guarantees. Comparing a fully-graded arm against a
    partially-graded one compares different question sets and the difference
    between them is mostly which questions happened to be reached, not which
    extractor is better. Pairing also removes question difficulty as a variable,
    so it is the better comparison even when both arms are complete.
    """
    a = {r["question_id"] for r in rows if r["arm"] == arm_a}
    b = {r["question_id"] for r in rows if r["arm"] == arm_b}
    shared = a & b
    return [r for r in rows if r["arm"] in (arm_a, arm_b) and r["question_id"] in shared]



def _fair_scores(rows: list[dict], arm: str) -> dict:
    """Strict / superset-normalized / partial-credit accuracy for one arm.

    Deterministic (no LLM): re-scores the arm's committed rows with
    `bench.core.scoring`. `strict` is the LLM judge as-is; `fair` flips a
    judge-wrong row only when the rule pass is confident it is correct;
    `partial` credits multi-part coverage. Adversarial rows keep their binary
    rule grade unchanged."""
    from bench.core.scoring import score_answer

    arm_rows = [r for r in rows if r["arm"] == arm]
    n = len(arm_rows)
    if not n:
        return {"strict": 0.0, "fair": 0.0, "partial": 0.0, "flips": 0}
    strict = fair = flips = 0
    partial = 0.0
    for r in arm_rows:
        correct = bool(r.get("correct"))
        strict += correct
        if r.get("abstain_expected"):
            fair += correct
            partial += 1.0 if correct else 0.0
            continue
        rs = score_answer(r.get("gold_answer") or "", r.get("answer") or "")
        f = correct or rs.strict_correct
        fair += f
        if f and not correct:
            flips += 1
        partial += 1.0 if correct else rs.partial
    return {"strict": strict / n, "fair": fair / n, "partial": partial / n, "flips": flips}


def _find_arm(agg: dict, extractor_label: str, strategy: str) -> str | None:
    """The arm id for one (extractor-pack, strategy), or None if it was not run.

    Arm ids are `<extractor>-<pack>/<strategy>/<policy>`, so a prefix match on
    the label and an exact match on the strategy segment locates an arm without
    hard-coding the policy.
    """
    for arm in agg:
        parts = arm.split("/")
        if len(parts) >= 2 and parts[0] == extractor_label and parts[1] == strategy:
            return arm
    return None


def _sig_line(delta: float, inst, *, what: str) -> str:
    """One sentence placing an overall gap against measured judge instability."""
    if inst is not None and abs(delta) < inst:
        return (f"The overall {what} is {100*delta:+.1f}pp, which is **smaller than the "
                f"measured judge instability of {100*inst:.1f}%** — not claimed as a result.")
    if inst is not None:
        return (f"The overall {what} is {100*delta:+.1f}pp against measured judge "
                f"instability of {100*inst:.1f}%. Per-category cells are smaller samples; "
                "read the Wilson brackets, not the point estimates.")
    return (f"The overall {what} is {100*delta:+.1f}pp. Judge instability was not measured, "
            "so no significance is claimed.")


def _pairwise_gap(agg: dict, rows: list[dict], manifest: dict,
                  arm_a: str, arm_b: str, name_a: str, name_b: str) -> str:
    """Per-category accuracy for two arms with Wilson intervals and the gap.

    Paired on the questions graded in BOTH arms when they are not graded to the
    same depth (a quota- or crash-interrupted run), because comparing a complete
    arm against a partial one compares different question sets. Pairing also
    removes question difficulty as a variable, so it is the better comparison
    even when both are complete.
    """
    n_a = agg[arm_a]["overall"]["n"]
    n_b = agg[arm_b]["overall"]["n"]
    pr = paired_rows(rows, arm_a, arm_b)
    paired_n = len({r["question_id"] for r in pr})
    use = aggregate(pr) if (pr and (paired_n < min(n_a, n_b) or n_a != n_b)) else agg
    ga, gb = use.get(arm_a, agg[arm_a]), use.get(arm_b, agg[arm_b])

    L: list[str] = []
    if use is not agg:
        L.append(f"**Paired on the {paired_n} questions graded in both arms** "
                 f"({n_a} {name_a}, {n_b} {name_b} graded in full); the arms are not "
                 "graded to the same depth, so a paired comparison is the honest one.")
        L.append("")
        L.append(_paired_scope_note(pr))
        L.append("")

    inst = (manifest.get("judge_calibration") or {}).get("instability")
    L += [f"| category | {name_a} | {name_b} | gap (pp) |", "|---|---|---|---:|"]
    cats = sorted(set(ga["categories"]) | set(gb["categories"]))
    for scope, label in (("overall", "**all questions**"),
                         ("overall_excl_adversarial", "**excl. adversarial**")):
        a, b = ga[scope], gb[scope]
        L.append(f"| {label} | {_acc_cell(a)} | {_acc_cell(b)} | {_gap_pp(a, b)} |")
    for c in cats:
        a = ga["categories"].get(c, {})
        b = gb["categories"].get(c, {})
        if not a.get("n") or not b.get("n"):
            continue
        L.append(f"| {c} | {_acc_cell(a)} | {_acc_cell(b)} | {_gap_pp(a, b)} |")
    L.append("")
    # Either arm may have an empty overall bucket on a partially-graded run; the
    # gap and its significance line are only meaningful when both have accuracy.
    ao, bo = ga["overall"], gb["overall"]
    if ao.get("accuracy") is None or bo.get("accuracy") is None:
        L.append("Overall gap not computable yet — one arm has no graded "
                 "(non-adversarial) rows. Grade both arms before reading a gap.")
    else:
        L.append(_sig_line(bo["accuracy"] - ao["accuracy"], inst,
                           what=f"gap ({name_b} − {name_a})"))
    return "\n".join(L)


def _extractor_cost_table(agg: dict, manifest: dict, arm_v: str, arm_l: str) -> str:
    """What the two extractors cost and leave in the store -- the axis accuracy
    hides. Reads the starter-pack ingest rows for each extractor."""
    ing = [r for r in manifest.get("ingest", []) if r.get("pack") in (None, "starter")]
    by_ext: dict[str, dict] = {}
    for row in ing:
        d = by_ext.setdefault(row.get("extractor", "?"), {})
        for k in ("drafts", "nodes_written", "edges_attached", "total_seconds",
                  "needs_confirmation"):
            d[k] = d.get(k, 0) + (row.get(k) or 0)
    if not (by_ext.get("verbatim") and by_ext.get("llm")):
        return ""
    v, m = by_ext["verbatim"], by_ext["llm"]
    vo, mo = agg[arm_v]["overall"], agg[arm_l]["overall"]
    L = ["Accuracy is one axis; what the extractors cost and store differs by more:",
         "", "| | verbatim (floor) | llm (starter) |", "|---|---:|---:|",
         f"| drafts written | {v['drafts']} | {m['drafts']} |",
         f"| **active nodes stored** | {v['nodes_written']} | {m['nodes_written']} |",
         f"| edges available to traverse | {v['edges_attached']} | {m['edges_attached']} |",
         f"| confirm-band round-trips | "
         f"{v['needs_confirmation']} ({v['needs_confirmation']/max(1,v['drafts']):.0%}) | "
         f"{m['needs_confirmation']} ({m['needs_confirmation']/max(1,m['drafts']):.0%}) |",
         f"| ingest wall-clock | {v['total_seconds']/60:.0f} min | {m['total_seconds']/60:.0f} min |",
         f"| retrieval payload bytes p50 | {vo['payload_bytes_p50']} | {mo['payload_bytes_p50']} |"]
    return "\n".join(L)


def _retrieval_comparison(agg: dict, rows: list[dict], manifest: dict,
                          extractor_label: str) -> str:
    """search_only vs search_then_traverse vs briefing_then_search on one store."""
    strategies = ["search_only", "search_then_traverse", "briefing_then_search"]
    arms = {s: _find_arm(agg, extractor_label, s) for s in strategies}
    arms = {s: a for s, a in arms.items() if a}
    if len(arms) < 2:
        return ""
    base = arms.get("search_only")
    inst = (manifest.get("judge_calibration") or {}).get("instability")

    L = ["| strategy | n | overall acc | multi-hop acc | payload bytes p50 |",
         "|---|---:|---|---|---:|"]
    for s in strategies:
        a = arms.get(s)
        if not a:
            continue
        o = agg[a]["overall"]
        mh = agg[a]["categories"].get("multi-hop", {})
        L.append(f"| `{s}` | {o['n']} | {_acc_cell(o)} | "
                 f"{_acc_cell(mh) if mh.get('n') else '—'} | {o['payload_bytes_p50']} |")
    L.append("")
    # Paired deltas vs search_only, on common graded questions.
    if base:
        for s in ("search_then_traverse", "briefing_then_search"):
            a = arms.get(s)
            if not a:
                continue
            pr = paired_rows(rows, base, a)
            if not pr:
                continue
            pa = aggregate(pr)
            av = pa.get(a, {}).get("overall", {}).get("accuracy")
            bv = pa.get(base, {}).get("overall", {}).get("accuracy")
            if av is None or bv is None:
                continue
            n = len({r["question_id"] for r in pr})
            L.append(f"- **{s} − search_only** (paired, n={n}): "
                     + _sig_line(av - bv, inst, what="delta").split("The overall ")[1])
    return "\n".join(L)


# --------------------------------------------------------------------- failures
_MODE_BLURB = {
    "extraction-miss": "the supporting fact appears to have never been stored — "
                       "not in the retrieved context and not found by a direct scope "
                       "probe for the gold answer. An extraction/ingest gap.",
    "retrieval-miss": "the fact is findable in the store (a scope probe surfaces it) "
                      "but this question's retrieval did not return it. A retrieval gap.",
    "reader-miss": "the supporting fact WAS in the retrieved context and the reader "
                   "still answered wrong. A reading/reasoning gap.",
    "reader-over-answered": "an adversarial question whose correct response is to "
                            "decline; the reader produced an answer instead.",
    "unattributed": "the gold-support heuristic could not tell (e.g. a bare number or "
                    "date with no content words to match). Read the context yourself.",
}


def render_failures(rows: list[dict]) -> str:
    """A human-readable failure log: incorrect questions grouped by arm, then
    failure mode, then category, each with its question, gold, answer, judge
    reason, and the retrieved context the reader actually saw.

    Diagnostics only. The failure-mode buckets rest on a keyword-overlap
    heuristic (`diagnostics.gold_support`) and are labelled as such -- the point
    is to route attention, not to be ground truth."""
    L: list[str] = []
    A = L.append
    wrong = [r for r in rows if not r.get("correct")]
    A("# Failure log")
    A("")
    A("Every incorrect answer, grouped by arm → failure mode → category. **The "
      "failure mode is a heuristic** (does the gold answer's supporting text appear "
      "in the retrieved context, and in a direct scope probe?), meant to route "
      "attention to extraction vs retrieval vs reading, not to be ground truth. "
      "Read the retrieved context under each item to check the call.")
    A("")
    A(f"{len(wrong)} incorrect of {len(rows)} graded.")
    A("")

    by_arm: dict[str, list[dict]] = defaultdict(list)
    for r in wrong:
        by_arm[r["arm"]].append(r)

    # Summary table first: where each arm's misses fall.
    modes = ["extraction-miss", "retrieval-miss", "reader-miss",
             "reader-over-answered", "unattributed"]
    A("## Where the misses fall")
    A("")
    A("| arm | wrong | " + " | ".join(modes) + " |")
    A("|---|---:|" + "|".join(["---:"] * len(modes)) + "|")
    for arm in sorted(by_arm):
        cnt = defaultdict(int)
        for r in by_arm[arm]:
            cnt[r.get("failure_mode", "unattributed")] += 1
        A(f"| `{arm}` | {len(by_arm[arm])} | "
          + " | ".join(str(cnt.get(m, 0)) for m in modes) + " |")
    A("")

    for arm in sorted(by_arm):
        A(f"## `{arm}`")
        A("")
        by_mode: dict[str, list[dict]] = defaultdict(list)
        for r in by_arm[arm]:
            by_mode[r.get("failure_mode", "unattributed")].append(r)
        for mode in modes:
            items = by_mode.get(mode, [])
            if not items:
                continue
            A(f"### {mode} ({len(items)})")
            A("")
            A(f"_{_MODE_BLURB.get(mode, '')}_")
            A("")
            for r in sorted(items, key=lambda x: x.get("category", "")):
                _render_one_failure(A, r)
    return "\n".join(L)


def _render_one_failure(A, r: dict) -> None:
    A(f"- **[{r.get('category')}]** {r.get('question_text', '(question text absent)')}")
    A(f"  - gold: `{r.get('gold_answer')}`")
    A(f"  - answer: `{(r.get('answer') or '').strip()[:300]}`")
    reason = (r.get("reason") or "").strip()
    if reason:
        A(f"  - judge: {reason[:240]}")
    gic = r.get("gold_in_context") or {}
    gis = r.get("gold_in_store") or {}
    A(f"  - gold-in-context: {gic.get('supported')} (frac {gic.get('fraction')}); "
      f"gold-in-store: {gis.get('supported')} (frac {gis.get('fraction')})")
    ctx = r.get("context") or []
    if not ctx:
        A("  - retrieved context: (empty — nothing was returned)")
    else:
        A(f"  - retrieved context ({len(ctx)} nodes):")
        for n in ctx[:6]:
            sc = n.get("score")
            sc_s = f"{sc:.3f}" if isinstance(sc, (int, float)) else "—"
            body = " ".join((n.get("body") or "").split())[:280]
            A(f"    - [{n.get('source')} {sc_s}] **{n.get('title')}** — {body}")
    A("")
