"""engraphy.server.wire_types — dispatcher-side wire-type enforcement
(design/07 §Per-argument wire types; plan in
design/implementation/wire-type-enforcement-plan.md).

Two groups:

- **The fixture-acceptance loop**, first and most important: every golden wire
  fixture's pinned request arguments must be ACCEPTED by the spec. The spec must
  never reject the pinned contract, and the loop has no exemptions — that is the
  whole point. Until 2026-07-21 nothing in the suite loaded `fixtures/wire/*.json`
  at all; their "byte-exact" status lived in module docstrings and was checked by
  eye, which is how `resolve_duplicate_merged.json` came to pin a request the
  server has always refused (QUESTIONS.md "wire-fixture-merge-into"). An artifact
  called normative that no test executes is a claim, not a check.
- **Per-rule unit tests** over `validate()`, one per rule class, plus the
  generated-schema properties.

Wire-level tests (the same refusals through the real funnel, alias parity, and
clamps surviving) live in test_app.py, where the server harness already is.
"""
import json
import pathlib

import pytest

from engraphy.server import wire_types
from engraphy.server.auth import ToolError

_WIRE_FIXTURE_DIR = (
    pathlib.Path(__file__).resolve().parent / "fixtures" / "wire"
)


def _fixture_files():
    """Every golden wire fixture. `errors.json` carries no `request` (it pins
    error texts only) and drops out below rather than being named in a skip
    list — the loop stays exemption-free by construction."""
    return sorted(_WIRE_FIXTURE_DIR.glob("*.json"))


def _fixture_requests():
    cases = []
    for path in _fixture_files():
        fixture = json.loads(path.read_text(encoding="utf-8"))
        request = fixture.get("request")
        if request is None:
            continue
        cases.append(pytest.param(request["name"], request.get("arguments", {}),
                                  id=path.stem))
    return cases


def test_the_fixture_directory_is_not_silently_empty():
    """The loop below is a no-op if the glob stops matching — a rename or a
    moved directory would turn the guarantee off without failing anything."""
    assert len(_fixture_files()) == 12
    assert len(_fixture_requests()) == 11, "eleven fixtures pin a request; errors.json does not"


@pytest.mark.parametrize("tool_name,arguments", _fixture_requests())
def test_every_golden_fixture_request_is_accepted_by_the_spec(tool_name, arguments):
    """The transcription must not reject the pinned contract. A failure here is
    either a transcription bug or a spec-vs-fixture disagreement — and per the
    plan the second is a QUESTIONS.md entry, never a quiet fixture edit."""
    wire_types.validate(tool_name, arguments)


def test_every_spec_tool_publishes_a_schema_naming_its_own_arguments():
    for tool_name, spec in wire_types.SPEC.items():
        schema = wire_types.input_schema(tool_name)
        assert set(schema["properties"]) == set(spec), tool_name
        assert schema["additionalProperties"] is False, tool_name


# --- Rule classes -----------------------------------------------------------


def _refused(tool_name, arguments) -> str:
    with pytest.raises(ToolError) as exc:
        wire_types.validate(tool_name, arguments)
    assert exc.value.code == "VALIDATION"
    return exc.value.message


_GOOD_WRITE = {"scope": "s", "type": "note", "title": "T", "body": "B"}
_A_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def test_unknown_argument_is_refused_and_named():
    message = _refused("write", {**_GOOD_WRITE, "flavour": "spicy"})
    assert "flavour" in message


def test_explicit_null_is_refused_with_omit_the_key_guidance():
    """07: absent and null are not the same thing. The message has to say what
    to do instead, or a model retries by sending null again."""
    message = _refused("write", {**_GOOD_WRITE, "attrs": None})
    assert "attrs" in message and "omit the key" in message


def test_null_in_a_required_argument_is_refused_as_null_not_as_missing():
    message = _refused("write", {**_GOOD_WRITE, "title": None})
    assert "title" in message and "omit the key" in message


def test_missing_required_argument_is_refused_and_named():
    message = _refused("write", {k: v for k, v in _GOOD_WRITE.items() if k != "body"})
    assert "body" in message and "required" in message


def test_wrong_json_type_is_refused_per_type():
    assert "must be a string" in _refused("write", {**_GOOD_WRITE, "title": 7})
    assert "must be an integer" in _refused("search", {"scope": "s", "query": "q", "limit": "25"})
    assert "must be a boolean" in _refused("search", {"scope": "s", "query": "q",
                                                      "include_inactive": "yes"})
    assert "must be an object" in _refused("write", {**_GOOD_WRITE, "attrs": []})
    assert "must be an array" in _refused("search", {"scope": "s", "query": "q", "types": "note"})


def test_integer_accepts_an_integral_json_number():
    """25.0 is 25 — JSON Schema's own convention. Client serializers differ on
    whether a whole number crosses the wire with a fractional part, and the
    difference carries no information."""
    wire_types.validate("search", {"scope": "s", "query": "q", "limit": 25.0})
    assert "must be an integer" in _refused("search", {"scope": "s", "query": "q", "limit": 25.5})


def test_boolean_is_not_accepted_as_an_integer():
    """Python's bool subclasses int, so `True` would pass an isinstance check
    for the integer type — an accident of the host language the wire never meant."""
    assert "must be an integer" in _refused("search", {"scope": "s", "query": "q", "limit": True})


def test_out_of_range_integer_is_NOT_refused_because_clamps_stay_clamps():
    """07 pins this explicitly: type errors reject, range excess clamps. Moving
    the clamp up here would turn a documented, forgiving behavior into a refusal."""
    wire_types.validate("search", {"scope": "s", "query": "q", "limit": 10_000})
    wire_types.validate("search", {"scope": "s", "query": "q", "limit": 0})
    wire_types.validate("traverse", {"start_id": _A_UUID, "direction": "out", "max_depth": 99})


def test_enum_miss_is_refused_naming_the_allowed_values():
    message = _refused("traverse", {"start_id": _A_UUID, "direction": "sideways"})
    assert "direction" in message and "out" in message and "both" in message


def test_uuid_is_accepted_case_insensitively():
    """Postgres's own acceptance; the wire does not tighten beyond what the
    database always meant, and the server emits lowercase regardless."""
    wire_types.validate("traverse", {"start_id": _A_UUID.upper(), "direction": "out"})


def test_malformed_uuid_is_a_validation_error_not_a_cast_failure():
    """The concrete gap 07 named: before enforcement a malformed uuid reached
    Postgres and surfaced as an INTERNAL-class cast failure."""
    message = _refused("traverse", {"start_id": "not-a-uuid", "direction": "out"})
    assert "start_id" in message and "uuid" in message


def test_uuid_inside_an_array_is_validated_per_element():
    message = _refused("get", {"ids": [_A_UUID, "nope"]})
    assert "ids[1]" in message and "uuid" in message


def test_merge_into_is_required_only_when_resolution_is_merge():
    wire_types.validate("resolve_duplicate", {"pending_id": _A_UUID, "resolution": "distinct"})
    message = _refused("resolve_duplicate", {"pending_id": _A_UUID, "resolution": "merge"})
    assert "merge_into" in message and "merge" in message
    wire_types.validate("resolve_duplicate", {"pending_id": _A_UUID, "resolution": "merge",
                                              "merge_into": _A_UUID})


def test_merge_into_alongside_distinct_is_accepted_not_refused():
    """07 pins presence ("required when resolution == merge") and nothing more.
    Refusing the pair would be a rule this build invented; the plan's "iff"
    shorthand loses to 07, which is normative."""
    wire_types.validate("resolve_duplicate", {"pending_id": _A_UUID, "resolution": "distinct",
                                              "merge_into": _A_UUID})


def test_inbox_review_per_action_requirements_both_ways():
    wire_types.validate("inbox_review", {"action": "list"})
    wire_types.validate("inbox_review", {"action": "list", "limit": 10, "offset": 5})
    wire_types.validate("inbox_review", {"action": "discard", "id": _A_UUID})
    assert "id" in _refused("inbox_review", {"action": "discard"})

    promote = {"action": "promote", "id": _A_UUID, "type": "note",
               "scope": "s", "title": "T", "body": "B"}
    wire_types.validate("inbox_review", promote)
    for missing in ("id", "type", "scope", "title", "body"):
        message = _refused("inbox_review", {k: v for k, v in promote.items() if k != missing})
        assert missing in message and "promote" in message


def test_list_action_does_not_require_promotes_fields():
    """The conditional has to be genuinely per-action, not "required if any
    promote field is present"."""
    wire_types.validate("inbox_review", {"action": "list"})


def test_link_items_are_type_checked_but_endpoint_counts_are_not():
    """Endpoint-count semantics stay in core's _validate_links_shape — one
    contract-holder. This layer must accept a shape core will later refuse."""
    wire_types.validate("link", {"edges": [{"type": "relates_to", "src_id": _A_UUID}]})
    wire_types.validate("write", {**_GOOD_WRITE,
                                  "links": [{"type": "relates_to", "src_id": _A_UUID,
                                             "dst_id": _A_UUID}]})
    assert "must be an array" in _refused("link", {"edges": {"type": "relates_to"}})
    assert "unknown key" in _refused("link", {"edges": [{"type": "r", "weight": 3}]})
    assert "type" in _refused("link", {"edges": [{"src_id": _A_UUID}]})
    assert "must be a string" in _refused("link", {"edges": [{"type": 7}]})


def test_scope_list_takes_no_arguments_at_all():
    wire_types.validate("scope_list", {})
    assert "anything" in _refused("scope_list", {"anything": 1})


def test_admin_tools_are_in_the_spec_with_their_enums():
    wire_types.validate("admin_token_create", {"principal": "p", "client_name": "c",
                                               "role": "readonly"})
    assert "role" in _refused("admin_token_create", {"principal": "p", "client_name": "c",
                                                     "role": "superuser"})
    assert "visibility" in _refused("admin_scope_visibility", {"scope_id": "s",
                                                               "visibility": "public"})


# --- The generated schema ---------------------------------------------------


def test_generated_schema_carries_real_types_required_and_enums():
    schema = wire_types.input_schema("search")
    assert schema["properties"]["limit"] == {"type": "integer"}
    assert schema["properties"]["detail"]["enum"] == ["full", "summary"]
    assert schema["required"] == ["query", "scope"]
    assert schema["properties"]["types"] == {"type": "array", "items": {"type": "string"}}


def test_generated_schema_omits_conditionally_required_arguments():
    """A flat schema cannot express "merge_into when resolution is merge".
    Advertising it as required would overstate the surface — drift in the
    other direction, which is exactly what generating from one spec prevents."""
    schema = wire_types.input_schema("resolve_duplicate")
    assert schema["required"] == ["pending_id", "resolution"]
    assert "merge_into" in schema["properties"]

    inbox = wire_types.input_schema("inbox_review")
    assert inbox["required"] == ["action"]


def test_generated_schema_marks_uuid_formats():
    assert wire_types.input_schema("update")["properties"]["id"]["format"] == "uuid"
    assert wire_types.input_schema("get")["properties"]["ids"]["items"]["format"] == "uuid"


def test_generating_a_schema_does_not_mutate_the_shared_type_templates():
    """The link-item template is a nested dict shared by every tool that takes
    links; a shallow copy would let one tool's schema edit leak into another's."""
    first = wire_types.input_schema("link")
    first["properties"]["edges"]["items"]["properties"]["type"]["type"] = "corrupted"
    assert wire_types.input_schema("write")["properties"]["links"]["items"][
        "properties"]["type"]["type"] == "string"


# --- Cross-checks against independent sources -------------------------------
#
# Four tools (`update`, `supersede`, `scope_create`) and the four `admin_*` ones
# have neither a golden wire fixture nor a funnel test, so for THEM the loop
# above proves nothing and correctness rests on the transcription alone. These
# two tests give that transcription a second source to agree with, so a dropped
# or renamed argument cannot pass unnoticed.


def test_every_core_tools_declared_argument_is_in_the_spec():
    """`packs.CORE_TOOLS` is the alias-preset allow-list -- an independently
    maintained list of each core tool's argument names, written for a different
    purpose. Every name it declares must exist in the spec, or a pack could
    legally preset an argument the wire now refuses.

    Subset, not equality, and deliberately so: CORE_TOOLS is NARROWER for
    `inbox_review` (it lists only `action`, since that is all a pack may
    preset), which is a known and accepted difference in surface rather than a
    transcription error.
    """
    from engraphy.admin.packs import CORE_TOOLS

    for tool_name, declared in CORE_TOOLS.items():
        assert tool_name in wire_types.SPEC, tool_name
        missing = declared - set(wire_types.SPEC[tool_name])
        assert not missing, f"{tool_name} declares {missing} but the spec omits it"


def test_the_spec_covers_exactly_the_dispatchable_tool_surface():
    """No tool may be dispatchable without a spec entry -- an unspecced tool
    would sail through `validate()` unchecked (it returns early on an unknown
    name), silently opting itself out of enforcement. The reverse also holds:
    a spec entry for a tool that does not exist is dead weight that would
    advertise a tool nothing can serve."""
    from engraphy.server.tool_registry import ADMIN_DISPATCH, CORE_DISPATCH

    assert set(wire_types.SPEC) == set(CORE_DISPATCH) | set(ADMIN_DISPATCH)


def test_the_merged_fixture_pins_the_instruction_byte_exactly():
    """`instruction` is a static string living in three places: dedup.py's
    constant, design/07's example, and this fixture. They are only useful if
    they agree, and a reworded copy would be a silently different contract --
    so the fixture is checked against the code rather than eyeballed.

    (The request-side acceptance loop above covers the fixture's ARGUMENTS; this
    is the one response-side assertion in the suite, added because the 2026-07-21
    ruling authorised an additive edit to this specific fixture and the field it
    added is the whole point of the change.)
    """
    from engraphy.core.dedup import MERGED_INSTRUCTION

    fixture = json.loads(
        (_WIRE_FIXTURE_DIR / "write_merged.json").read_text(encoding="utf-8"))
    assert fixture["response"]["instruction"] == MERGED_INSTRUCTION
