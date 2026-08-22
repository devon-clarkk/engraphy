"""Failure-attribution diagnostics (added 2026-07-23). Hermetic: no DB, no LLM.

These pin the buckets that route Devon's attention in failures.md, so a change
that silently reshuffled extraction/retrieval/reader misses would be caught.
"""

from __future__ import annotations

from bench.core.diagnostics import (
    attribute_failure,
    compact_context,
    context_text,
    gold_support,
)


def test_gold_support_detects_present_and_absent():
    text = "Caroline adopted a greyhound named Pepper last spring."
    assert gold_support("a greyhound", text)["supported"] is True
    assert gold_support("a poodle from the shelter", text)["supported"] is False


def test_gold_support_abstains_on_contentless_gold():
    # A bare number/date has no content words: the heuristic must say None, not guess.
    assert gold_support("20", "she sleeps twenty hours a day")["supported"] is None
    assert gold_support("2023", "it happened in the spring")["supported"] is None


def test_attribution_three_way_split():
    sup = {"supported": True}
    no = {"supported": False}
    # In context, wrong answer -> the reader had it and missed.
    assert attribute_failure(correct=False, abstain_expected=False,
                             gold_in_context=sup, gold_in_store=sup) == "reader-miss"
    # Not in context but in the store -> retrieval failed to surface it.
    assert attribute_failure(correct=False, abstain_expected=False,
                             gold_in_context=no, gold_in_store=sup) == "retrieval-miss"
    # Nowhere -> never stored.
    assert attribute_failure(correct=False, abstain_expected=False,
                             gold_in_context=no, gold_in_store=no) == "extraction-miss"


def test_attribution_edge_cases():
    assert attribute_failure(correct=True, abstain_expected=False,
                             gold_in_context=None, gold_in_store=None) == "correct"
    # Adversarial + wrong = the reader answered when it should have declined.
    assert attribute_failure(correct=False, abstain_expected=True,
                             gold_in_context=None, gold_in_store=None) == "reader-over-answered"
    # Heuristic could not tell (numeric gold) -> unattributed, never guessed.
    none = {"supported": None}
    assert attribute_failure(correct=False, abstain_expected=False,
                             gold_in_context=none, gold_in_store=none) == "unattributed"


def test_compact_context_flattens_all_envelope_shapes():
    search = {"results": [{"node": {"id": "n1", "title": "T1", "body": "b1"},
                           "score": 0.9, "similarity": 0.9}]}
    assert [n["id"] for n in compact_context(search)] == ["n1"]

    traverse = {"results": [{"node": {"id": "n1", "title": "T1", "body": "b1"}, "score": 0.9}],
                "traversed": [{"id": "n2", "title": "T2", "body": "b2"}]}
    cc = compact_context(traverse)
    assert [n["id"] for n in cc] == ["n1", "n2"]
    assert cc[1]["source"] == "traverse"

    briefing = {"briefing": {"sections": [{"name": "due", "nodes": [
        {"id": "n3", "title": "T3", "body": "b3"}]}]},
        "search": {"results": [{"node": {"id": "n1", "title": "T1", "body": "b1"}, "score": 0.8}]}}
    ids = [n["id"] for n in compact_context(briefing)]
    assert "n3" in ids and "n1" in ids


def test_compact_context_dedups_and_caps():
    env = {"results": [{"node": {"id": f"n{i}", "title": f"T{i}", "body": "b"}, "score": 0.5}
                       for i in range(30)]
           + [{"node": {"id": "n0", "title": "dup", "body": "b"}, "score": 0.5}]}
    cc = compact_context(env)
    ids = [n["id"] for n in cc]
    assert len(ids) == len(set(ids)), "must dedup by node id"
    assert len(cc) <= 12, "must cap node count"


def test_context_text_includes_titles_and_bodies():
    cc = [{"title": "Pepper the dog", "body": "a greyhound"}]
    t = context_text(cc)
    assert "Pepper" in t and "greyhound" in t
