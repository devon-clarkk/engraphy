"""Retrieval recall at several search widths, over an existing run's store. No LLM.

    python -m bench.k_sweep --run-dir runs/<run-id> --ks 10,15,20,25,30,40,50 \\
        --out runs/<run-id>/k-sweep [--save-envelopes 20,30]

This is the instrument for choosing the `search_only` width. It measures the
retrieval stack alone, over the store the run's extractor actually wrote, so no
reader or judge noise enters the choice, and the final accuracy figure is never
the thing being swept.

## Recall, measured against the dataset's own evidence

LoCoMo names, for each question, the dialogue turns that carry its answer. An
Engraphy memory written by the conversational extractor quotes the turns it rests
on verbatim in its body. A question's **evidence recall** at width k is the share
of its evidence turns whose text appears in at least one of the k memories
returned. It needs no judgment and no gold-answer wording, so it cannot reward a
width for surfacing text that merely resembles the answer.

A turn counts as present when the first 60 characters of its normalised text
(lower case, punctuation and repeated spaces removed) occur in a returned
memory's normalised body. The prefix tolerates a quote the extractor shortened at
its end. Questions with no evidence, the adversarial ones, are not measured.

The harness's own `gold_in_context` heuristic is reported beside it, as the same
signal a run records.

## Stability

Search updates recall statistics on the rows it returns. Before trusting a sweep,
the width-10 results are compared with the envelopes the run saved: if the same
memories come back in the same order, repeated searches have not moved the ranking.

Nothing here writes a result row or reads a question's text into the output: the
per-question file carries ids, categories and numbers only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys
from collections import defaultdict

from psycopg_pool import AsyncConnectionPool

from bench.core import run as harness
from bench.core.answer import render_envelope
from bench.core.diagnostics import MAX_CONTEXT_NODES
from bench.core.meter import Meter
from bench.core.retrieve import SearchOnly
from engraphy.core import embedding

PREFIX = 60


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(text).lower()).split())


def evidence_turns(dataset: pathlib.Path) -> dict[tuple[str, str], str]:
    """`(sample_id, dia_id)` -> normalised turn text, straight from the dataset file."""
    data = json.loads(dataset.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], str] = {}
    for sample in data:
        conv = sample["conversation"]
        for key, turns in conv.items():
            if key.startswith("session_") and isinstance(turns, list):
                for turn in turns:
                    if turn.get("dia_id") and turn.get("text"):
                        out[(sample["sample_id"], turn["dia_id"])] = _norm(turn["text"])
    return out


def _dia_ids(evidence: tuple[str, ...]) -> list[str]:
    # A few LoCoMo entries pack several ids into one string ("D8:6; D9:17").
    ids = []
    for e in evidence:
        ids += re.findall(r"D\d+:\d+", e)
    return ids


def recall(envelope: dict, turn_keys: list[str]) -> float | None:
    if not turn_keys:
        return None
    bodies = [_norm((r.get("node") or r).get("body") or "") for r in envelope.get("results") or []]
    hit = sum(1 for key in turn_keys if any(key in b for b in bodies))
    return hit / len(turn_keys)


async def sweep(args) -> dict:
    run_dir = args.run_dir if args.run_dir.is_absolute() else harness.REPO / args.run_dir
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    dataset = harness.REPO / "datasets" / manifest["dataset"]["path"]
    corpus = harness.LOADERS["locomo"]().load(dataset)
    wanted = set(manifest["haystacks"])
    arm = harness.parse_arm(args.arm)
    questions = [q for q in corpus.questions
                 if q.haystack_id in wanted and not q.abstain_expected]
    space_id = manifest["packs"][arm.pack]["space_id"]
    arm_space = harness.ArmSpace(space_id, "bench", arm.extractor)
    turns = evidence_turns(dataset)

    saved10: dict[str, list[str]] = {}
    env_path = run_dir / "envelopes.jsonl"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            saved10[row["question_id"]] = [
                (r.get("node") or r).get("id") for r in row["envelope"].get("results") or []]

    ks = sorted({int(k) for k in args.ks.split(",")})
    save = {int(k) for k in args.save_envelopes.split(",") if k}
    args.out.mkdir(parents=True, exist_ok=True)
    per_q = (args.out / "per_question.jsonl").open("w", encoding="utf-8", newline="\n")
    sinks = {k: (args.out / f"envelopes-k{k}.jsonl").open("w", encoding="utf-8", newline="\n")
             for k in save}

    embedding.load_model()
    agg: dict[int, dict] = {k: defaultdict(list) for k in ks}
    stable = unstable = 0
    pool = AsyncConnectionPool(harness.APP_DB, open=False, min_size=1, max_size=2)
    await pool.open()
    try:
        for i, q in enumerate(questions, 1):
            keys = [turns[(q.haystack_id, d)][:PREFIX] for d in _dia_ids(q.evidence)
                    if (q.haystack_id, d) in turns]
            for k in ks:
                got = await SearchOnly(limit=k).retrieve(pool, arm_space, q, Meter())
                env = got.envelope
                r = recall(env, keys)
                compact = harness.compact_context(env, max_nodes=max(MAX_CONTEXT_NODES, k))
                ctx = harness.gold_support(q.gold_answer, harness.context_text(compact))
                ctx_frac = ctx.get("fraction") if isinstance(ctx, dict) else ctx
                row = {"question_id": q.question_id, "category": q.category, "k": k,
                       "evidence_turns": len(keys), "evidence_recall": r,
                       "all_evidence": None if r is None else r == 1.0,
                       "gold_in_context_frac": ctx_frac,
                       "returned": len(env.get("results") or []),
                       "rendered_chars": len(render_envelope(env))}
                per_q.write(json.dumps(row) + "\n")
                for bucket in ("all", q.category):
                    a = agg[k]
                    if r is not None:
                        a[f"{bucket}:recall"].append(r)
                        a[f"{bucket}:all_evidence"].append(1.0 if r == 1.0 else 0.0)
                    if isinstance(ctx_frac, (int, float)):
                        a[f"{bucket}:gold_in_context"].append(float(ctx_frac))
                    a[f"{bucket}:rendered_chars"].append(row["rendered_chars"])
                if k == 10 and q.question_id in saved10:
                    ids = [(x.get("node") or x).get("id") for x in env.get("results") or []]
                    if ids == saved10[q.question_id]:
                        stable += 1
                    else:
                        unstable += 1
                if k in sinks:
                    sinks[k].write(json.dumps({"arm": f"{arm.arm_id}/k{k}",
                                               "question_id": q.question_id,
                                               "envelope": env}) + "\n")
            if i % 50 == 0:
                print(f"  {i}/{len(questions)}", file=sys.stderr, flush=True)
    finally:
        await pool.close()
        per_q.close()
        for s in sinks.values():
            s.close()

    def mean(xs):
        return round(sum(xs) / len(xs), 4) if xs else None

    summary = {
        "run_dir": str(args.run_dir), "arm": arm.arm_id, "space_id": space_id,
        "questions_measured": len(questions),
        "evidence_turn_match": f"normalised {PREFIX}-character prefix in a returned body",
        "k10_matches_saved_envelopes": {"same_order": stable, "different": unstable},
        "by_k": {},
    }
    for k in ks:
        a = agg[k]
        buckets = sorted({key.split(":")[0] for key in a})
        summary["by_k"][str(k)] = {
            b: {"evidence_recall": mean(a[f"{b}:recall"]),
                "all_evidence": mean(a[f"{b}:all_evidence"]),
                "gold_in_context": mean(a[f"{b}:gold_in_context"]),
                "rendered_chars_mean": mean(a[f"{b}:rendered_chars"]),
                "n": len(a[f"{b}:recall"])}
            for b in buckets}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n",
                                           encoding="utf-8", newline="\n")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(prog="bench.k_sweep")
    ap.add_argument("--run-dir", type=pathlib.Path, required=True,
                    help="a completed run whose spaces were kept")
    ap.add_argument("--arm", default="llm-conversational:search_only")
    ap.add_argument("--ks", default="10,15,20,25,30,40,50")
    ap.add_argument("--save-envelopes", default="", help="widths whose envelopes to keep")
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()
    summary = asyncio.run(sweep(args))
    for k, buckets in summary["by_k"].items():
        a = buckets["all"]
        print(f"k={k:>3}  evidence recall {a['evidence_recall']}  all evidence "
              f"{a['all_evidence']}  gold_in_context {a['gold_in_context']}  "
              f"chars {a['rendered_chars_mean']}")
    print(f"k=10 against saved envelopes: {summary['k10_matches_saved_envelopes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
