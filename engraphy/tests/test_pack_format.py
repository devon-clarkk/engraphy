"""engraphy.admin.packs.check_pack_format() -- design/04 s.Versioning and
release discipline: "The server logs pack-format warnings when a pack uses
constructs newer than it declares." See check_pack_format()'s own docstring
for the narrower, checkable version implemented (declared-format-vs-engine
comparison, not per-construct newness).
"""
import pathlib

import yaml

from engraphy.admin.packs import (
    CURRENT_PACK_FORMAT,
    check_pack_format,
    check_same_topic_declared,
    validate,
)

REPO_ROOT = pathlib.Path(__file__).parents[2]


def test_shipped_starter_pack_has_no_format_warning():
    pack = yaml.safe_load((REPO_ROOT / "packs" / "starter" / "pack.yaml").read_text(encoding="utf-8"))
    assert check_pack_format(pack) is None


def test_shipped_packs_declare_same_topic():
    """Phase B (§1.3): every shipped pack (and the bench pack + template) declares
    the `same_topic` edge type and a covering rule, so the merge path can link
    clusters without the graceful-skip warning firing."""
    for rel in (
        "packs/starter/pack.yaml",
        "packs/conversational/pack.yaml",
        "packs/pack-template.yaml",
        "bench/pack/bench-pack.yaml",
    ):
        pack = yaml.safe_load((REPO_ROOT / rel).read_text(encoding="utf-8"))
        assert check_same_topic_declared(pack) is None, rel


def test_pack_missing_same_topic_warns():
    """A pack that predates the type warns but is not rejected (I1 holds; only the
    cluster edge is skipped)."""
    no_type = {"pack": "xx", "version": 1, "node_types": {"aa": {"description": "d"}},
               "edge_types": {"relates_to": {"description": "d", "bidirectional": True}},
               "edge_rules": [{"type": "relates_to", "src": "*", "dst": "*"}]}
    warning = check_same_topic_declared(no_type)
    assert warning is not None and "same_topic" in warning
    assert validate(no_type) == []  # a warning, never a validate() error

    # Type present but no covering rule still warns (the merge path would find no
    # rule at the §1.3 pre-check and skip the edge).
    type_no_rule = {**no_type,
                    "edge_types": {**no_type["edge_types"],
                                   "same_topic": {"description": "d", "bidirectional": True}}}
    assert check_same_topic_declared(type_no_rule) is not None


def test_pack_without_pack_format_field_defaults_to_1_no_warning():
    pack = {"pack": "xx", "version": 1, "node_types": {"a": {"description": "d"}}, "edge_types": {}}
    assert check_pack_format(pack) is None


def test_pack_declaring_current_format_no_warning():
    pack = {"pack": "xx", "version": 1, "pack_format": CURRENT_PACK_FORMAT,
            "node_types": {"a": {"description": "d"}}, "edge_types": {}}
    assert check_pack_format(pack) is None


def test_pack_declaring_future_format_warns():
    pack = {"pack": "xx", "version": 1, "pack_format": CURRENT_PACK_FORMAT + 1,
            "node_types": {"a": {"description": "d"}}, "edge_types": {}}
    warning = check_pack_format(pack)
    assert warning is not None
    assert "x" in warning
    assert str(CURRENT_PACK_FORMAT + 1) in warning
    assert str(CURRENT_PACK_FORMAT) in warning


def test_pack_format_field_is_schema_valid_and_optional():
    base = {"pack": "xx", "version": 1, "node_types": {"aa": {"description": "d"}}, "edge_types": {}}
    assert validate(base) == []
    with_format = {**base, "pack_format": 1}
    assert validate(with_format) == []


# ---- Phase C: `searchable` attr flag (grammar + format warning) -------------


def _pack_with_searchable(flag_value, *, pack_format=None):
    """A minimal valid pack whose `note` type marks an optional string attr
    `searchable: <flag_value>`."""
    p = {
        "pack": "sx", "version": 1,
        "node_types": {
            "note": {"description": "d",
                     "attrs": {"optional": {"tag": {"type": "string", "searchable": flag_value}},
                               "closed": True}}
        },
        "edge_types": {},
    }
    if pack_format is not None:
        p["pack_format"] = pack_format
    return p


def test_searchable_flag_is_schema_valid_on_a_rule_object():
    """The grammar accepts `searchable: true|false` alongside type (and enum)."""
    assert validate(_pack_with_searchable(True)) == []
    assert validate(_pack_with_searchable(False)) == []
    # alongside an enum, too.
    enum_pack = {
        "pack": "sx", "version": 1,
        "node_types": {"note": {"description": "d",
                                "attrs": {"required": {"sev": {"enum": ["hi", "lo"], "searchable": True}}}}},
        "edge_types": {},
    }
    assert validate(enum_pack) == []


def test_searchable_flag_must_be_boolean():
    assert validate(_pack_with_searchable("yes")) != []  # string, not bool -> schema error


def test_pack_using_searchable_without_declaring_format_2_warns():
    """A format-1 pack using `searchable` warns (pre-C engines ignore it); a
    format-2 pack does not."""
    w = check_pack_format(_pack_with_searchable(False))  # default pack_format 1
    assert w is not None and "searchable" in w
    assert check_pack_format(_pack_with_searchable(False, pack_format=2)) is None


def test_shipped_packs_do_not_use_searchable_and_do_not_warn():
    """§1: the construct-based default covers every shipped pack -- no explicit
    `searchable` override is needed, so none is declared and none warns."""
    for rel in ("packs/starter/pack.yaml", "packs/conversational/pack.yaml",
                "bench/pack/bench-pack.yaml"):
        pack = yaml.safe_load((REPO_ROOT / rel).read_text(encoding="utf-8"))
        assert check_pack_format(pack) is None, rel
