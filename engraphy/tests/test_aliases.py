"""engraphy.server.aliases -- pack tool-alias binding (design/03 s.Tool aliases;
acceptance line "the example pack's aliases bind and round-trip against
core tools").

Pure tests use an explicit core-tool map (no yaml/jsonschema needed). The last test
is the design/03 acceptance case: the real example pack's log_error alias binds and
round-trips.
"""

import pathlib

import pytest

from engraphy.server.aliases import AliasBinding, build_aliases, resolve_alias_call

_CORE = {
    "write": {"scope", "type", "title", "body", "attrs", "links", "session_id"},
    "search": {"scope", "query", "types", "limit", "include_inactive", "detail"},
}


def test_build_aliases_valid_binding():
    pack = {"tool_aliases": {"log_error": {
        "binds": "write", "preset": {"type": "error"}, "description": "Record a failure."}}}
    bindings = build_aliases(pack, core_tools=_CORE)
    assert list(bindings) == ["log_error"]
    b = bindings["log_error"]
    assert isinstance(b, AliasBinding)
    assert (b.binds, b.preset, b.description) == ("write", {"type": "error"}, "Record a failure.")


@pytest.mark.parametrize("pack", [{}, {"tool_aliases": {}}, {"tool_aliases": None}])
def test_build_aliases_empty(pack):
    assert build_aliases(pack, core_tools=_CORE) == {}


@pytest.mark.parametrize("pack,needle", [
    ({"tool_aliases": {"x": {"binds": "nope", "preset": {}}}}, "not a core tool"),
    ({"tool_aliases": {"x": {"binds": "write", "preset": {"bogus": 1}}}}, "not argument"),
    ({"tool_aliases": {"write": {"binds": "write", "preset": {}}}}, "shadows core tool"),
])
def test_build_aliases_rejects_malformed(pack, needle):
    with pytest.raises(ValueError) as exc:
        build_aliases(pack, core_tools=_CORE)
    assert needle in str(exc.value)


def test_resolve_alias_call_preset_wins_and_audit_identity():
    b = AliasBinding("log_error", "write", {"type": "error"}, None)
    core, merged, audit = resolve_alias_call(
        b, {"scope": "s", "title": "t", "body": "x", "type": "widget"})
    assert core == "write"
    assert merged["type"] == "error"                 # preset overrides caller
    assert merged["scope"] == "s" and merged["title"] == "t"  # caller args preserved
    assert audit == "write via log_error"


def test_resolve_alias_call_handles_no_caller_args():
    b = AliasBinding("log_error", "write", {"type": "error"}, None)
    core, merged, audit = resolve_alias_call(b, None)
    assert merged == {"type": "error"} and core == "write" and audit == "write via log_error"


def test_example_pack_aliases_bind_and_round_trip():
    """design/03 acceptance: the real pack's log_error binds `write` and round-trips.
    Uses the default core-tool map (packs.CORE_TOOLS) -- the true single source."""
    import yaml

    pack_path = (pathlib.Path(__file__).parent / "fixtures" / "packs" / "example-pack.yaml")
    pack = yaml.safe_load(pack_path.read_text(encoding="utf-8"))
    bindings = build_aliases(pack)  # default core_tools from packs.CORE_TOOLS
    assert "log_error" in bindings
    b = bindings["log_error"]
    assert b.binds == "write" and b.preset == {"type": "error"}
    core, merged, audit = resolve_alias_call(b, {"scope": "project-alpha", "title": "boom", "body": "…"})
    assert core == "write" and merged["type"] == "error" and audit == "write via log_error"
