"""Compare two arms of one run as paired per-question outcomes.

    python scripts/arm_compare.py runs/extract-ab-seen/results.jsonl
    python scripts/arm_compare.py runs/<run>/results.jsonl --out runs/<run>/arm-compare.json

Two arms of the same run saw the same questions, the same reader and the same
judge, so the comparison that decides anything is paired: which questions one arm
gets and the other does not. Rates alone cannot separate a real gain from a
reshuffle of the same count.

Reported per arm: non-adversarial accuracy overall and by category, and the
adversarial rate, which is where a wider store carries its risk. Reported per
pair: the discordant counts and an exact McNemar p value, non-adversarial and
adversarial separately, because the pre-registered rule asks two different
questions of them. A gain has to beat the between-run spread; a loss of
adversarial ground only has to be significant to block promotion.

Decides nothing on its own. The rule is in engraphy-benchmarks
`analysis/2026-09-30-settling-run-preregistration.md`.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from extraction_gate import mcnemar_exact


def load(path: pathlib.Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def outcomes(rows: list[dict], *, adversarial: bool) -> dict[str, int]:
    """question_id -> 1 if graded correct, for one slice of one arm."""
    return {r["question_id"]: (1 if r.get("correct") else 0)
            for r in rows if bool(r.get("abstain_expected")) is adversarial}


def rate(marks: dict[str, int]) -> tuple[int, int, float | None]:
    n = len(marks)
    hits = sum(marks.values())
    return hits, n, (round(100 * hits / n, 2) if n else None)


def by_category(rows: list[dict]) -> dict[str, tuple[int, int, float | None]]:
    buckets: dict[str, dict[str, int]] = collections.defaultdict(dict)
    for r in rows:
        if r.get("abstain_expected"):
            continue
        buckets[r.get("category") or "unknown"][r["question_id"]] = 1 if r.get("correct") else 0
    return {cat: rate(marks) for cat, marks in sorted(buckets.items())}


def compare(left: dict[str, int], right: dict[str, int]) -> dict:
    shared = sorted(set(left) & set(right))
    only_left = sum(1 for q in shared if left[q] and not right[q])
    only_right = sum(1 for q in shared if right[q] and not left[q])
    return {"paired_questions": len(shared),
            "only_first": only_left, "only_second": only_right,
            "p_mcnemar_exact": round(mcnemar_exact(only_left, only_right), 6)}


def main() -> int:
    ap = argparse.ArgumentParser(prog="arm_compare")
    ap.add_argument("results", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path)
    args = ap.parse_args()

    rows = load(args.results)
    arms = sorted({r["arm"] for r in rows})
    per_arm = {arm: [r for r in rows if r["arm"] == arm] for arm in arms}

    report: dict = {"results": str(args.results), "arms": {}, "pairs": {}}
    for arm, arm_rows in per_arm.items():
        non_adv = outcomes(arm_rows, adversarial=False)
        adv = outcomes(arm_rows, adversarial=True)
        hits, n, pct = rate(non_adv)
        a_hits, a_n, a_pct = rate(adv)
        report["arms"][arm] = {
            "non_adversarial": {"correct": hits, "n": n, "pct": pct},
            "adversarial": {"correct": a_hits, "n": a_n, "pct": a_pct},
            "by_category": {cat: {"correct": c, "n": t, "pct": p}
                            for cat, (c, t, p) in by_category(arm_rows).items()},
        }
        print(f"{arm}")
        print(f"  non-adversarial  {hits}/{n} = {pct}%")
        print(f"  adversarial      {a_hits}/{a_n} = {a_pct}%")
        for cat, (c, t, p) in by_category(arm_rows).items():
            print(f"    {cat:28} {c}/{t} = {p}%")

    for i, first in enumerate(arms):
        for second in arms[i + 1:]:
            key = f"{first} vs {second}"
            non_adv = compare(outcomes(per_arm[first], adversarial=False),
                              outcomes(per_arm[second], adversarial=False))
            adv = compare(outcomes(per_arm[first], adversarial=True),
                          outcomes(per_arm[second], adversarial=True))
            report["pairs"][key] = {"non_adversarial": non_adv, "adversarial": adv}
            print()
            print(f"{key}")
            print(f"  non-adversarial  paired {non_adv['paired_questions']}, "
                  f"only first {non_adv['only_first']}, only second {non_adv['only_second']}, "
                  f"p {non_adv['p_mcnemar_exact']}")
            print(f"  adversarial      paired {adv['paired_questions']}, "
                  f"only first {adv['only_first']}, only second {adv['only_second']}, "
                  f"p {adv['p_mcnemar_exact']}")

    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
        print()
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
