"""A usage cap during ingest stops the run; any other model failure does not.

The distinction decides whether a figure is trustworthy. A failed window is one
gap in one conversation, and the harness records it and carries on. A usage cap
fails every remaining window the same way, so swallowing it records
conversations as ingested that hold nothing, and the run then scores its
questions against a store missing whole conversations. Seen live on 2026-09-30:
two conversations ingested `0 drafts -> 0 nodes` in 31 seconds each while the
Claude CLI was capped.
"""

from __future__ import annotations

import pytest

from bench.core.corpus import Session, Turn
from bench.core.extract import ExtractWindow, LLMExtractor, NodeDraft
from bench.core.ingest import LLMAdjudicate
from bench.core.llm import LLMError, LLMResponse
from bench.core.providers import AuthExpired, QuotaExhausted

PACK = {"node_types": {"fact": {"attrs": {}}}, "edge_types": {}}


class Failing:
    """A client that fails every call with one exception."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def complete(self, *a, **kw):
        self.calls += 1
        raise self.exc


class Fine:
    def complete(self, *a, **kw):
        return LLMResponse(text="", data={"nodes": [], "edges": []}, model="fake")


def window() -> ExtractWindow:
    turns = (Turn(speaker="Ada", text="I moved to Paris.", turn_id="D1:1"),)
    return ExtractWindow(haystack_id="conv-1",
                         session=Session(session_id="session_1", turns=turns,
                                         timestamp="1:56 pm on 8 May, 2023"),
                         turns=turns, window_index=0)


def draft() -> NodeDraft:
    return NodeDraft(local_id="n1", node_type="fact", title="Ada moved to Paris",
                     body="Ada moved to Paris.")


def test_a_cap_stops_extraction_instead_of_returning_an_empty_window():
    client = Failing(QuotaExhausted("usage limit"))
    with pytest.raises(QuotaExhausted):
        LLMExtractor(client, PACK).extract(window())
    assert client.calls == 1


def test_any_other_failure_still_yields_an_empty_window():
    result = LLMExtractor(Failing(LLMError("one bad window")), PACK).extract(window())
    assert result.nodes == () and result.edges == ()


def test_a_cap_stops_adjudication_instead_of_deciding_distinct():
    with pytest.raises(QuotaExhausted):
        LLMAdjudicate(Failing(QuotaExhausted("usage limit"))).decide({"candidates": []}, draft())


def test_any_other_adjudication_failure_still_falls_back():
    adjudicator = LLMAdjudicate(Failing(LLMError("adjudicator hiccup")))
    decision = adjudicator.decide({"candidates": []}, draft())
    assert decision.resolution == "distinct" and adjudicator.fallbacks == 1


def test_a_clean_extraction_still_returns_a_result():
    assert LLMExtractor(Fine(), PACK).extract(window()).nodes == ()


def test_an_expired_session_also_stops_extraction():
    """Same reasoning as a cap, and worse: every later window fails identically,
    and no amount of waiting fixes it."""
    with pytest.raises(AuthExpired):
        LLMExtractor(Failing(AuthExpired("no usable credentials")), PACK).extract(window())


def test_an_expired_session_also_stops_adjudication():
    with pytest.raises(AuthExpired):
        LLMAdjudicate(Failing(AuthExpired("no usable credentials"))).decide(
            {"candidates": []}, draft())
