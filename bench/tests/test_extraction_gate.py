"""The extraction gate applies the pre-registered rule, including when it closes.

A gate that only ever opens is decoration. These check both answers, and check
that the noise floor is what the lever has to clear, since that is the part the
rule added on 2026-10-01.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
GATE = REPO / "scripts" / "extraction_gate.py"


def space(extractor: str, held: dict[str, int], memories: int = 400) -> dict:
    scorable = len(held)
    return {"extractor": extractor, "memories": memories, "memories_by_scope": {},
            "scorable_questions": scorable,
            "all_evidence_held": sum(held.values()),
            "all_evidence_held_pct": round(100 * sum(held.values()) / scorable, 1),
            "some_evidence_held_pct": None, "gap_set": {}, "gap_set_total": {},
            "all_evidence_held_by_question": held}


def run(tmp_path: pathlib.Path, spaces: dict) -> subprocess.CompletedProcess:
    report = {"spaces": spaces, "questions": 100, "haystacks": ["conv-26"]}
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return subprocess.run([sys.executable, str(GATE), str(path)], capture_output=True,
                          text=True, check=False)


def held(n: int, true_up_to: int) -> dict[str, int]:
    return {f"q{i}": (1 if i < true_up_to else 0) for i in range(n)}


def test_a_gain_inside_the_replicate_spread_closes_the_gate(tmp_path):
    """The replicate differs from the base by as much as the lever does, which is
    the case the rule exists for."""
    result = run(tmp_path, {
        "bench-ab-conversational": space("llm", held(100, 60)),
        "bench-ab-rep-conversational": space("llm", held(100, 64)),
        "bench-ab-wide-conversational": space("llm_wide", held(100, 63), memories=520),
    })
    assert result.returncode == 3, result.stdout
    assert "GATE CLOSED" in result.stdout
    assert "noise floor" in result.stdout


def test_a_large_consistent_gain_opens_the_gate(tmp_path):
    result = run(tmp_path, {
        "bench-ab-conversational": space("llm", held(100, 50)),
        "bench-ab-rep-conversational": space("llm", held(100, 51)),
        "bench-ab-wide-conversational": space("llm_wide", held(100, 80), memories=600),
    })
    assert result.returncode == 0, result.stdout
    assert "GATE OPEN" in result.stdout


def test_without_a_replicate_the_gate_is_undecided_rather_than_open(tmp_path):
    result = run(tmp_path, {
        "bench-ab-conversational": space("llm", held(100, 50)),
        "bench-ab-wide-conversational": space("llm_wide", held(100, 80)),
    })
    assert result.returncode == 2, result.stdout


def test_the_exact_test_is_two_sided_and_handles_no_discordance():
    sys.path.insert(0, str(REPO / "scripts"))
    from extraction_gate import mcnemar_exact

    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(10, 0) < 0.01
    assert mcnemar_exact(5, 5) == 1.0


def test_two_extractors_sharing_one_space_are_reported_separately():
    """Two arms of one run share a space and are separated by scope, so a report
    keyed on the space alone kept only the last arm and could not be compared.
    Seen 2026-10-02: coverage ran over all 389 seen questions and the gate could
    not read it."""
    source = (REPO / "bench" / "extraction_coverage.py").read_text(encoding="utf-8")
    assert 'report["spaces"][f"{space}#{extractor}"]' in source
    assert '"space": space,' in source
