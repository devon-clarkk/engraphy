"""The LLM seam, the typed extractor, and the confirm-band adjudicator.

All hermetic: every test drives `StubLLM`, so the suite needs no API key, no
network, and not even the `bench` extra installed. That is the point of the
seam — the roles that make the harness expensive to run must still be cheap to
test.
"""

from __future__ import annotations

import pytest

from bench.core.corpus import Session, Turn
from bench.core.extract import (
    ExtractWindow,
    LLMExtractor,
    NodeDraft,
    build_extraction_schema,
    validate_against_pack,
)
from bench.core.ingest import ConfirmDecision, LLMAdjudicate
from bench.core.llm import ROLE_MODELS, LLMError, StubLLM, load_prompt, prompt_hash
from bench.core.space import load_bench_pack


@pytest.fixture(scope="module")
def pack():
    return load_bench_pack()


def _window(texts, prior=(), hid="conv-a", sid="s1"):
    turns = tuple(Turn(speaker="A", text=t, turn_id=f"{sid}:{i}") for i, t in enumerate(texts))
    return ExtractWindow(
        haystack_id=hid,
        session=Session(sid, turns),
        turns=turns,
        window_index=0,
        prior_titles=tuple(prior),
    )


# --------------------------------------------------------------------------
# The seam
# --------------------------------------------------------------------------


def test_every_role_has_a_recorded_model_configuration():
    """The pinning guard, rewritten for the mixed-vendor setup.

    Its purpose was never "always Opus" -- it was that a cheaper model must not
    silently move the headline number. Under two free providers the invariant
    becomes: every role names a model, that model is recorded, and no role
    defaults to something unrecorded. Asserting one fixed id would now fail for
    the right configuration and pass for a bad one.
    """
    roles = ROLE_MODELS
    assert set(roles) == {"extractor", "reader", "judge", "adjudicator"}
    for role, cfg in roles.items():
        assert cfg["provider"] in {"claude-cli", "gemini"}, role
        assert cfg["model"], f"{role} has no recorded model"

    # Cross-vendor judging: the judge must not be the same vendor as the system
    # under test, or the harness grades its own homework.
    assert roles["judge"]["provider"] != roles["extractor"]["provider"]


def test_judge_model_is_on_the_free_tier():
    """Pro models left the Gemini free tier in April 2026; a Pro id here would
    fail at request time, not at configuration time."""
    from bench.core.providers import GEMINI_FREE_MODELS

    assert ROLE_MODELS["judge"]["model"] in GEMINI_FREE_MODELS


def test_llm_module_never_sets_temperature():
    """`temperature`, `top_p` and `top_k` were removed on Opus 4.8 and return
    HTTP 400. The benchmark reflex 'pin temperature to 0 for reproducibility'
    does not degrade here — it fails the call outright."""
    import pathlib

    src = (pathlib.Path(__file__).parents[1] / "core" / "llm.py").read_text(encoding="utf-8")
    code = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
    body = "\n".join(code)
    for banned in ('"temperature"', "temperature=", '"top_p"', "top_p=", '"top_k"', "top_k="):
        assert banned not in body, f"llm.py sets {banned}, which 400s on Opus 4.8"


def test_prompt_hashes_are_stable_and_change_with_content(tmp_path, monkeypatch):
    first = prompt_hash("extract.md")
    assert first == prompt_hash("extract.md")
    assert first.startswith("sha256:")

    import bench.core.llm as llm_mod

    monkeypatch.setattr(llm_mod, "PROMPTS_DIR", tmp_path)
    (tmp_path / "p.md").write_text("one", encoding="utf-8")
    a = prompt_hash("p.md")
    (tmp_path / "p.md").write_text("two", encoding="utf-8")
    assert prompt_hash("p.md") != a, "an edited prompt must change its manifest hash"


def test_missing_prompt_fails_loudly():
    with pytest.raises(LLMError, match="prompt file not found"):
        load_prompt("no_such_prompt.md")


def test_stub_raises_when_exhausted():
    stub = StubLLM(responses=["one"])
    stub.complete("s", "u")
    with pytest.raises(LLMError, match="exhausted"):
        stub.complete("s", "u")


# --------------------------------------------------------------------------
# Schema built from the pack
# --------------------------------------------------------------------------


def test_schema_enumerates_the_packs_own_types(pack):
    schema = build_extraction_schema(pack)
    node_enum = schema["properties"]["memories"]["items"]["properties"]["node_type"]["enum"]
    assert set(node_enum) == set(pack["node_types"])
    edge_enum = schema["properties"]["edges"]["items"]["properties"]["edge_type"]["enum"]
    assert set(edge_enum) == set(pack["edge_types"])


def test_schema_closes_every_object(pack):
    """Strict structured output requires additionalProperties: false — and it is
    what stops the extractor inventing an attribute the write path would then
    reject."""
    schema = build_extraction_schema(pack)
    mem = schema["properties"]["memories"]["items"]
    assert mem["additionalProperties"] is False
    assert mem["properties"]["attrs"]["additionalProperties"] is False
    assert schema["additionalProperties"] is False


def test_schema_maps_enum_attrs_to_enums(pack):
    schema = build_extraction_schema(pack)
    strength = schema["properties"]["memories"]["items"]["properties"]["attrs"]["properties"][
        "strength"
    ]
    assert sorted(strength["enum"]) == ["hard", "soft"]


def test_schema_build_refuses_a_pack_with_no_types():
    from bench.core.extract import ExtractionError

    with pytest.raises(ExtractionError, match="no node_types"):
        build_extraction_schema({"node_types": {}})


def test_validate_against_pack_uses_the_engines_own_interpreter(pack):
    """The schema constrains which attr keys may appear; only the engine's
    validator knows which are *required* for the chosen type."""
    ok = NodeDraft("l1", "preference", "Prefers aisle seats on flights", "Aisle, always.",
                   attrs={"strength": "hard"})
    assert validate_against_pack(ok, pack) == []

    missing = NodeDraft("l2", "preference", "Prefers aisle seats on flights", "Aisle.", attrs={})
    assert validate_against_pack(missing, pack), "a missing required attr must be reported"

    unknown = NodeDraft("l3", "note", "A plain note about sailing", "Body.", attrs={"nope": "x"})
    assert validate_against_pack(unknown, pack), "a closed spec must reject an unknown key"


# --------------------------------------------------------------------------
# LLMExtractor
# --------------------------------------------------------------------------


def test_extractor_produces_drafts_and_edges(pack):
    stub = StubLLM(responses=[{
        "memories": [
            {"local_id": "p1", "node_type": "person", "title": "Melanie, Caroline's sister",
             "body": "Melanie is Caroline's younger sister.", "attrs": {"relation": "sister"}},
            {"local_id": "e1", "node_type": "event", "title": "Caroline adopted a greyhound",
             "body": "Caroline adopted a greyhound named Pepper.", "attrs": {}},
        ],
        "edges": [{"src_local_id": "e1", "dst_local_id": "p1", "edge_type": "involves"}],
    }])
    result = LLMExtractor(stub, pack).extract(_window(["I adopted a greyhound.", "Lovely!"]))

    assert [d.node_type for d in result.nodes] == ["person", "event"]
    assert len(result.edges) == 1
    assert result.edges[0].edge_type == "involves"
    # Edge endpoints are namespaced to the window, matching the draft local_ids.
    assert result.edges[0].src_local_id == result.nodes[1].local_id
    assert result.edges[0].dst_local_id == result.nodes[0].local_id
    # Token usage flows to the manifest's ingest-side accounting.
    assert result.input_tokens > 0


def test_extractor_emits_edges_flag_is_true(pack):
    assert LLMExtractor(StubLLM(), pack).emits_edges is True


def test_extractor_drops_unusable_memories_not_the_whole_window(pack):
    """One malformed memory should cost that memory, not the other nine."""
    stub = StubLLM(responses=[{
        "memories": [
            {"local_id": "a", "node_type": "not_a_type", "title": "Bad type here", "body": "x"},
            {"local_id": "b", "node_type": "note", "title": "hi", "body": "too short a title"},
            {"local_id": "c", "node_type": "note", "title": "A perfectly good memory", "body": "y"},
        ],
        "edges": [],
    }])
    result = LLMExtractor(stub, pack).extract(_window(["text"]))
    assert [d.title for d in result.nodes] == ["A perfectly good memory"]


def test_extractor_drops_edges_pointing_at_dropped_memories(pack):
    """An edge to a memory that never survived has nothing to attach to."""
    stub = StubLLM(responses=[{
        "memories": [
            {"local_id": "good", "node_type": "note", "title": "A surviving memory", "body": "y"},
        ],
        "edges": [
            {"src_local_id": "good", "dst_local_id": "ghost", "edge_type": "relates_to"},
            {"src_local_id": "good", "dst_local_id": "good", "edge_type": "relates_to"},
        ],
    }])
    result = LLMExtractor(stub, pack).extract(_window(["text"]))
    assert result.edges == (), "edges to missing or self endpoints must be dropped"


def test_extractor_survives_an_llm_failure(pack):
    """A failed window must not abort a 500-haystack run."""
    result = LLMExtractor(StubLLM(responses=[LLMError("boom")]), pack).extract(_window(["t"]))
    assert result.nodes == () and result.edges == ()


def test_extractor_prompt_never_mentions_a_benchmark_or_question(pack):
    """design/09 §Neutrality: an extraction prompt that mentions a benchmark, a
    category, or a question format is a neutrality breach."""
    text = load_prompt("extract.md").lower()
    # Suite names and QA-task framing, not the bare word "question" -- the
    # prompt legitimately says "questions with no answer yet" as an instruction
    # about what NOT to extract, which is ontology guidance, not leakage. What
    # would be a breach is the extractor knowing it is being benchmarked, or
    # being told to optimize for a downstream question.
    for banned in (
        "locomo",
        "longmemeval",
        "benchmark",
        "evaluation",
        "the question",
        "answer the question",
        "multi-hop",
        "single-hop",
        "knowledge-update",
        "temporal reasoning",
    ):
        assert banned not in text, f"extraction prompt mentions {banned!r}"


def test_extractor_shows_prior_titles_but_not_prior_bodies(pack):
    stub = StubLLM(responses=[{"memories": [], "edges": []}])
    LLMExtractor(stub, pack).extract(_window(["now"], prior=("An earlier memory title",)))
    sent = stub.calls[0]["user"]
    assert "An earlier memory title" in sent
    assert "now" in sent


def test_extractor_renders_the_live_ontology_into_the_prompt(pack):
    """Rendered per call, so changing the pack cannot desynchronize from the
    instructions the extractor is given."""
    stub = StubLLM(responses=[{"memories": [], "edges": []}])
    LLMExtractor(stub, pack).extract(_window(["t"]))
    sent = stub.calls[0]["user"]
    for node_type in pack["node_types"]:
        assert node_type in sent


def test_extractor_carries_supersedes_title_through(pack):
    stub = StubLLM(responses=[{
        "memories": [{
            "local_id": "n", "node_type": "note", "title": "Works at the bakery now",
            "body": "Left the hardware store.", "supersedes_title": "Works at the hardware store",
        }],
        "edges": [],
    }])
    (draft,) = LLMExtractor(stub, pack).extract(_window(["t"])).nodes
    assert draft.supersedes_local_id == "Works at the hardware store"


# --------------------------------------------------------------------------
# LLMAdjudicate
# --------------------------------------------------------------------------


def _envelope(*ids):
    return {
        "outcome": "needs_confirmation",
        "pending_id": "p1",
        "candidates": [
            {"id": i, "similarity": 0.87, "title": f"Candidate {i}", "body": "b"} for i in ids
        ],
    }


def _draft():
    return NodeDraft("l1", "note", "An incoming memory title", "Incoming body.")


def test_adjudicate_merge_names_an_offered_candidate():
    stub = StubLLM(responses=[{"resolution": "merge", "merge_into": "n1", "reason": "same fact"}])
    d = LLMAdjudicate(stub).decide(_envelope("n1", "n2"), _draft())
    assert d.resolution == "merge"
    assert d.merge_into == "n1"


def test_adjudicate_distinct_is_passed_through():
    stub = StubLLM(responses=[{"resolution": "distinct", "reason": "different property"}])
    d = LLMAdjudicate(stub).decide(_envelope("n1"), _draft())
    assert d.resolution == "distinct"
    assert d.merge_into is None


def test_adjudicate_falls_back_to_distinct_on_failure():
    """The two failure directions are not symmetric: a spurious distinct costs a
    duplicate row, a spurious merge destroys a fact."""
    policy = LLMAdjudicate(StubLLM(responses=[LLMError("timeout")]))
    d = policy.decide(_envelope("n1"), _draft())
    assert d.resolution == "distinct"
    assert policy.fallbacks == 1


def test_adjudicate_refuses_a_merge_into_an_unoffered_candidate():
    """Guessing the nearest candidate would be inventing a fact-destroying merge
    on the model's behalf."""
    stub = StubLLM(responses=[{"resolution": "merge", "merge_into": "hallucinated", "reason": "r"}])
    policy = LLMAdjudicate(stub)
    d = policy.decide(_envelope("n1"), _draft())
    assert d.resolution == "distinct"
    assert policy.fallbacks == 1


def test_adjudicate_records_decisions_for_audit():
    stub = StubLLM(responses=[{"resolution": "distinct", "reason": "different subject"}])
    policy = LLMAdjudicate(stub)
    policy.decide(_envelope("n1"), _draft())
    assert policy.decisions == [{"resolution": "distinct", "reason": "different subject"}]


def test_adjudicate_prompt_leans_toward_distinct_on_ties():
    text = load_prompt("adjudicate.md").lower()
    assert "distinct" in text and "destroys a fact" in text


def test_adjudicate_sees_candidates_and_the_incoming_memory():
    stub = StubLLM(responses=[{"resolution": "distinct", "reason": "r"}])
    LLMAdjudicate(stub).decide(_envelope("n1"), _draft())
    sent = stub.calls[0]["user"]
    assert "An incoming memory title" in sent
    assert "n1" in sent


def test_merge_decision_still_requires_a_target():
    with pytest.raises(ValueError, match="merge_into"):
        ConfirmDecision(resolution="merge")
