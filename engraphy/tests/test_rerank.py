"""engraphy.core.rerank.rerank_fuse -- golden fixtures in fixtures/rerank_cases.yaml,
byte-exact per design/07 §Exact formulas (RRF), generalized to N rank-lists.
"""
import pathlib

import pytest
import yaml

from engraphy.core.rerank import rerank_fuse

FIXTURES = yaml.safe_load(
    (pathlib.Path(__file__).parent / "fixtures" / "rerank_cases.yaml").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", FIXTURES, ids=[c["name"] for c in FIXTURES])
def test_rerank_fixture_case(case):
    base_fused = [(e["id"], e["score"]) for e in case["base_fused"]]
    result = rerank_fuse(
        base_fused,
        case["signal_ranked"],
        created_at=case.get("created_at"),
    )
    expected = [(e["id"], e["score"]) for e in case["expect"]]
    assert result == expected


def test_zero_signals_returns_the_identical_list_object_elements_unrounded():
    """The identity branch must not round-trip scores through arithmetic at
    all -- an unrounded float survives exactly, which a recompute-then-round
    implementation would not guarantee."""
    base_fused = [("A", 0.0123456789), ("B", 0.01)]
    assert rerank_fuse(base_fused, []) == base_fused


def test_reorder_only_id_set_never_grows_or_shrinks():
    """Property check beyond the fixture's specific cases: for arbitrary
    signal content, the output id set always equals the base id set."""
    base_fused = [("A", 0.9), ("B", 0.5), ("C", 0.1), ("D", 0.05)]
    base_ids = {node_id for node_id, _ in base_fused}
    signal_ranked = [
        ["Z", "Y", "A"],          # extra ids the base never carried
        ["C"],                    # partial coverage
        [],                       # a signal that found nothing
        ["D", "C", "B", "A", "W"],  # full coverage plus one extra
    ]
    result = rerank_fuse(base_fused, signal_ranked)
    assert {node_id for node_id, _ in result} == base_ids
    assert len(result) == len(base_fused)
