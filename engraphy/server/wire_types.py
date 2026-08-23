"""Dispatcher-side wire-type enforcement (design/07 §Per-argument wire types;
ruled 2026-07-21, DECISIONS-DELTA; plan in
design/implementation/wire-type-enforcement-plan.md).

This module is the code-side single source of truth for what the server
*accepts*. `SPEC` below is a transcription of 07's per-argument table -- not an
independent design. Where this module and 07 disagree, 07 wins and this module
is the bug.

**Why enforcement is ours and never the SDK's.** `call_tool(validate_input=
False)` is permanent design, not a workaround awaiting an upstream fix. Two
independent reasons, either sufficient: the SDK `Server`'s `_tool_cache` is
process-global and shared by list_tools/call_tool across requests, so per-space
tool sets would clobber each other's cached schemas under concurrency; and the
SDK's jsonschema errors are not `ENGRAPHY_<CODE>`-shaped, so turning it on would
break 07's own error contract as a side effect. Do not enable it, and do not
reach into `_tool_cache`. The published `inputSchema` this module generates is a
truthful advertisement for client UIs -- never a validation source -- which is
what keeps that cache hazard dormant.

**Where it runs.** One place: `app.py::handle_call_tool`, on the RESOLVED core
tool's MERGED arguments, after the role/rate gates and before dispatch. Running
on the resolved-and-merged view is what makes aliases correct by construction
(an alias inherits its target's surface, and a pack preset that names a bogus
argument fails every call loudly rather than sometimes) and what keeps malformed
floods rate-throttled.

**Pinned semantics** (07, verbatim in intent):

- Closed argument surface -- an argument name outside the table is refused.
- Explicit `null` is refused everywhere; absent and null are not the same thing.
- `integer` means a JSON number with zero fractional part: `25.0` is `25`. JSON
  Schema's own convention; client serializers differ and the difference carries
  no information.
- uuids are RFC-4122 textual form, accepted case-insensitively (Postgres's own
  acceptance -- the wire does not tighten beyond what the database always
  meant). A malformed uuid is VALIDATION here rather than a cast failure three
  layers down, which is the concrete gap 07 named.
- **Clamps stay clamps.** A well-typed out-of-range integer is NOT refused --
  clamping lives downstream where it always has. Type errors reject; range
  excess clamps.
- Enums are enforced here; the dispatchers' own enum checks stay as defense in
  depth (their messages were never pinned, only their code).
- Link items are type-checked only. The endpoint-count rules (exactly-one for
  `write.links`, both for `link.edges`) stay in core's `_validate_links_shape`
  -- one contract-holder, no duplicated logic.

**Two deliberate non-refusals**, both because 07 pins presence and nothing more:

- `merge_into` is required when `resolution == "merge"` and merely *ignored*
  otherwise. The plan's shorthand says "iff", but 07's table says only "required
  when", and 07 wins; refusing `merge_into` alongside `distinct` would be a
  refusal this build invented.
- `inbox_review`'s `limit`/`offset` are documented "action='list' only", but
  that is which action READS them, not a per-action name ban. They stay valid
  names for the tool. The closed surface refuses names that are not in the
  table, not names that a particular action ignores.
"""
import copy
import re

from engraphy.server.auth import ToolError

# RFC-4122 textual form. Deliberately does NOT assert the version/variant
# nibbles: 07 pins "accepted case-insensitively (Postgres's own acceptance --
# the wire does not tighten beyond what the database always meant)", and
# Postgres accepts any 8-4-4-4-12 hex, including the nil uuid. Tightening here
# would refuse ids the database has always issued and stored.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# The JSON types this spec speaks, as they arrive over MCP.
STRING = "string"
INTEGER = "integer"
BOOLEAN = "boolean"
OBJECT = "object"
ARRAY_OF_STRING = "array_of_string"
ARRAY_OF_UUID = "array_of_uuid"
ARRAY_OF_LINK_ITEMS = "array_of_link_items"

#: Keys a link item may carry (07 §Links wire shape). `type` is always present;
#: which endpoints are required is core's call, not this layer's.
_LINK_ITEM_KEYS = frozenset({"type", "src_id", "dst_id"})


class Arg:
    """One row of 07's per-argument table.

    required: True (always), False (optional), or a callable taking the merged
    arguments and returning whether this argument is required for THIS call --
    the two genuinely conditional rows. They are encoded as code rather than
    schema because that is precisely what generic jsonschema made ugly and a
    purpose-built validator makes trivial (the third reason dispatcher-side
    validation beat the SDK path).
    """

    __slots__ = ("enum", "json_type", "required", "uuid")

    def __init__(self, json_type, required=False, enum=None, uuid=False):
        self.json_type = json_type
        self.required = required
        self.enum = enum
        self.uuid = uuid

    def is_required(self, arguments: dict) -> bool:
        if callable(self.required):
            return self.required(arguments)
        return bool(self.required)

    @property
    def conditional(self) -> bool:
        return callable(self.required)


def _merge_into_required(arguments: dict) -> bool:
    """07: required when `resolution == "merge"`. Read from the merged
    arguments, so a pack alias that presets `resolution` is judged on what the
    core tool will actually receive."""
    return arguments.get("resolution") == "merge"


def _inbox_promote_only(arguments: dict) -> bool:
    """The reviewer authors the write, so promote needs write's own required
    fields (07's inbox_review rows)."""
    return arguments.get("action") == "promote"


def _inbox_id_required(arguments: dict) -> bool:
    return arguments.get("action") in ("promote", "discard")


# --- The spec: 07's per-argument table, transcribed -------------------------
#
# Transcribe, do not invent. A mismatch between this table and a dispatcher's
# actual reads is a QUESTIONS.md entry, not a silent fix in either direction.

_WRITE_FIELDS = {
    "scope": Arg(STRING, required=True),
    "type": Arg(STRING, required=True),
    "title": Arg(STRING, required=True),
    "body": Arg(STRING, required=True),
    "attrs": Arg(OBJECT),
    "links": Arg(ARRAY_OF_LINK_ITEMS),
    "session_id": Arg(STRING),
}

SPEC: dict[str, dict[str, Arg]] = {
    "briefing": {
        "scope": Arg(STRING, required=True),
        "hint": Arg(STRING),
    },
    "search": {
        "scope": Arg(STRING, required=True),
        "query": Arg(STRING, required=True),
        "types": Arg(ARRAY_OF_STRING),
        "limit": Arg(INTEGER),                      # clamped 1-25 downstream
        "include_inactive": Arg(BOOLEAN),
        "detail": Arg(STRING, enum=("full", "summary")),
    },
    "traverse": {
        "start_id": Arg(STRING, required=True, uuid=True),
        "direction": Arg(STRING, required=True, enum=("out", "in", "both")),
        "edge_types": Arg(ARRAY_OF_STRING),
        "max_depth": Arg(INTEGER),                  # clamped downstream
        "limit": Arg(INTEGER),                      # clamped downstream
        "detail": Arg(STRING, enum=("summary", "full")),
    },
    "get": {
        "ids": Arg(ARRAY_OF_UUID, required=True),
    },
    "pending_list": {
        "limit": Arg(INTEGER),                      # clamped 1-100 downstream
        "offset": Arg(INTEGER),                     # clamped >=0 downstream
    },
    "stats": {
        "range_days": Arg(INTEGER),                 # clamped 1-3650 downstream
        "group_by": Arg(STRING, enum=("space", "user")),
    },
    "write": dict(_WRITE_FIELDS),
    # 07: "*(remaining)* ... identical to `write`'s".
    "supersede": {"old_id": Arg(STRING, required=True, uuid=True), **_WRITE_FIELDS},
    "update": {
        "id": Arg(STRING, required=True, uuid=True),
        "title": Arg(STRING),
        "body": Arg(STRING),
        "attrs": Arg(OBJECT),
    },
    "link": {
        "edges": Arg(ARRAY_OF_LINK_ITEMS, required=True),
    },
    "resolve_duplicate": {
        "pending_id": Arg(STRING, required=True, uuid=True),
        "resolution": Arg(STRING, required=True, enum=("distinct", "merge")),
        "merge_into": Arg(STRING, required=_merge_into_required, uuid=True),
    },
    "scope_list": {},
    "scope_guide": {},
    "scope_create": {
        "id": Arg(STRING, required=True),
        "display_name": Arg(STRING, required=True),
        # Mandatory: this layer refuses a MISSING description (required check).
        # An empty-string "" passes the type check here, so the non-empty refusal
        # is the dispatcher/core's semantic job -- same split as `confirm`.
        "description": Arg(STRING, required=True),
        # A falsy value is ENGRAPHY_VALIDATION, but that is a semantic refusal the
        # dispatcher owns -- this layer only pins the JSON type.
        "confirm": Arg(BOOLEAN),
    },
    "inbox_review": {
        "action": Arg(STRING, required=True, enum=("list", "promote", "discard")),
        "limit": Arg(INTEGER),
        "offset": Arg(INTEGER),
        "id": Arg(STRING, required=_inbox_id_required, uuid=True),
        "type": Arg(STRING, required=_inbox_promote_only),
        "scope": Arg(STRING, required=_inbox_promote_only),
        "title": Arg(STRING, required=_inbox_promote_only),
        "body": Arg(STRING, required=_inbox_promote_only),
        "attrs": Arg(OBJECT),
        "links": Arg(ARRAY_OF_LINK_ITEMS),
        "session_id": Arg(STRING),
    },
    "admin_member_add": {
        "id": Arg(STRING, required=True),
        "display_name": Arg(STRING, required=True),
        "role": Arg(STRING, enum=("member", "space_admin")),
    },
    "admin_token_create": {
        "principal": Arg(STRING, required=True),
        "client_name": Arg(STRING, required=True),
        "role": Arg(STRING, required=True, enum=("readwrite", "readonly")),
        # The per-token scope restriction (migration 0023). OPTIONAL, defaulting
        # to false at the dispatcher: a required flag would break every existing
        # caller of a security-critical mint to say "no restriction", which is
        # what they already meant. Absent = unrestricted, exactly as before.
        "no_scope_all": Arg(BOOLEAN),
    },
    "admin_scope_visibility": {
        "scope_id": Arg(STRING, required=True),
        "visibility": Arg(STRING, required=True,
                          enum=("private", "team-read", "team-write")),
    },
    "admin_grant": {
        "scope_id": Arg(STRING, required=True),
        "principal": Arg(STRING, required=True),
        "level": Arg(STRING, required=True, enum=("read", "write")),
    },
}


def _refuse(message: str) -> None:
    raise ToolError("VALIDATION", message)


def _is_integer(value) -> bool:
    """A JSON number with zero fractional part. `bool` is excluded explicitly:
    Python's bool subclasses int, so `True` would otherwise pass as 1 -- an
    accident of the host language, not something the wire ever meant."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


def _check_link_items(field: str, value) -> None:
    """JSON shape only. Which endpoints an item must carry is core's
    `_validate_links_shape` -- duplicating it here would create a second
    contract-holder for the one rule that differs between write and link."""
    if not isinstance(value, list):
        _refuse(f"{field} must be an array of link items")
    for index, item in enumerate(value):
        where = f"{field}[{index}]"
        if not isinstance(item, dict):
            _refuse(f"{where} must be an object with a type and an endpoint")
        unknown = sorted(set(item) - _LINK_ITEM_KEYS)
        if unknown:
            _refuse(f"{where} has unknown key '{unknown[0]}'; link items accept "
                    f"only type, src_id and dst_id")
        if "type" not in item:
            _refuse(f"{where}.type is required")
        for key in sorted(_LINK_ITEM_KEYS & set(item)):
            if item[key] is None:
                _refuse(f"{where}.{key} must not be null; omit the key instead")
            if not isinstance(item[key], str):
                _refuse(f"{where}.{key} must be a string")


def _check_type(field: str, spec: Arg, value) -> None:
    kind = spec.json_type
    if kind == STRING:
        if not isinstance(value, str):
            _refuse(f"{field} must be a string")
        if spec.uuid and not _UUID_RE.match(value):
            _refuse(f"{field} must be a uuid")
    elif kind == INTEGER:
        if not _is_integer(value):
            _refuse(f"{field} must be an integer")
    elif kind == BOOLEAN:
        if not isinstance(value, bool):
            _refuse(f"{field} must be a boolean")
    elif kind == OBJECT:
        if not isinstance(value, dict) or isinstance(value, bool):
            _refuse(f"{field} must be an object")
    elif kind in (ARRAY_OF_STRING, ARRAY_OF_UUID):
        if not isinstance(value, list):
            _refuse(f"{field} must be an array of strings")
        for index, item in enumerate(value):
            if not isinstance(item, str):
                _refuse(f"{field}[{index}] must be a string")
            if kind == ARRAY_OF_UUID and not _UUID_RE.match(item):
                _refuse(f"{field}[{index}] must be a uuid")
    elif kind == ARRAY_OF_LINK_ITEMS:
        _check_link_items(field, value)
    else:  # pragma: no cover -- a spec typo, not caller input
        raise AssertionError(f"unknown wire type {kind!r} for {field}")

    if spec.enum is not None and value not in spec.enum:
        _refuse(f"{field} must be one of {'|'.join(spec.enum)}")


def validate(core_name: str, arguments: dict) -> None:
    """Validate a resolved core tool's merged arguments. Raises
    `ToolError("VALIDATION", ...)` naming field and rule (07's error contract);
    returns None when the call is well-formed.

    An unknown `core_name` is not this layer's error to raise -- the funnel has
    already refused unknown tool names by then -- so it passes through rather
    than inventing a second "unknown tool" message with different wording.
    """
    spec = SPEC.get(core_name)
    if spec is None:  # pragma: no cover -- resolve_dispatch gates this first
        return

    for name in sorted(arguments):
        if name not in spec:
            _refuse(f"unknown argument '{name}' for {core_name}; accepted "
                    f"arguments are {', '.join(sorted(spec)) or '(none)'}")

    # Presence before types: a caller who omitted a required key gets told
    # that, not a type complaint about a key they did supply.
    for name in sorted(spec):
        value_present = name in arguments
        if value_present and arguments[name] is None:
            _refuse(f"{name} must not be null; omit the key instead")
        if not value_present and spec[name].is_required(arguments):
            _refuse(f"{name} is required for {core_name}"
                    + _conditional_because(core_name, name, arguments))

    for name in sorted(arguments):
        _check_type(name, spec[name], arguments[name])


def _conditional_because(core_name: str, name: str, arguments: dict) -> str:
    """The two conditional rows earn a clause saying WHICH condition made them
    required -- a model retrying on "merge_into is required" with no mention of
    `resolution` has been told a rule it cannot see in its own request."""
    if core_name == "resolve_duplicate" and name == "merge_into":
        return " when resolution is 'merge'"
    if core_name == "inbox_review":
        return f" when action is '{arguments.get('action')}'"
    return ""


# --- Published schema, generated from the same spec --------------------------

_JSON_SCHEMA_TYPES = {
    STRING: {"type": "string"},
    INTEGER: {"type": "integer"},
    BOOLEAN: {"type": "boolean"},
    OBJECT: {"type": "object"},
    ARRAY_OF_STRING: {"type": "array", "items": {"type": "string"}},
    ARRAY_OF_UUID: {"type": "array", "items": {"type": "string", "format": "uuid"}},
    ARRAY_OF_LINK_ITEMS: {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "src_id": {"type": "string", "format": "uuid"},
                "dst_id": {"type": "string", "format": "uuid"},
            },
            "required": ["type"],
            "additionalProperties": False,
        },
    },
}


def _property_schema(spec: Arg) -> dict:
    # deepcopy, not dict(): the templates nest (an array's `items`, a link
    # item's `properties`), and a shallow copy would hand every tool the SAME
    # inner dicts -- one caller mutating its published schema would silently
    # edit another tool's.
    schema = copy.deepcopy(_JSON_SCHEMA_TYPES[spec.json_type])
    if spec.uuid:
        schema["format"] = "uuid"
    if spec.enum is not None:
        schema["enum"] = list(spec.enum)
    return schema


def input_schema(core_name: str) -> dict:
    """The tool's published JSON Schema, generated from the same spec that
    enforces -- so the advertised surface and the enforced one cannot drift.

    Advertisement only. Nothing validates against this; `validate()` above is
    what refuses, and `validate_input` stays False (see the module docstring).

    `required` carries only the UNCONDITIONALLY required arguments. Flat JSON
    Schema cannot express "merge_into when resolution is merge" or
    inbox_review's per-action requirements, and a schema that overstated them
    would advertise a stricter surface than the server enforces -- the drift
    this generation exists to prevent, in the other direction. The conditionals
    live in `validate()` alone.
    """
    spec = SPEC.get(core_name, {})
    return {
        "type": "object",
        "properties": {name: _property_schema(arg) for name, arg in sorted(spec.items())},
        "required": sorted(name for name, arg in spec.items() if arg.required is True),
        "additionalProperties": False,
    }
