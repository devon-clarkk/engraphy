"""Apply the pre-registered extraction gate to a coverage report.

    python scripts/extraction_gate.py runs/extract-ab-seen/coverage.json

The rule is fixed in engraphy-benchmarks
`analysis/2026-09-30-settling-run-preregistration.md`, addendum 2026-10-01: the
answer and judge pass on the two arms is paid for only if the coverage gain of
`llm_wide` over `llm` both

1. exceeds the difference between the two `llm` ingests, which is the noise
   floor this lever has to clear, measured rather than assumed, and
2. reaches p < 0.05 on an exact McNemar test over the paired per-question
   coverage outcomes.

Exit 0 opens the gate, exit 3 closes it, exit 2 means the report cannot decide
it. Nothing here chooses the threshold: it reads the rule written before the
numbers existed and reports what the numbers do against it.
"""
from __future__ import annotations

import json
import math
import pathlib
import sys

ALPHA = 0.05


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar on the discordant pairs.

    b and c are the counts that one store holds and the other does not. Under no
    effect each discordant pair is a coin flip, so the p value is the two-sided
    binomial tail. Returns 1.0 with no discordant pairs, which is the honest
    reading: two identical stores are no evidence of a difference.
    """
    n = b + c
    if n == 0:
        return 1.0
    extreme = max(b, c)
    tail = sum(math.comb(n, k) for k in range(extreme, n + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def paired(left: dict, right: dict) -> tuple[int, int, int]:
    """(both, only left, only right) over the questions both spaces scored."""
    shared = sorted(set(left) & set(right))
    both = sum(1 for q in shared if left[q] and right[q])
    only_left = sum(1 for q in shared if left[q] and not right[q])
    only_right = sum(1 for q in shared if right[q] and not left[q])
    return both, only_left, only_right


def rate(space: dict) -> float:
    return space["all_evidence_held_pct"]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    report = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    spaces = report["spaces"]

    def find(extractor: str, *, replicate: bool) -> dict | None:
        for key, body in spaces.items():
            if body["extractor"] != extractor:
                continue
            if ("-rep-" in body.get("space", key)) == replicate:
                return body
        return None

    base = find("llm", replicate=False)
    rep = find("llm", replicate=True)
    wide = find("llm_wide", replicate=False)
    if base is None or wide is None:
        print("cannot decide: the report needs an llm space and an llm_wide space")
        return 2

    outcomes = "all_evidence_held_by_question"
    print(f"questions scored        {base['scorable_questions']}")
    print(f"llm        coverage     {rate(base)}%  ({base['memories']} memories)")
    if rep is not None:
        print(f"llm (rep)  coverage     {rate(rep)}%  ({rep['memories']} memories)")
    print(f"llm_wide   coverage     {rate(wide)}%  ({wide['memories']} memories)")

    if rep is None:
        print()
        print("no replicate ingest in this report, so the noise floor is unmeasured;")
        print("the pre-registered rule needs it. Treating the gate as undecided.")
        return 2

    floor = abs(rate(rep) - rate(base))
    _, base_only, wide_only = paired(base[outcomes], wide[outcomes])
    gain = rate(wide) - rate(base)
    p = mcnemar_exact(base_only, wide_only)
    _, a_only, b_only = paired(base[outcomes], rep[outcomes])
    p_floor = mcnemar_exact(a_only, b_only)

    print()
    print(f"noise floor, llm to llm {floor:+.1f} points, discordant {a_only}/{b_only}, "
          f"p {p_floor:.4f}")
    print(f"lever, llm to llm_wide  {gain:+.1f} points, discordant {base_only}/{wide_only}, "
          f"p {p:.4f}")
    print(f"store size change       {wide['memories'] - base['memories']:+d} memories "
          f"({100 * (wide['memories'] - base['memories']) / base['memories']:+.0f}%)")
    print()

    clears_floor = gain > floor
    significant = p < ALPHA
    if clears_floor and significant:
        print(f"GATE OPEN: the gain clears the {floor:.1f} point noise floor and "
              f"p {p:.4f} < {ALPHA}.")
        print("Pay for the answer and judge pass on both arms.")
        return 0
    reasons = []
    if not clears_floor:
        reasons.append(f"the gain ({gain:+.1f}) does not clear the noise floor ({floor:.1f})")
    if not significant:
        reasons.append(f"p {p:.4f} is not below {ALPHA}")
    print("GATE CLOSED: " + ", and ".join(reasons) + ".")
    print("A prompt that does not change what is stored cannot change what is")
    print("answered, so the lever is dropped here and reported with this number.")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
