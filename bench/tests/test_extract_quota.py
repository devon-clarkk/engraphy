"""A quota stop during extraction must stop the run, not empty the store."""
import pytest

from bench.core.corpus import Session, Turn
from bench.core.extract import ExtractWindow, LLMExtractor
from bench.core.llm import LLMError, LLMResponse
from bench.core.providers import QuotaExhausted

PACK = {"node_types": {"fact": {"attrs": {"optional": {}, "closed": True}}}, "edge_types": {}}


def _window():
    turns = (Turn(speaker="A", text="I moved to Leeds in May.", turn_id="D1:1"),)
    return ExtractWindow(haystack_id="hs", session=Session(session_id="s1", turns=turns),
                         turns=turns, window_index=0)


class _Raises:
    model = "test"

    def __init__(self, exc):
        self.exc = exc

    def complete(self, *a, **k):
        raise self.exc


class _Empty:
    model = "test"

    def complete(self, *a, **k):
        return LLMResponse(text="", data={"nodes": [], "edges": []})


def test_quota_exhausted_propagates_rather_than_yielding_an_empty_window():
    ex = LLMExtractor(_Raises(QuotaExhausted("usage limit")), PACK)
    with pytest.raises(QuotaExhausted):
        ex.extract(_window())


def test_an_ordinary_llm_error_still_yields_an_empty_window():
    ex = LLMExtractor(_Raises(LLMError("transient")), PACK)
    assert ex.extract(_window()).nodes == ()


def test_an_empty_model_answer_is_still_an_empty_window():
    assert LLMExtractor(_Empty(), PACK).extract(_window()).nodes == ()


def test_llm_wide_is_a_separate_extractor_with_its_own_prompt_and_scope():
    """The wide prompt is opt-in: `llm` is untouched, `llm_wide` is its own arm."""
    from bench.core.run import EXTRACT_PROMPTS, EXTRACTORS, parse_arm
    from bench.core.space import scope_id_for

    assert EXTRACT_PROMPTS["llm"] == "extract.md"
    assert EXTRACT_PROMPTS["llm_wide"] == "extract-wide.md"
    assert set(EXTRACT_PROMPTS) | {"verbatim"} == set(EXTRACTORS)

    arm = parse_arm("llm_wide-conversational:search_only:k=25")
    assert arm.extractor == "llm_wide" and arm.pack == "conversational" and arm.k == 25
    assert arm.arm_id == "llm_wide-conversational/search_only/always_distinct/k25"
    # Its own scope, so a wide ingest can never land in the `llm` store.
    assert scope_id_for("conv-26:llm_wide") != scope_id_for("conv-26:llm")


def test_both_extraction_prompts_exist_and_differ():
    from bench.core.llm import load_prompt

    base, wide = load_prompt("extract.md"), load_prompt("extract-wide.md")
    assert base and wide and base != wide
    # The clauses the wide prompt exists to remove.
    assert "Prefer fewer, well-formed memories" in base
    assert "Prefer fewer, well-formed memories" not in wide
    assert "depends entirely on the immediate exchange" in base
    assert "depends entirely on the immediate exchange" not in wide


def test_the_arm_name_selects_the_prompt_the_extractor_actually_runs():
    """The registry naming `extract-wide.md` is not enough: an A/B is only an A/B
    if the built extractor loads that file. Both arms carrying `extract.md` would
    have produced two identical stores and a null result that looked measured."""
    from bench.core.run import _build_extractor

    pack = {"node_types": {"fact": {"attrs": {}}}, "edge_types": {}}
    assert _build_extractor("llm", pack).prompt_name == "extract.md"
    assert _build_extractor("llm_wide", pack).prompt_name == "extract-wide.md"
    assert (_build_extractor("llm", pack).system
            != _build_extractor("llm_wide", pack).system)


def test_the_manifest_records_which_prompt_each_arm_selected():
    """`prompt_hashes` lists every prompt in the tree, so it cannot tell an
    auditor whether the wide arm really ran the wide prompt. This can."""
    from bench.core.run import extract_prompts_manifest, parse_arm

    arms = [parse_arm("llm-conversational:search_only:k=25"),
            parse_arm("llm_wide-conversational:search_only:k=25")]
    recorded = extract_prompts_manifest(arms)
    names = {entry["prompt"] for entry in recorded.values()}
    assert names == {"extract.md", "extract-wide.md"}
    assert len({entry["sha256"] for entry in recorded.values()}) == 2
