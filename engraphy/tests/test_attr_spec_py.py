"""engraphy.core.attr_spec.validate_attrs — Python side of the dual attr-spec
interpreter (design/implementation/attr-spec-interpreter-plan.md §Test plan,
row `test_attr_spec_py.py`): every fixture case in
`fixtures/attr_spec_cases.yaml`, exact ordered-array match. The plpgsql side
(`test_attr_spec_pg.py`) and the parity fuzzer both require a live Postgres
and are not part of this module.
"""

import pathlib

import pytest
import yaml

from engraphy.core.attr_spec import validate_attrs

FIXTURES_PATH = pathlib.Path(__file__).parent / "fixtures" / "attr_spec_cases.yaml"
CASES = yaml.safe_load(FIXTURES_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_fixture_case(case):
    result = validate_attrs(case["spec"], case["attrs"])
    assert result == case["expect"]


# --- Engine-reserved keys (migration 0017 / RESERVED_ATTR_KEYS) --------------
# These run without Postgres; test_attr_spec_pg.py and the parity fuzzer assert
# the plpgsql side agrees. See the regression block in test_dedup.py for why
# this exemption exists (closed-spec types could never receive a merge addendum).

_CLOSED_SPEC = {
    "attrs": {
        "required": {"status": {"enum": ["open", "closed"]}},
        "optional": {"note": {"type": "string"}},
        "closed": True,
    }
}


def test_reserved_addenda_is_exempt_from_closed_spec_check():
    assert validate_attrs(_CLOSED_SPEC, {"status": "open", "addenda": [{"body": "x"}]}) == []


def test_reserved_exemption_does_not_widen_to_other_unknown_keys():
    assert validate_attrs(_CLOSED_SPEC, {"status": "open", "surprise": 1}) == [
        "attrs.surprise is not allowed (closed spec)"
    ]


def test_reserved_exemption_is_inert_on_an_open_spec():
    assert validate_attrs({"attrs": {"closed": False}}, {"addenda": [{"body": "x"}]}) == []


def test_pack_declared_addenda_is_still_value_checked():
    """The Phase-3 exemption must not leak into Phase 4: if a pack does declare
    `addenda` (pathological, but the grammar permits it), its value rule still
    applies -- on both sides, since neither skips Phase 4 for reserved keys."""
    spec = {"attrs": {"optional": {"addenda": {"type": "string"}}, "closed": True}}
    assert validate_attrs(spec, {"addenda": 5}) == ["attrs.addenda must be a string"]
