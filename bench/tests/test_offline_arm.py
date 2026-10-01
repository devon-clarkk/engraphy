"""An offline pass reads one named arm of a run, never a mixture of two.

Run A of the settling measurement carries the combined engine and the shipped
extraction prompt on one ingest, so it holds two envelopes per question. A pass
that flattened them would grade a mixture of two configurations and report it
under one name.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from bench import offline

WIDE = "llm_wide-conversational/search_only/always_distinct/k25"
CONTROL = "llm-conversational/search_only/always_distinct/k25"


def make_run(tmp_path: pathlib.Path, arms: list[str]) -> pathlib.Path:
    (tmp_path / "manifest.json").write_text(json.dumps({
        "dataset": {"path": "locomo10.json"}, "benchmark": "locomo",
        "haystacks": ["conv-26"], "judge_provider": "claude"}), encoding="utf-8")
    rows, envelopes = [], []
    for arm in arms:
        rows.append({"arm": arm, "question_id": "conv-26:q0", "answer": f"from {arm}",
                     "correct": True})
        envelopes.append({"arm": arm, "question_id": "conv-26:q0",
                          "envelope": {"nodes": [{"title": arm}]}})
    (tmp_path / "results.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (tmp_path / "envelopes.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in envelopes), encoding="utf-8")
    return tmp_path


def test_a_single_arm_run_needs_no_naming(tmp_path):
    src = offline.load_source(make_run(tmp_path, [WIDE]))
    assert src.arm == WIDE
    assert src.envelopes["conv-26:q0"]["nodes"][0]["title"] == WIDE


def test_a_two_arm_run_refuses_to_guess(tmp_path):
    with pytest.raises(SystemExit) as caught:
        offline.load_source(make_run(tmp_path, [WIDE, CONTROL]))
    assert "--arm" in str(caught.value)


def test_the_named_arm_is_the_one_read(tmp_path):
    run = make_run(tmp_path, [WIDE, CONTROL])
    for arm in (WIDE, CONTROL):
        src = offline.load_source(run, arm=arm)
        assert src.arm == arm
        assert src.envelopes["conv-26:q0"]["nodes"][0]["title"] == arm
        assert src.source_row("conv-26:q0")["answer"] == f"from {arm}"


def test_an_arm_that_is_not_there_is_named_in_the_error(tmp_path):
    with pytest.raises(SystemExit) as caught:
        offline.load_source(make_run(tmp_path, [WIDE]), arm=CONTROL)
    assert CONTROL in str(caught.value)
