"""Jev rerank experiment: reorder arithmetic, scoring summary and the client
stub's failure modes. Hermetic: the HTTP call is replaced, never made."""

from __future__ import annotations

import pytest

from bench.jev import client as jev_client
from bench.jev.client import (
    JevClient,
    JevConfigError,
    JevResponseError,
    LexicalScorer,
    _parse_score,
)
from bench.jev.rerank import by_score, rrf_with_base
from bench.jev_rerank_recall import hit, summarize

BASE = ["a", "b", "c", "d"]


def test_by_score_sorts_desc_and_ties_keep_search_order():
    assert by_score(BASE, {"a": 0.1, "b": 0.9, "c": 0.9, "d": 0.5}) == ["b", "c", "d", "a"]


def test_by_score_treats_unscored_ids_as_zero_and_never_drops_them():
    assert by_score(BASE, {"d": 1.0}) == ["d", "a", "b", "c"]


def test_rrf_with_base_is_a_permutation_and_a_compromise():
    scores = {"a": 0.0, "b": 0.0, "c": 0.0, "d": 1.0}
    fused = rrf_with_base(BASE, scores)
    assert sorted(fused) == sorted(BASE)
    # d jumps a place on the signal's vote but does not overrule search outright.
    assert fused[0] == "a"
    assert fused.index("d") < BASE.index("d")


def test_lexical_scorer_is_fraction_of_query_tokens():
    s = LexicalScorer().score("where did Mel go hiking", [
        ("x", "Mel went hiking in Yosemite"), ("y", "unrelated text")])
    assert s["x"] == pytest.approx(2 / 5)
    assert s["y"] == 0.0


def test_hit_and_summary_count_gains_and_losses():
    provenance = {"a": ("t1",), "d": ("t9",)}
    assert hit(["a", "b"], 1, {"t1"}, provenance)
    assert not hit(["b", "a"], 1, {"t1"}, provenance)

    rows = [
        {"ceiling": True, "hits": {"baseline": {"1": False}, "by_score": {"1": True}}},
        {"ceiling": True, "hits": {"baseline": {"1": True}, "by_score": {"1": False}}},
        {"ceiling": False, "hits": {"baseline": {"1": False}, "by_score": {"1": False}}},
    ]
    report = summarize(rows, [1], ["by_score"])
    assert report["recall_at_1"]["baseline"] == pytest.approx(1 / 3, abs=1e-4)
    assert report["recall_at_1"]["by_score"] == {"recall": pytest.approx(1 / 3, abs=1e-4),
                                                 "gained": 1, "lost": 1}
    assert report["ceiling_at_depth"] == pytest.approx(2 / 3, abs=1e-4)


def test_from_env_names_every_missing_variable(monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.delenv("JEV_API_URL", raising=False)
    with pytest.raises(JevConfigError, match="JEV_API_KEY and JEV_API_URL"):
        JevClient.from_env()


@pytest.mark.parametrize("payload", [{}, {"answers": [{"id": "other", "value": 1}]},
                                     {"answers": [{"id": "relevance", "value": "high"}]},
                                     {"answers": [{"id": "relevance", "value": 3.0}]}])
def test_parse_score_fails_loudly_on_an_unexpected_shape(payload):
    with pytest.raises(JevResponseError):
        _parse_score(payload)


def test_client_caches_scores_on_disk(tmp_path, monkeypatch):
    posted = []

    def fake_post(self, body):
        posted.append(body)
        return {"answers": [{"id": "relevance", "value": 0.7}]}

    monkeypatch.setattr(jev_client.JevClient, "_post", fake_post)
    cache = tmp_path / "cache.jsonl"
    first = JevClient(api_key="k", api_url="https://example.invalid", cache_path=cache)
    assert first.score("q", [("a", "mem a"), ("b", "mem b")]) == {"a": 0.7, "b": 0.7}
    assert first.calls == 2 and len(posted) == 2

    second = JevClient(api_key="k", api_url="https://example.invalid", cache_path=cache)
    second._load_cache()
    assert second.score("q", [("a", "mem a")]) == {"a": 0.7}
    assert second.calls == 0 and second.cache_hits == 1 and len(posted) == 2
