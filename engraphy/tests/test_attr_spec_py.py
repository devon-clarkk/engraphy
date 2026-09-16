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

from engraphy.core.attr_spec import sanitize_attrs, validate_attrs

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


# --- sanitize_attrs: quarantine a bad value rather than destroy the node -----
# Pure. test_attr_quarantine.py proves the same rule end to end against a real
# database, and the parity fuzzer proves the plpgsql side agrees.

MEMO = {"attrs": {"required": {"occurred_on": {"type": "date"}},
                  "optional": {"note": {"type": "string"}, "rank": {"type": "int"}},
                  "closed": True}}
_BAD_DATE = "attrs.occurred_on must be a date"


def test_sanitize_leaves_a_valid_write_untouched():
    attrs = {"occurred_on": "2026-01", "note": "fine"}
    clean, dropped = sanitize_attrs(MEMO, attrs)
    assert clean is attrs
    assert dropped == []


def test_sanitize_quarantines_a_bad_required_value_and_the_rest_still_validates():
    clean, dropped = sanitize_attrs(MEMO, {"occurred_on": "last month", "note": "kept"})
    assert clean == {"note": "kept",
                     "dropped": {"occurred_on": {"value": "last month", "error": _BAD_DATE}}}
    assert dropped == [{"key": "occurred_on", "value": "last month", "error": _BAD_DATE}]
    # the quarantined key still satisfies required-presence, so the node stores
    assert validate_attrs(MEMO, clean) == []


def test_sanitize_quarantines_every_failing_key_in_lexicographic_order():
    clean, dropped = sanitize_attrs(MEMO, {"occurred_on": "nope", "note": 5, "rank": 3})
    assert [d["key"] for d in dropped] == ["note", "occurred_on"]
    assert clean["rank"] == 3
    assert set(clean["dropped"]) == {"note", "occurred_on"}


def test_sanitize_merges_an_existing_bucket_and_replaces_a_malformed_one():
    prior = {"as_of": {"value": "x", "error": "y"}}
    clean, _ = sanitize_attrs(MEMO, {"occurred_on": "nope", "dropped": prior})
    assert set(clean["dropped"]) == {"as_of", "occurred_on"}
    # only an object bucket carries forward; anything else is replaced by one
    clean, _ = sanitize_attrs(MEMO, {"occurred_on": "nope", "dropped": ["as_of"]})
    assert set(clean["dropped"]) == {"occurred_on"}


def test_sanitize_never_quarantines_a_reserved_or_undeclared_key():
    # `addenda` and `dropped` are engine-reserved, and an undeclared key under a
    # closed spec stays a hard rejection rather than something to rescue.
    attrs = {"occurred_on": "2026-01-15", "addenda": 5, "nope": "x"}
    clean, dropped = sanitize_attrs(MEMO, attrs)
    assert dropped == []
    assert clean is attrs
    assert "attrs.nope is not allowed (closed spec)" in validate_attrs(MEMO, attrs)


def test_sanitize_on_absent_attrs_is_an_empty_identity():
    assert sanitize_attrs(MEMO, None) == ({}, [])
