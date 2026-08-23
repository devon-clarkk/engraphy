"""Phase C attr-surface helpers (fact-searchability-phase-c.md §1, §2.1): the
selection rule (attr_spec.searchable_keys) and the pure renders
(embedding.render_attr_surface / searchable_text). No DB, no model.
"""
from engraphy.core.attr_spec import searchable_keys
from engraphy.core.embedding import render_attr_surface, searchable_text


def _spec(required=None, optional=None):
    attrs = {}
    if required:
        attrs["required"] = required
    if optional:
        attrs["optional"] = optional
    return {"attrs": attrs}


# --- selection rule (§1) -----------------------------------------------------

def test_construct_default_strings_and_dates_in_enums_bools_numbers_out():
    keys = searchable_keys(_spec(optional={
        "occupation": {"type": "string"},
        "occurred_on": {"type": "date"},
        "strength": {"enum": ["hard", "soft"]},
        "urgent": {"type": "bool"},
        "priority": {"type": "int"},
        "score": {"type": "number"},
    }))
    assert keys == {"occupation", "occurred_on"}


def test_explicit_searchable_override_both_directions():
    keys = searchable_keys(_spec(optional={
        "email": {"type": "string", "searchable": False},   # string, excluded
        "severity": {"enum": ["hi", "lo"], "searchable": True},  # enum, included
        "location": {"type": "string"},                     # default in
    }))
    assert keys == {"severity", "location"}


def test_required_and_optional_both_scanned_reserved_excluded():
    keys = searchable_keys(_spec(
        required={"occurred_on": {"type": "date"}},
        optional={"location": {"type": "string"}},
    ))
    assert keys == {"occurred_on", "location"}
    # addenda is engine-reserved -- never searchable even if it somehow appeared.
    assert "addenda" not in searchable_keys(_spec(optional={"addenda": {"type": "string"}}))


def test_non_dict_rule_is_skipped_rather_than_raising():
    """A malformed rule (not an object) is skipped key by key, not fatal.

    Pack validation is what reports the shape error; selection still has to
    answer for the keys that ARE well formed, because this runs on every write
    and must not turn one bad rule into a failed write.
    """
    keys = searchable_keys(_spec(
        optional={"broken": "string", "location": {"type": "string"}},
    ))
    assert keys == {"location"}


def test_empty_or_missing_spec_yields_no_keys():
    assert searchable_keys({}) == set()
    assert searchable_keys(None) == set()
    assert searchable_keys({"attrs": {}}) == set()


# --- render (§2.1) -----------------------------------------------------------

def test_render_sorted_key_value_lines():
    out = render_attr_surface(
        {"occupation": "nurse", "location": "Leeds", "strength": "hard"},
        {"occupation", "location"})  # strength not a searchable key
    assert out == "location: Leeds\noccupation: nurse"


def test_render_skips_absent_none_and_empty():
    out = render_attr_surface(
        {"occupation": "nurse", "location": None, "url": ""},
        {"occupation", "location", "url", "birthday"})  # birthday absent
    assert out == "occupation: nurse"


def test_render_empty_when_nothing_matches():
    assert render_attr_surface({}, {"occupation"}) == ""
    assert render_attr_surface({"x": "y"}, set()) == ""
    assert render_attr_surface({"enum_flag": "hard"}, set()) == ""


def test_render_dates_verbatim():
    assert render_attr_surface({"occurred_on": "2023-06-15"}, {"occurred_on"}) == "occurred_on: 2023-06-15"


# --- searchable_text (§2.1) --------------------------------------------------

def test_searchable_text_empty_extra_is_byte_identical_to_title_body():
    # the load-bearing identity for §3's bounding argument.
    assert searchable_text("T", "B", "") == "T\nB"


def test_searchable_text_appends_extra_with_newline():
    assert searchable_text("T", "B", "occupation: nurse") == "T\nB\noccupation: nurse"
