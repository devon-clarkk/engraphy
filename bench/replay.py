"""Reader validation: re-read a completed run's saved envelopes under a given reader.

    python -m bench.replay --run-dir runs/<run-id> --name verify \\
        --select declined,adversarial
    python -m bench.replay --run-dir runs/<run-id> --name baseline \\
        --select declined,adversarial --contract direct --skill-path <old skill>

Every question is read from the envelope the run saved, so a candidate reader and
a baseline reader see identical memory and differ only in the reader. Answers are
graded by the unchanged strict judge; adversarial questions by the abstention rule.

**Groups**, taken from the run's own results: `declined` (non-adversarial, the run
declined), `correct` and `wrong` (non-adversarial, the run answered), and
`adversarial`. `all` is every group.

**`--sample N`** keeps at most N questions per group, the first N in the order of
a hash of the question id, so the same sample is drawn on every machine and rerun.

**`--reuse-verdicts`** grades an answer whose text is identical to the run's own
answer with the run's verdict instead of calling the judge again. It saves judge
calls on a reader that mostly reproduces the run, and the report counts how many
verdicts were reused.

`--envelopes` reads a different envelope file for the same run, such as the wider
envelopes `bench.k_sweep --save-envelopes` writes.

Output goes to `runs/<run-id>/replay/<name>/` and resumes from its checkpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter

from bench import offline
from bench.core import run as harness
from bench.core.answer import READER_CONTRACTS, READER_STANCES, Reader, is_abstention
from bench.core.judge import Verdict
from bench.core.providers import ClaudeCLIClient
from bench.core.retrieve import Retrieval
from bench.core.score import grade_abstention

GROUPS = ("declined", "correct", "wrong", "adversarial")


def group_of(row: dict) -> str:
    if row.get("abstain_expected"):
        return "adversarial"
    if is_abstention(row.get("answer") or ""):
        return "declined"
    return "correct" if row.get("correct") else "wrong"


def main() -> int:
    ap = argparse.ArgumentParser(prog="bench.replay")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--select", default="all")
    ap.add_argument("--envelopes")
    ap.add_argument("--contract", default="verify", choices=READER_CONTRACTS)
    ap.add_argument("--stance", default="grounded", choices=READER_STANCES)
    ap.add_argument("--skill-path")
    ap.add_argument("--judge-passes", type=int, default=3)
    ap.add_argument("--reuse-verdicts", action="store_true")
    ap.add_argument("--sample", type=int, default=0, help="at most N per group, by id hash")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--judge-concurrency", type=int, default=4)
    args = ap.parse_args()

    src = offline.load_source(args.run_dir, args.envelopes)
    wanted = set(GROUPS) if args.select == "all" else set(args.select.split(","))
    unknown = wanted - set(GROUPS)
    if unknown:
        raise SystemExit(f"--select: unknown group(s) {sorted(unknown)}")
    out = src.dir / "replay" / args.name
    ck = harness.Checkpoint(out)
    reader_model = harness.ROLE_MODELS["reader"]["model"]
    probe = Reader(None, stance=args.stance, contract=args.contract, skill_path=args.skill_path)
    arm = f"{src.arm}/replay-{args.name}"

    items = []
    for qid in sorted(src.questions):
        base = src.source_row(qid)
        if base is None or qid not in src.envelopes:
            continue
        group = group_of(base)
        if group in wanted:
            items.append({"arm": arm, "question_id": qid, "group": group})
    if args.sample:
        items = sample_items(items, args.sample)

    manifest = {
        "instrument": "bench.replay",
        "name": args.name,
        "source_run": src.manifest.get("run_id"),
        "source_engine_git_sha": src.manifest.get("engine_git_sha"),
        "arm": arm,
        "envelopes": src.envelope_source,
        "select": sorted(wanted),
        "reader": {**probe.system_manifest, "model": reader_model,
                   "system_sha256": hashlib.sha256(probe.system.encode("utf-8")).hexdigest()},
        "judge": {"prompt": "bench/prompts/judge.md", "passes": args.judge_passes,
                  "model": harness.CLAUDE_JUDGE_MODEL,
                  "reuse_verdicts_for_identical_answers": args.reuse_verdicts},
        "groups": dict(Counter(i["group"] for i in items)),
        "sample_per_group": args.sample or None,
    }
    print(f"replay {args.name} over {src.manifest.get('run_id')} -> {out}")
    print(f"  {len(items)} questions: {manifest['groups']}")

    def read_one(item: dict) -> dict:
        q = src.questions[item["question_id"]]
        env = src.envelopes[q.question_id]
        reader = Reader(ClaudeCLIClient(model=reader_model), stance=args.stance,
                        contract=args.contract, skill_path=args.skill_path)
        ans = reader.read(q, Retrieval(envelope=env, strategy="replay"))
        base = src.source_row(q.question_id)
        return {"arm": arm, "question_id": q.question_id, "haystack_id": q.haystack_id,
                "category": q.category, "abstain_expected": q.abstain_expected,
                "group": item["group"], "source_answer": base.get("answer"),
                "source_correct": base.get("correct"), **ans.as_dict()}

    def grade_one(row: dict) -> Verdict:
        q = src.questions[row["question_id"]]
        answer = row.get("answer") or ""
        if q.abstain_expected:
            return grade_abstention(answer)
        if args.reuse_verdicts and answer.strip() == (row.get("source_answer") or "").strip():
            return Verdict(correct=bool(row.get("source_correct")),
                           reason="identical answer text; the run's verdict", graded_by="reused")
        return harness._build_judge("claude").grade_majority(q, answer, passes=args.judge_passes)

    reason = offline.read_pass(ck, items, read_one, concurrency=args.concurrency)
    if reason:
        return offline.stop(ck, manifest, reason)
    reason = offline.judge_pass(ck, grade_one, concurrency=args.judge_concurrency)
    if reason:
        return offline.stop(ck, manifest, reason)

    offline.finish(ck, manifest, f"Replay `{args.name}`")
    results = harness.load_rows(ck.path("results.jsonl"))
    transitions = transition_table(results)
    manifest = json.loads(ck.path("manifest.json").read_text(encoding="utf-8"))
    manifest["transitions"] = transitions
    ck.path("manifest.json").write_text(json.dumps(manifest, indent=2, default=str),
                                        encoding="utf-8")
    print(json.dumps(transitions, indent=2))
    return 0


def sample_items(items: list[dict], per_group: int) -> list[dict]:
    """At most `per_group` items per group, chosen by a hash of the question id."""
    kept: list[dict] = []
    for group in GROUPS:
        members = sorted((i for i in items if i["group"] == group),
                         key=lambda i: hashlib.sha256(i["question_id"].encode()).hexdigest())
        kept += members[:per_group]
    return kept


def transition_table(results: list[dict]) -> dict:
    """Per group: how verdicts and declines moved against the run's own answers."""
    out: dict[str, dict] = {}
    for group in GROUPS:
        rows = [r for r in results if r.get("group") == group]
        if not rows:
            continue
        out[group] = {
            "n": len(rows),
            "correct_before": sum(1 for r in rows if r.get("source_correct")),
            "correct_after": sum(1 for r in rows if r.get("correct")),
            "right_to_wrong": sum(1 for r in rows if r.get("source_correct") and not r.get("correct")),
            "wrong_to_right": sum(1 for r in rows if not r.get("source_correct") and r.get("correct")),
            "declined_before": sum(1 for r in rows if is_abstention(r.get("source_answer") or "")),
            "declined_after": sum(1 for r in rows if r.get("abstained")),
            "verdicts_reused": sum(1 for r in rows if r.get("graded_by") == "reused"),
        }
    return out


if __name__ == "__main__":
    sys.exit(main())
