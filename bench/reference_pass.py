"""The matched-convention figure: a completed run, read and graded under the reference harness conventions.

    python -m bench.reference_pass --run-dir runs/<run-id>

Engraphy's primary LoCoMo figure is the strict one the run itself produces. This
pass produces a second figure beside it, labelled as measured under the reference
harness conventions for comparability: every non-adversarial question the run
answered is read again from the run's saved retrieval envelope, under the
reference answer prompt, and graded under the reference judge. The conventions and
every difference from the reference are in `bench/core/reference.py` and in the
pass's manifest.

Output goes to `runs/<run-id>/reference/`. It resumes from its checkpoint, and it
runs under the supervisor like a run does:

    python -m bench.supervise --run-dir runs/<run-id>/reference \\
        --log runs/<run-id>/reference.supervise.log -- \\
        python -m bench.reference_pass --run-dir runs/<run-id>
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from bench import offline
from bench.core import run as harness
from bench.core.providers import ClaudeCLIClient
from bench.core.reference import ReferenceJudge, ReferenceReader, conventions_manifest, \
    reference_date

SUFFIX = "/reference-conventions"


def main() -> int:
    ap = argparse.ArgumentParser(prog="bench.reference_pass")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--out", help="default: <run-dir>/reference")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--judge-concurrency", type=int, default=4)
    ap.add_argument("--judge-passes", type=int, default=1,
                    help="the reference grades each answer once; that is the default")
    ap.add_argument("--limit", type=int, default=0, help="only the first N questions (a check)")
    args = ap.parse_args()

    src = offline.load_source(args.run_dir)
    if src.manifest.get("judge_provider") != "claude":
        raise SystemExit("the matched-convention pass uses the run's Claude judge route")
    out = pathlib.Path(args.out) if args.out else src.dir / "reference"
    ck = harness.Checkpoint(out)
    reader_model = harness.ROLE_MODELS["reader"]["model"]
    # The run's Claude judge model; ROLE_MODELS["judge"] names the Gemini default.
    judge_model = harness.CLAUDE_JUDGE_MODEL
    arm = src.arm + SUFFIX

    ref_dates = {hid: reference_date(h) for hid, h in src.haystacks.items()}
    questions = [q for qid, q in sorted(src.questions.items())
                 if not q.abstain_expected and qid in src.envelopes]
    if args.limit:
        questions = questions[:args.limit]
    missing = [qid for qid, q in src.questions.items()
               if not q.abstain_expected and qid not in src.envelopes]

    manifest = {
        "instrument": "bench.reference_pass",
        "source_run": src.manifest.get("run_id"),
        "source_engine_git_sha": src.manifest.get("engine_git_sha"),
        "engine_git_sha": harness._git_sha(),
        "arm": arm,
        "dataset": src.manifest.get("dataset"),
        "haystacks": src.manifest.get("haystacks"),
        "retrieval_configs": src.manifest.get("retrieval_configs"),
        "reference_dates": ref_dates,
        "role_models": {"reader": {"provider": "claude-cli", "model": reader_model},
                        "judge": {"provider": "claude-cli", "model": judge_model}},
        "conventions": conventions_manifest(judge_passes=args.judge_passes),
        "questions": len(questions),
        "questions_without_saved_envelope": sorted(missing),
        "limit": args.limit or None,
    }
    print(f"reference pass over {src.manifest.get('run_id')} -> {out}")
    print(f"  {len(questions)} non-adversarial questions, arm {arm}")

    todo = [{"arm": arm, "question_id": q.question_id} for q in questions]

    def read_one(item: dict) -> dict:
        q = src.questions[item["question_id"]]
        env = src.envelopes[q.question_id]
        ans = ReferenceReader(ClaudeCLIClient(model=reader_model)).read(
            q, env, ref_dates[q.haystack_id])
        size, digest = offline.envelope_digest(env)
        return {"arm": arm, "question_id": q.question_id, "haystack_id": q.haystack_id,
                "category": q.category, "abstain_expected": False,
                "answer": ans.text, "reader_output": ans.raw, "reader_seconds": ans.seconds,
                "reader_model": ans.model, "envelope_bytes": size, "envelope_sha256": digest,
                "error": ans.error}

    def grade_one(row: dict):
        q = src.questions[row["question_id"]]
        return ReferenceJudge(ClaudeCLIClient(model=judge_model)).grade_majority(
            q, row.get("answer") or "", passes=args.judge_passes)

    reason = offline.read_pass(ck, todo, read_one, concurrency=args.concurrency)
    if reason:
        return offline.stop(ck, manifest, reason)
    reason = offline.judge_pass(ck, grade_one, concurrency=args.judge_concurrency)
    if reason:
        return offline.stop(ck, manifest, reason)
    agg = offline.finish(ck, manifest,
                         "LoCoMo under the reference harness conventions, for comparability")
    for name, a in agg.items():
        b = a["overall_excl_adversarial"]
        print(f"  {name}: {b.get('correct')}/{b.get('n')} = {b.get('accuracy')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
