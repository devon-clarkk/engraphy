"""Reader inference-stance selector (skills/answer-discipline.md §The inference
stance). Hermetic: pure text assembly, no LLM, no DB."""

from __future__ import annotations

import pytest

from bench.core.answer import (
    READER_DEFAULT_STANCE,
    READER_STANCES,
    Reader,
    build_reader_system,
)
from bench.core.llm import StubLLM


def test_default_stance_is_grounded():
    assert READER_DEFAULT_STANCE == "grounded"
    assert set(READER_STANCES) == {"strict", "grounded"}


def test_grounded_enables_inference_strict_declines():
    g_sys, g_man = build_reader_system("grounded")
    s_sys, s_man = build_reader_system("strict")
    assert g_man["inference_stance"] == "grounded"
    assert s_man["inference_stance"] == "strict"
    # grounded permits a sourced inference as an answer; strict says do not infer.
    assert "grounded stance" in g_sys and "supports a reasonable" in g_sys
    assert "do not infer" in s_sys
    # both still carry the machine-scorable INSUFFICIENT contract and the skill.
    assert "INSUFFICIENT" in g_sys and "INSUFFICIENT" in s_sys
    assert "Answering from memory" in g_sys  # the shipped skill body


def test_stance_recorded_and_threaded_through_reader():
    r = Reader(StubLLM(), stance="strict")
    assert r.stance == "strict"
    assert r.system_manifest["inference_stance"] == "strict"
    assert "do not infer" in r.system


def test_unknown_stance_rejected():
    with pytest.raises(ValueError):
        build_reader_system("permissive")
