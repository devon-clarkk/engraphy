"""pack validate | apply | upgrade.

validate: JSON Schema (packs/schema.json == design/07 §Pack file schema) + cross-refs
(edge_rules/briefing types exist; aliases bind core tools only).
apply: expand '*' rules to concrete rows; registries written ONLY here.
upgrade: diff -> additive | tightening (conformance scan via engram_validate_attrs;
violators are refused by default, or applied with --allow-nonconforming -- see
upgrade()'s docstring for why nonconformance is derived on demand rather than
stored, per DECISIONS-DELTA.md "attrs_nonconforming is derived, not stored")
| destructive (refused while active rows/edges exist). design/04 §Pack migrations.
"""

import datetime
import json
import pathlib
import re

import jsonschema
import psycopg
import yaml
from psycopg.types.json import Jsonb

from engraphy.core.attr_spec import RESERVED_ATTR_KEYS
from engraphy.core.sentinel import RESERVED_NODE_TYPES, SENTINEL_NODE_TYPE

SCHEMA_PATH =pathlib.Path(__file__).resolve().parents[2] / "packs" / "schema.json"

# This engine's own understanding of the pack FILE FORMAT (packs/schema.json's
# shape) -- bump only when the schema itself gains/changes constructs, never
# for a shipped pack's own content revision (that's `pack.version`). design/04
# s.Versioning and release discipline: "The server logs pack-format warnings
# when a pack uses constructs newer than it declares." A precise per-construct
# check would need every schema addition tagged with the format version that
# introduced it, which packs/schema.json does not do today -- see
# check_pack_format()'s docstring and DECISIONS-DELTA.md "pack-format warning
# scope" for the narrower, checkable version implemented instead.
#
# Bumped to 2 in Phase C (fact-searchability-model.md I2): the `searchable`
# attr-rule flag is the first format-2 construct. An engine older than Phase C
# understands only format 1 and silently ignores `searchable`, embedding those
# attrs out of the searchable surface -- check_pack_format() now surfaces exactly
# that hazard (the first per-construct warning this function grew).
CURRENT_PACK_FORMAT = 2

# design/03 §The tool surface -- the core tool table, argument names as
# listed there. tool_aliases may bind only to these and preset only these
# argument names (design/07 §Pack file schema, "Cross-reference validation
# beyond the schema"). admin_* tools are role-gated, not alias-bindable
# sugar, so they are deliberately excluded.
CORE_TOOLS = {
    "briefing": {"scope", "hint"},
    "search": {"scope", "query", "types", "limit", "include_inactive", "detail"},
    "traverse": {"start_id", "edge_types", "direction", "max_depth", "limit", "detail"},
    "get": {"ids"},
    "write": {"scope", "type", "title", "body", "attrs", "links", "session_id"},
    "link": {"edges"},
    "update": {"id", "title", "body", "attrs"},
    "supersede": {"old_id", "scope", "type", "title", "body", "attrs", "links", "session_id"},
    "resolve_duplicate": {"pending_id", "resolution", "merge_into"},
    "scope_list": set(),
    "scope_create": {"id", "display_name", "confirm"},
    "inbox_review": {"action"},
}

_RELATIVE_DATE_RE = re.compile(r"^\+\d+d$")

# Same pattern packs/schema.json uses for node_types/edge_types keys and the
# attrmap $def's patternProperties key, and that design/01's DDL enforces via
# CHECK (name ~ '^[a-z][a-z0-9_]{1,40}$') on node_types.name / edge_types.name.
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


def validate_name_patterns(pack: dict) -> list[str]:
    """Reject node/edge type names and attr-map keys that JSON Schema's
    `patternProperties` alone does not catch.

    `packs/schema.json` has no `additionalProperties: false` on `node_types`,
    `edge_types`, or the `attrmap` $def -- per JSON Schema semantics,
    `patternProperties` only constrains keys that MATCH the pattern, so a key
    that doesn't match is an unconstrained extra property and validates
    cleanly under `jsonschema` alone. QUESTIONS.md "pack-schema" (resolved):
    catch it explicitly here, at `pack validate` time, rather than relying on
    the DB CHECK constraint to reject it later as a raw constraint violation
    at `pack apply`/INSERT time.

    Ordered error strings; empty = every name conforms. Order: node_types
    keys (lexicographic), then edge_types keys (lexicographic), then each
    node type's attr keys (node type lexicographic, then required before
    optional, then attr key lexicographic) -- deterministic, but NOT a 07
    contract (07 does not specify pack-validate error text; see
    DECISIONS-DELTA.md).
    """
    errors: list[str] = []

    for name in sorted(pack.get("node_types", {}) or {}):
        if not _NAME_PATTERN.fullmatch(name):
            errors.append(f"node_types.{name} is not a valid type name")

    for name in sorted(pack.get("edge_types", {}) or {}):
        if not _NAME_PATTERN.fullmatch(name):
            errors.append(f"edge_types.{name} is not a valid type name")

    node_types = pack.get("node_types", {}) or {}
    for type_name in sorted(node_types):
        attrs = (node_types[type_name] or {}).get("attrs", {}) or {}
        for section in ("required", "optional"):
            keys = attrs.get(section, {}) or {}
            for key in sorted(keys):
                if not _NAME_PATTERN.fullmatch(key):
                    errors.append(
                        f"node_types.{type_name}.attrs.{section}.{key} is not a valid attr key"
                    )

    return errors


def validate_reserved_names(pack: dict) -> list[str]:
    """Reject the two names design/07 s.Pack file schema reserves for the engine:
    the node type `engram_sentinel` and the attrs key `addenda`.

    The reserved node-type name(s) come from `sentinel.RESERVED_NODE_TYPES`,
    the same frozenset `dedup._validate_not_reserved_type` reads, so the
    pack-authoring refusal and the write-path refusal (ruled 2026-07-21) can
    never drift apart.

    Both are engine-owned surfaces a pack must not be able to collide with.
    `engram_sentinel` is registered by `space create`, so a pack declaring it
    would fail at `pack apply` on a primary-key violation and at `pack upgrade`
    would look like the operator hand-edited the registry -- a confusing DB error
    for what is really a pack authoring mistake. `addenda` is the engine-managed
    merge history under `attrs` (RESERVED_ATTR_KEYS, migration 0017): a pack
    declaring it would be writing rules for a key the engine overwrites.

    Caught here rather than at apply time for the same reason
    validate_name_patterns() is: the operator should get a sentence about their
    pack file, not a constraint violation from three layers down.
    """
    errors: list[str] = []

    declared_types = pack.get("node_types", {}) or {}
    for reserved in sorted(RESERVED_NODE_TYPES & set(declared_types)):
        errors.append(
            f"node_types.{reserved} is a reserved engine-owned type "
            f"(minted by `space create` for verify-restore) and may not be declared by a pack")

    if SENTINEL_NODE_TYPE in (pack.get("edge_types", {}) or {}):
        errors.append(f"edge_types.{SENTINEL_NODE_TYPE} is a reserved engine-owned name")

    node_types = pack.get("node_types", {}) or {}
    for type_name in sorted(node_types):
        attrs = (node_types[type_name] or {}).get("attrs", {}) or {}
        for section in ("required", "optional"):
            for key in sorted(attrs.get(section, {}) or {}):
                if key in RESERVED_ATTR_KEYS:
                    errors.append(
                        f"node_types.{type_name}.attrs.{section}.{key} is an engine-reserved "
                        f"attrs key and may not be declared by a pack")

    return errors


def _is_iso_date(value: str) -> bool:
    try:
        datetime.date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def load_pack_file(path: str | pathlib.Path) -> dict:
    return yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))


def validate(pack: dict) -> list[str]:
    """JSON Schema (packs/schema.json) + cross-reference validation beyond it
    (design/07 §Pack file schema): edge_rules/briefing types and edges must
    name a type defined in this pack (or '*' where permitted), before/after
    accept ISO dates or +Nd relative form, aliases bind only to core tools
    and preset only that tool's declared arguments -- plus the name-pattern
    check validate_name_patterns() adds (QUESTIONS.md "pack-schema").

    Ordered error strings; empty = valid. Schema errors short-circuit the
    cross-reference pass (which assumes a schema-valid shape to walk).
    """
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    schema_errors = [
        f"schema: {'.'.join(str(p) for p in e.path)}: {e.message}"
        for e in jsonschema.Draft202012Validator(schema).iter_errors(pack)
    ]
    if schema_errors:
        return schema_errors

    errors = [f"name-pattern: {e}" for e in validate_name_patterns(pack)]
    errors.extend(f"reserved: {e}" for e in validate_reserved_names(pack))

    node_type_names = set(pack.get("node_types", {}))
    edge_type_names = set(pack.get("edge_types", {}))

    for i, rule in enumerate(pack.get("edge_rules", [])):
        if rule["type"] not in edge_type_names:
            errors.append(f"edge_rules[{i}]: type '{rule['type']}' is not defined in edge_types")
        if rule["src"] != "*" and rule["src"] not in node_type_names:
            errors.append(f"edge_rules[{i}]: src '{rule['src']}' is not defined in node_types")
        if rule["dst"] != "*" and rule["dst"] not in node_type_names:
            errors.append(f"edge_rules[{i}]: dst '{rule['dst']}' is not defined in node_types")

    briefing = pack.get("briefing", {}) or {}
    for i, section in enumerate(briefing.get("sections", [])):
        types_to_check = []
        if "type" in section:
            types_to_check.append(section["type"])
        types_to_check.extend(section.get("types", []))
        for t in types_to_check:
            if t not in node_type_names:
                errors.append(f"briefing.sections[{i}]: type '{t}' is not defined in node_types")

        for link_key in ("include_linked", "without_edge"):
            edge = (section.get(link_key) or {}).get("edge")
            if edge and edge not in edge_type_names:
                errors.append(
                    f"briefing.sections[{i}].{link_key}: edge '{edge}' is not defined in edge_types"
                )

        where_attr = section.get("where_attr") or {}
        for key in ("before", "after"):
            if key in where_attr:
                val = where_attr[key]
                if not (_is_iso_date(val) or _RELATIVE_DATE_RE.match(val)):
                    errors.append(
                        f"briefing.sections[{i}].where_attr.{key}: '{val}' is not an ISO date "
                        "or +Nd relative form"
                    )

    for alias_name, alias in (pack.get("tool_aliases") or {}).items():
        binds = alias.get("binds")
        if binds not in CORE_TOOLS:
            errors.append(f"tool_aliases.{alias_name}: binds '{binds}' is not a core tool")
            continue
        allowed_args = CORE_TOOLS[binds]
        for key in (alias.get("preset") or {}):
            if key not in allowed_args:
                errors.append(
                    f"tool_aliases.{alias_name}.preset: '{key}' is not a declared argument of {binds}"
                )

    return errors


def check_pack_format(pack: dict) -> str | None:
    """design/04 s.Versioning and release discipline: "The server logs
    pack-format warnings when a pack uses constructs newer than it declares."
    The literal per-construct version of that check would need every
    packs/schema.json addition tagged with the format version that
    introduced it -- nothing in this codebase tracks that today, and adding
    it is a bigger design exercise than this pass (see DECISIONS-DELTA.md
    "pack-format warning scope"). What's actually checkable, and covers the
    real interoperability hazard operators hit in practice: whether the pack
    declares a `pack_format` this engine's own CURRENT_PACK_FORMAT doesn't
    understand yet (a pack authored for a newer engine, run against an older
    one) -- exactly the direction that matters when picking up someone
    else's pack file. Returns a warning string, or None if the pack's
    declared format is within what this engine understands. A warning, not a
    validate() error: an unrecognized-but-newer format may still apply
    cleanly (schema/DDL additions are usually additive), so this doesn't
    block `pack apply`/`pack upgrade`, matching the design wording's own
    "warns", not "refuses"."""
    declared = pack.get("pack_format", 1)
    if declared > CURRENT_PACK_FORMAT:
        return (
            f"pack '{pack.get('pack', '?')}' declares pack_format {declared}, "
            f"this engine only understands up to {CURRENT_PACK_FORMAT} -- "
            "some constructs it uses may not be recognized"
        )
    # Phase C: the `searchable` attr flag is a format-2 construct. A pack that
    # uses it but declares only format 1 will be silently mis-handled by a
    # pre-Phase-C engine (which ignores `searchable` and embeds those attrs out
    # of the searchable surface). Warn so that interop hazard is explained --
    # the first per-construct warning, per the design's own "constructs newer
    # than it declares" wording. Not a refusal (this engine handles it fine).
    if declared < 2 and _pack_uses_searchable(pack):
        return (
            f"pack '{pack.get('pack', '?')}' uses the `searchable` attr flag "
            f"(Phase C, fact-searchability-model.md I2) but declares pack_format "
            f"{declared}; engines older than this one ignore `searchable` and embed "
            f"those attrs out of the searchable surface. Declare `pack_format: 2` "
            f"to signal it."
        )
    return None


def _pack_uses_searchable(pack: dict) -> bool:
    """True if any attr rule object in the pack carries an explicit `searchable`
    key (§1). Scans required + optional maps of every node type."""
    for node_type in (pack.get("node_types") or {}).values():
        attrs = (node_type.get("attrs") or {})
        for section in ("required", "optional"):
            for rule in (attrs.get(section) or {}).values():
                if isinstance(rule, dict) and "searchable" in rule:
                    return True
    return False


def check_same_topic_declared(pack: dict) -> str | None:
    """Phase B (design/analysis/fact-searchability-model.md §2.2; implementation
    fact-searchability-phase-b.md §1.3): the merge path attaches a `same_topic`
    edge between two near-duplicate writes it keeps both of (a fact cluster). If
    the pack does not declare the edge type AND a covering rule, that link is
    skipped gracefully -- both memories are still saved and searchable (I1
    holds), only the cluster's graph edge is lost. Warn about it so the omission
    is never silent. Like `check_pack_format`, this is a warning, never a
    validate() error: a pack that predates the type still applies cleanly and its
    writes still succeed. Returns the warning string, or None when the pack
    declares `same_topic` with at least one edge_rules row of that type."""
    edge_types = pack.get("edge_types", {}) or {}
    rules = pack.get("edge_rules", []) or []
    has_type = "same_topic" in edge_types
    has_rule = any(rule.get("type") == "same_topic" for rule in rules)
    if has_type and has_rule:
        return None
    return (
        "pack does not declare same_topic (+ wildcard rule): the merge path will "
        "save both near-duplicates but cannot link them; clusters lose their "
        "graph edge"
    )


def expand_edge_rules(pack: dict) -> set[tuple[str, str, str]]:
    """'*' wildcards expanded to concrete (type, src_type, dst_type) rows
    (design/01: "the runtime check is a plain lookup, never wildcard logic").
    Shared by apply() and upgrade()'s diff so both expand identically."""
    node_type_names = list(pack.get("node_types", {}).keys())
    expanded: set[tuple[str, str, str]] = set()
    for rule in pack.get("edge_rules", []):
        srcs = node_type_names if rule["src"] == "*" else [rule["src"]]
        dsts = node_type_names if rule["dst"] == "*" else [rule["dst"]]
        for src in srcs:
            for dst in dsts:
                expanded.add((rule["type"], src, dst))
    return expanded


def apply(pack: dict, space_id: str, cur) -> None:
    """Write node_types/edge_types/edge_rules to the registry. Registries are
    written ONLY here (and by upgrade(), the versioned sibling of this path).
    Caller must have already run validate(pack) == [] -- apply does not
    re-validate, matching pack-validate/pack-apply as two distinct steps.
    """
    for name, spec in pack.get("node_types", {}).items():
        attr_spec = {"attrs": spec.get("attrs", {}) or {}}
        cur.execute(
            "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES (%s, %s, %s, %s)",
            (space_id, name, spec["description"], Jsonb(attr_spec)),
        )

    for name, spec in pack.get("edge_types", {}).items():
        cur.execute(
            "INSERT INTO edge_types (space_id, name, description, bidirectional) "
            "VALUES (%s, %s, %s, %s)",
            (space_id, name, spec["description"], spec.get("bidirectional", False)),
        )

    for type_, src, dst in expand_edge_rules(pack):
        cur.execute(
            "INSERT INTO edge_rules (space_id, type, src_type, dst_type) "
            "VALUES (%s, %s, %s, %s)",
            (space_id, type_, src, dst),
        )

    cur.execute(
        "UPDATE spaces SET pack_name = %s, pack_version = %s WHERE id = %s",
        (pack["pack"], pack["version"], space_id),
    )

    # The briefing tool needs the applied pack's `briefing:` fragment at call
    # time, app.py needs `tool_aliases` to register alias tool names
    # (aliases.build_aliases) and `tool_descriptions` for per-space
    # description assembly (design/03: "engine base text + pack
    # tool_descriptions overrides") -- and apply() is the only place that
    # ever sees the full pack file; spaces.pack_name/pack_version record
    # WHICH pack, not its content. config is the existing per-space settings
    # channel (dedup thresholds, resonance floor, rate limits all live there
    # already), so this reuses it rather than adding a new table or a
    # filesystem pack registry. One row per non-registry fragment (not one
    # combined blob) so a reader that only needs one (briefing at call time)
    # doesn't have to parse the whole pack back out. Plain INSERT, no ON
    # CONFLICT: apply() can only run once per space (node_types' own PK
    # rejects a second call immediately); a pack upgrade is a distinct,
    # not-yet-built code path (design/04 §Pack migrations), not a second
    # apply() call.
    for key, fragment in (
        ("pack.briefing", pack.get("briefing", {}) or {}),
        ("pack.tool_aliases", pack.get("tool_aliases", {}) or {}),
        ("pack.tool_descriptions", pack.get("tool_descriptions", {}) or {}),
    ):
        cur.execute(
            "INSERT INTO config (space_id, key, value) VALUES (%s, %s, %s)",
            (space_id, key, Jsonb(fragment)),
        )


class PackUpgradeRefused(Exception):
    """A destructive change has active rows/edges in its way, or a tightening
    change has conformance violators and --allow-nonconforming wasn't given.
    `worklist` is the operator-facing detail (per-violation lines); the
    upgrade's transaction is left uncommitted -- caller must roll back."""

    def __init__(self, message: str, worklist: list[str]):
        super().__init__(message)
        self.worklist = worklist


def current_registry(space_id: str, cur) -> tuple[dict, dict, set]:
    cur.execute("SELECT name, attr_spec FROM node_types WHERE space_id = %s", (space_id,))
    node_types = {name: attr_spec for name, attr_spec in cur.fetchall()}
    cur.execute("SELECT name, bidirectional FROM edge_types WHERE space_id = %s", (space_id,))
    edge_types = {name: bidirectional for name, bidirectional in cur.fetchall()}
    cur.execute("SELECT type, src_type, dst_type FROM edge_rules WHERE space_id = %s", (space_id,))
    edge_rules = {tuple(row) for row in cur.fetchall()}
    return node_types, edge_types, edge_rules


def upgrade(pack: dict, space_id: str, cur, *, allow_nonconforming: bool = False) -> list[str]:
    """Diff the new pack's registry against the space's current one and apply
    by change class (design/04 s.Pack migrations):

    - additive (new node type / edge type / edge rule, or an existing node
      type's attr_spec changing in a way no existing row violates): applied
      immediately.
    - tightening (an existing node type's attr_spec changing such that one or
      more currently-active rows of that type would now fail
      engram_validate_attrs against it): applied only after a conformance
      scan. Classified empirically, not syntactically -- rather than
      pattern-matching "new required key / narrowed enum / closed
      false->true" against the spec JSON (fragile: it would need to
      enumerate every tightening shape engram_validate_attrs understands,
      and silently miss new ones), this runs the actual validator against
      every active row and asks whether it still passes. That is the exact
      question "tightening" exists to answer, and it can't misclassify by
      construction. Violators are refused (worklist printed, nothing
      committed) unless allow_nonconforming=True, in which case the spec is
      applied anyway and violating rows stay readable/queryable -- they are
      not flagged in a column (see doctor.py's module docstring: nonconformance
      is derived from engram_validate_attrs on demand, not stored), and the
      existing nodes_validate_attrs trigger already refuses any UPDATE to a
      violating row that doesn't fix its attrs, for free.
    - destructive (a node type, edge type, or edge rule present in the
      current registry but absent from the new pack): refused while active
      rows/edges depend on it (design/04's "archived-then-remove across two
      pack versions" -- an operator archives the data in a prior step, then
      this upgrade's second pass can proceed). If none exist, the registry
      row is deleted; DECISIONS-DELTA.md "pack-upgrade destructive removal"
      records the residual gap this can hit (a node_types row can still have
      non-active, i.e. archived/merged/superseded, rows referencing it, which
      the plain FK -- no ON DELETE CASCADE from nodes/edges to the registry
      -- will then refuse; that FK error is surfaced as-is rather than papered
      over, since silently cascading a delete through historical rows would
      violate the "no hard deletes" rule this schema is built on).

    Caller must have already run validate(pack) == [] (pack-validate is a
    distinct step, matching apply()). Raises PackUpgradeRefused, leaving the
    transaction uncommitted, on any destructive refusal or unresolved
    tightening violator. Returns operator-facing report lines on success;
    caller commits.
    """
    old_node_types, old_edge_types, old_edge_rules = current_registry(space_id, cur)
    # The engine-owned sentinel type is not pack territory: it is registered by
    # `space create`, never appears in any pack file, and so would otherwise fall
    # into Phase 3's destructive set on EVERY upgrade of EVERY space. It would
    # not even fail cleanly -- its node is archived, so the active-rows check
    # passes, the DELETE then hits the FK from that archived row, and the whole
    # upgrade is refused. Dropping it from the diff here (rather than special-
    # casing Phase 3) also keeps it out of any future diff phase by default,
    # which is the safer direction for a row packs must never manage.
    old_node_types = {n: s for n, s in old_node_types.items() if n != SENTINEL_NODE_TYPE}
    new_node_type_specs = pack.get("node_types", {})
    new_edge_type_specs = pack.get("edge_types", {})
    new_edge_rules = expand_edge_rules(pack)

    report: list[str] = []
    refusal_worklist: list[str] = []

    # Ordering matters below: edge_rules.(src_type,dst_type) FK-reference
    # node_types, and edge_rules.type FK-references edge_types, so destructive
    # removal must run edge_rules -> edge_types -> node_types (the reverse of
    # additive insertion order) or a live edge_rules row still referencing a
    # to-be-removed type raises a FK violation that (uncaught) would abort the
    # whole transaction, not just that one change. Each removal attempt that
    # CAN hit an unexpected FK failure runs inside its own SAVEPOINT so a
    # caught failure only rolls back that one DELETE, not everything already
    # applied earlier in this same call -- without it, Postgres marks the
    # whole transaction aborted the instant any statement errors, and every
    # subsequent cur.execute() in this function (even unrelated ones) would
    # raise InFailedSqlTransaction.

    # -- node types: additive (new), tightening (changed, existing rows probed
    # empirically) -- destructive removal is Phase 3, below, after edge_rules. --
    for name, spec in new_node_type_specs.items():
        new_attr_spec = {"attrs": spec.get("attrs", {}) or {}}
        if name not in old_node_types:
            cur.execute(
                "INSERT INTO node_types (space_id, name, description, attr_spec) "
                "VALUES (%s, %s, %s, %s)",
                (space_id, name, spec["description"], Jsonb(new_attr_spec)),
            )
            report.append(f"additive: node type '{name}' added")
            continue

        if old_node_types[name] == new_attr_spec:
            cur.execute(
                "UPDATE node_types SET description = %s WHERE space_id = %s AND name = %s",
                (spec["description"], space_id, name),
            )
            continue  # attr_spec unchanged -- nothing to scan, just keep description in sync

        cur.execute(
            "SELECT id, attrs FROM nodes WHERE space_id = %s AND type = %s AND status = 'active'",
            (space_id, name),
        )
        rows = cur.fetchall()
        violators = []
        for node_id, attrs in rows:
            cur.execute("SELECT engram_validate_attrs(%s, %s)", (Jsonb(new_attr_spec), Jsonb(attrs)))
            (errors,) = cur.fetchone()
            if errors:
                violators.append((node_id, errors))

        if not violators:
            cur.execute(
                "UPDATE node_types SET description = %s, attr_spec = %s WHERE space_id = %s AND name = %s",
                (spec["description"], Jsonb(new_attr_spec), space_id, name),
            )
            report.append(f"additive: node type '{name}' attr_spec changed, no existing rows affected")
        elif allow_nonconforming:
            cur.execute(
                "UPDATE node_types SET description = %s, attr_spec = %s WHERE space_id = %s AND name = %s",
                (spec["description"], Jsonb(new_attr_spec), space_id, name),
            )
            report.append(
                f"tightening: node type '{name}' applied with {len(violators)} nonconforming row(s) "
                "(--allow-nonconforming); run `engraphy-admin doctor` to list them")
        else:
            for node_id, errors in violators:
                refusal_worklist.append(f"node type '{name}', node {node_id}: {'; '.join(errors)}")

    # -- edge types: additive (new) -- destructive removal is Phase 2, below. --
    for name, spec in new_edge_type_specs.items():
        if name not in old_edge_types:
            cur.execute(
                "INSERT INTO edge_types (space_id, name, description, bidirectional) VALUES (%s, %s, %s, %s)",
                (space_id, name, spec["description"], spec.get("bidirectional", False)),
            )
            report.append(f"additive: edge type '{name}' added")
        else:
            cur.execute(
                "UPDATE edge_types SET description = %s, bidirectional = %s "
                "WHERE space_id = %s AND name = %s",
                (spec["description"], spec.get("bidirectional", False), space_id, name),
            )

    # -- edge rules: additive (new) -- destructive removal is Phase 1, below. --
    for type_, src, dst in new_edge_rules - old_edge_rules:
        cur.execute(
            "INSERT INTO edge_rules (space_id, type, src_type, dst_type) VALUES (%s, %s, %s, %s)",
            (space_id, type_, src, dst),
        )
        report.append(f"additive: edge rule '{type_}' {src} -> {dst} added")

    # Phase 1 (destructive, edge_rules -- must run before edge_types/node_types).
    for type_, src, dst in old_edge_rules - new_edge_rules:
        cur.execute(
            "SELECT count(*) FROM edges e JOIN nodes s ON s.id = e.src_id JOIN nodes d ON d.id = e.dst_id "
            "WHERE e.space_id = %s AND e.type = %s AND s.type = %s AND d.type = %s",
            (space_id, type_, src, dst),
        )
        (edge_count,) = cur.fetchone()
        if edge_count > 0:
            refusal_worklist.append(
                f"destructive: edge rule '{type_}' {src} -> {dst} has {edge_count} edge(s); "
                "archive-then-remove across two pack versions (design/04 s.Pack migrations)")
            continue
        cur.execute("SAVEPOINT upgrade_delete")
        try:
            cur.execute(
                "DELETE FROM edge_rules WHERE space_id = %s AND type = %s AND src_type = %s AND dst_type = %s",
                (space_id, type_, src, dst),
            )
        except psycopg.errors.ForeignKeyViolation as exc:
            cur.execute("ROLLBACK TO SAVEPOINT upgrade_delete")
            refusal_worklist.append(
                f"destructive: edge rule '{type_}' {src} -> {dst} could not be removed: {exc}")
            continue
        report.append(f"destructive: edge rule '{type_}' {src} -> {dst} removed")

    # Phase 2 (destructive, edge_types -- after edge_rules are gone).
    for name in old_edge_types.keys() - new_edge_type_specs.keys():
        cur.execute("SELECT count(*) FROM edges WHERE space_id = %s AND type = %s", (space_id, name))
        (edge_count,) = cur.fetchone()
        if edge_count > 0:
            refusal_worklist.append(
                f"destructive: edge type '{name}' has {edge_count} edge(s); "
                "archive-then-remove across two pack versions (design/04 s.Pack migrations)")
            continue
        cur.execute("SAVEPOINT upgrade_delete")
        try:
            cur.execute("DELETE FROM edge_types WHERE space_id = %s AND name = %s", (space_id, name))
        except psycopg.errors.ForeignKeyViolation as exc:
            cur.execute("ROLLBACK TO SAVEPOINT upgrade_delete")
            refusal_worklist.append(f"destructive: edge type '{name}' could not be removed: {exc}")
            continue
        report.append(f"destructive: edge type '{name}' removed")

    # Phase 3 (destructive, node_types -- after edge_rules are gone, since
    # edge_rules.(src_type,dst_type) FK-reference node_types).
    for name in old_node_types.keys() - new_node_type_specs.keys():
        cur.execute(
            "SELECT count(*) FROM nodes WHERE space_id = %s AND type = %s AND status = 'active'",
            (space_id, name),
        )
        (active_count,) = cur.fetchone()
        if active_count > 0:
            refusal_worklist.append(
                f"destructive: node type '{name}' has {active_count} active node(s); "
                "archive them first (two-release removal, design/04 s.Pack migrations)")
            continue
        cur.execute("SAVEPOINT upgrade_delete")
        try:
            cur.execute("DELETE FROM node_types WHERE space_id = %s AND name = %s", (space_id, name))
        except psycopg.errors.ForeignKeyViolation as exc:
            cur.execute("ROLLBACK TO SAVEPOINT upgrade_delete")
            refusal_worklist.append(
                f"destructive: node type '{name}' has no active rows but non-active "
                f"(archived/merged/superseded) rows still reference it, blocking removal: {exc}")
            continue
        report.append(f"destructive: node type '{name}' removed")

    if refusal_worklist:
        raise PackUpgradeRefused(
            f"pack upgrade refused: {len(refusal_worklist)} destructive/tightening issue(s); "
            "see worklist", refusal_worklist)

    for key, fragment in (
        ("pack.briefing", pack.get("briefing", {}) or {}),
        ("pack.tool_aliases", pack.get("tool_aliases", {}) or {}),
        ("pack.tool_descriptions", pack.get("tool_descriptions", {}) or {}),
    ):
        cur.execute(
            "INSERT INTO config (space_id, key, value) VALUES (%s, %s, %s) "
            "ON CONFLICT (space_id, key) DO UPDATE SET value = EXCLUDED.value",
            (space_id, key, Jsonb(fragment)),
        )

    cur.execute(
        "UPDATE spaces SET pack_name = %s, pack_version = %s WHERE id = %s",
        (pack["pack"], pack["version"], space_id),
    )
    report.append(f"space '{space_id}' now on pack '{pack['pack']}' version {pack['version']}")
    return report
