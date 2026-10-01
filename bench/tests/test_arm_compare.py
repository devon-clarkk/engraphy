"""The arm comparison is paired, and keeps adversarial separate from the rest.

Two arms can hold the same accuracy and still differ, or differ in rate while
agreeing on every question. Both cases decide differently under the rule, so
both are checked here on fixtures rather than waited for in a run.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOL = REPO / "scripts" / "arm_compare.py"


def row(arm: str, qid: str, *, correct: bool, adversarial: bool = False,
        category: str = "single-hop") -> dict:
    return {"arm": arm, "question_id": qid, "category": category,
            "abstain_expected": adversarial, "correct": correct}


def run(tmp_path: pathlib.Path, rows: list[dict]) -> dict:
    results = tmp_path / "results.jsonl"
    results.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = tmp_path / "compare.json"
    done = subprocess.run([sys.executable, str(TOOL), str(results), "--out", str(out)],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_equal_rates_that_disagree_per_question_are_not_called_identical(tmp_path):
    """Both arms score 2 of 4. A rate comparison sees nothing; the pairing sees
    four discordant questions, which is what a reshuffle looks like."""
    rows = [row("a", "q1", correct=True), row("a", "q2", correct=True),
            row("a", "q3", correct=False), row("a", "q4", correct=False),
            row("b", "q1", correct=False), row("b", "q2", correct=False),
            row("b", "q3", correct=True), row("b", "q4", correct=True)]
    report = run(tmp_path, rows)
    assert report["arms"]["a"]["non_adversarial"]["pct"] == 50.0
    assert report["arms"]["b"]["non_adversarial"]["pct"] == 50.0
    pair = report["pairs"]["a vs b"]["non_adversarial"]
    assert (pair["only_first"], pair["only_second"]) == (2, 2)
    assert pair["p_mcnemar_exact"] == 1.0


def test_adversarial_questions_are_counted_apart_from_the_rest(tmp_path):
    rows = [row("a", "q1", correct=True), row("a", "adv1", correct=True, adversarial=True),
            row("b", "q1", correct=True), row("b", "adv1", correct=False, adversarial=True)]
    report = run(tmp_path, rows)
    assert report["arms"]["a"]["adversarial"] == {"correct": 1, "n": 1, "pct": 100.0}
    assert report["arms"]["b"]["adversarial"] == {"correct": 0, "n": 1, "pct": 0.0}
    assert report["arms"]["a"]["non_adversarial"]["n"] == 1
    assert report["pairs"]["a vs b"]["adversarial"]["only_first"] == 1
    assert report["pairs"]["a vs b"]["non_adversarial"]["only_first"] == 0


def test_categories_are_reported_per_arm(tmp_path):
    rows = [row("a", "q1", correct=True, category="multi-hop"),
            row("a", "q2", correct=False, category="temporal"),
            row("b", "q1", correct=True, category="multi-hop"),
            row("b", "q2", correct=True, category="temporal")]
    report = run(tmp_path, rows)
    assert report["arms"]["a"]["by_category"]["temporal"]["pct"] == 0.0
    assert report["arms"]["b"]["by_category"]["temporal"]["pct"] == 100.0
