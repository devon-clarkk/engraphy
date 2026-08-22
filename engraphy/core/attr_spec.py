"""Python mirror of the plpgsql attr-spec interpreter.

Normative: design/implementation/attr-spec-interpreter-plan.md — algorithm phases,
exact error strings, and deterministic ordering are contract. This mirror exists for
friendly pydantic-layer errors and pack conformance scans; the plpgsql function is
the authority. BOTH run the same fixture file; a parity fuzzer holds them identical.
"""

import datetime
import re

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Engine-reserved attr keys: never pack-declarable, never caller-suppliable, and
# therefore exempt from the Phase-3 closed-spec unknown-key check. `addenda` is
# dedup.py's merge-history append target (QUESTIONS.md "get-addenda-shape",
# 2026-07-18); the merge write is an UPDATE that re-fires the validate trigger,
# so without this exemption no `closed: true` node type can ever receive a second
# dedup occurrence. Migration 0017 carries the identical exemption on the plpgsql
# side -- the parity fuzzer holds the two definitions identical, so this set and
# migration 0017's `k <> 'addenda'` guard must change together.
RESERVED_ATTR_KEYS = frozenset({"addenda"})


def searchable_keys(attr_spec: dict) -> set:
    """Phase C (fact-searchability-phase-c.md §1): the set of attr keys whose
    values enter the searchable surface for a node type, per the three-clause
    rule, evaluated per key:

    1. reserved keys (RESERVED_ATTR_KEYS) never (their bodies reach the tsvector
       via addenda weight-D, never the embedding);
    2. explicit `searchable: true|false` on the rule object wins;
    3. construct default: `{type: string}` / `{type: date}` are searchable,
       `{enum: ...}` / `{type: bool|int|number}` are not (discriminative content
       in, homogenizing low-cardinality flags out).

    `attr_spec` is the node type's registered spec (the `{"attrs": {...}}` shape).
    Pure -- the write path resolves this once per write before embedding."""
    attrs = (attr_spec or {}).get("attrs") or {}
    keys: set = set()
    for section in ("required", "optional"):
        for key, rule in (attrs.get(section) or {}).items():
            if key in RESERVED_ATTR_KEYS:
                continue
            if not isinstance(rule, dict):
                continue
            if "searchable" in rule:
                if rule["searchable"]:
                    keys.add(key)
                continue
            if "enum" in rule:
                continue
            if rule.get("type") in ("string", "date"):
                keys.add(key)
    return keys


def _json_type(value: object) -> str:
    """Classify a Python value (as produced by json/yaml loading) the way the
    attr-spec grammar's `{type: ...}` names classify JSON values. `bool` must
    be checked before `int`/`float` -- Python's bool is an int subclass, but
    JSON boolean and JSON number are distinct types."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"  # pragma: no cover


def _check_value(key: str, value: object, rule: dict) -> str | None:
    """Phase-4 per-key check. Returns the error string, or None if it passes."""
    if "enum" in rule:
        choices = rule["enum"]
        if _json_type(value) != "string" or value not in choices:
            return f"attrs.{key} must be one of {'|'.join(choices)}"
        return None

    kind = rule.get("type")
    if kind == "string":
        if _json_type(value) != "string":
            return f"attrs.{key} must be a string"
        if len(value) > 2000:
            return f"attrs.{key} must be at most 2000 characters"
        return None
    if kind == "int":
        if _json_type(value) != "number":
            return f"attrs.{key} must be a int"
        if value != int(value):  # trunc-equality: 5.0 passes, 5.1 fails
            return f"attrs.{key} must be a int"
        return None
    if kind == "number":
        if _json_type(value) != "number":
            return f"attrs.{key} must be a number"
        return None
    if kind == "bool":
        if _json_type(value) != "boolean":
            return f"attrs.{key} must be a bool"
        return None
    if kind == "date":
        if _json_type(value) != "string" or not _DATE_RE.match(value):
            return f"attrs.{key} must be a date"
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            return f"attrs.{key} must be a valid ISO date"
        return None
    raise ValueError(f"unrecognized attr-spec rule for {key!r}: {rule!r}")  # pragma: no cover


def validate_attrs(spec: dict, attrs: dict) -> list[str]:
    """Ordered error strings; empty list = valid. JSON null is a PRESENT value.

    Phase order: (1) required-presence, (2) conditional-presence (array order),
    (3) unknown-keys under `closed`, (4) per-key value checks -- phases 1/3/4
    iterate their keys in lexicographic order (design/implementation/
    attr-spec-interpreter-plan.md §Algorithm). The interpreter tolerates a
    spec missing `required`/`optional`/`closed`/`requires` entirely, treating
    each as its identity default (empty map, empty list, True respectively).
    """
    attrs_section = spec.get("attrs") or {}
    req: dict = attrs_section.get("required") or {}
    opt: dict = attrs_section.get("optional") or {}
    cond: list = attrs_section.get("requires") or []
    closed: bool = attrs_section.get("closed", True)
    known = set(req) | set(opt)

    errors: list[str] = []

    # Phase 1 — required presence, lexicographic over req keys.
    for key in sorted(req):
        if key not in attrs:
            errors.append(f"attrs.{key} is required")

    # Phase 2 — conditional presence, in ARRAY order (author's order).
    for c in cond:
        when = c.get("when", {})
        when_key = when.get("key")
        equals = when.get("equals")
        cond_key = c.get("key")
        when_value = attrs.get(when_key)
        if (
            when_key in attrs
            and isinstance(when_value, str)
            and when_value == equals
            and cond_key not in attrs
        ):
            errors.append(f"attrs.{cond_key} is required when {when_key}={equals}")

    # Phase 3 — unknown keys under a closed spec, lexicographic over attrs keys.
    # Engine-reserved keys are exempt (see RESERVED_ATTR_KEYS); migration 0017
    # carries the same exemption in plpgsql. Phase 4 needs no equivalent guard:
    # a reserved key is by definition never in `known`, so it is already skipped
    # there on both sides.
    if closed:
        for key in sorted(attrs):
            if key not in known and key not in RESERVED_ATTR_KEYS:
                errors.append(f"attrs.{key} is not allowed (closed spec)")

    # Phase 4 — per-key value checks, lexicographic over attrs keys that are known.
    for key in sorted(attrs):
        if key not in known:
            continue
        rule = req[key] if key in req else opt[key]
        error = _check_value(key, attrs[key], rule)
        if error:
            errors.append(error)

    return errors
