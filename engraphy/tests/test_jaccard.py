"""engraphy.core.jaccard — golden fixtures in fixtures/jaccard_cases.yaml,
byte-exact per design/07 §Exact formulas (addendum novelty).
"""
import pathlib

import pytest
import yaml

from engraphy.core.jaccard import is_novel, jaccard

FIXTURES = yaml.safe_load(
    (pathlib.Path(__file__).parent / "fixtures" / "jaccard_cases.yaml").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", FIXTURES, ids=[c["name"] for c in FIXTURES])
def test_jaccard_fixture_case(case):
    j = jaccard(case["body_a"], case["body_b"])
    assert j == pytest.approx(case["expect_j"], abs=1e-4)
    assert is_novel(case["body_a"], case["body_b"]) == case["expect_addendum"]
