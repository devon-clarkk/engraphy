"""Shared exception -> wire ENGRAPHY_<CODE> translation (design/07 s.Error codes,
E2-plan.md s.6 "Error-envelope contract"). One place: dedup.py's typed
exceptions and the E0 triggers' raw psycopg.errors.CheckViolation both funnel
here, so the split between them (ENGRAPHY_VALIDATION vs ENGRAPHY_EDGE_RULE, both
raised as the SAME psycopg exception class, distinguished only by message
content) lives in exactly one function rather than once per tool.

07 forbids leaking SQL/stack traces: anything not recognized here becomes the
generic ENGRAPHY_INTERNAL text, never the raw exception message.
"""
import psycopg

from engraphy.core.dedup import (
    ConfigError,
    NotFoundError,
    PendingExpiredError,
    ScopeUnknownError,
    SupersedeUnresolvedBandError,
    ValidationError,
)
from engraphy.core.scope_set import ScopeForbiddenError
from engraphy.server.auth import ToolError

# edges_validate's trigger message (schema.sql / migration 0006): "edge_rules:
# no rule for type=%, src=%, dst=%" -- the one CheckViolation message that
# means EDGE_RULE rather than VALIDATION. Both an unregistered edge type and a
# registered type with no matching rule land here (dedup.py::
# _validate_links_shape's docstring: neither has a separate edge_types/
# edge_rules FK to fail on first).
_EDGE_RULE_PREFIX = "edge_rules: no rule for"


def to_tool_error(exc: Exception) -> ToolError:
    """Map a core-layer exception to the ToolError the transport raises.
    Callers catch the core's typed exceptions (or a raw psycopg error) and
    re-raise `to_tool_error(exc)` in its place."""
    if isinstance(exc, NotFoundError):
        return ToolError("NOT_FOUND", _strip_code(exc, "NOT_FOUND"))
    if isinstance(exc, ScopeUnknownError):
        return ToolError("SCOPE_UNKNOWN", _strip_code(exc, "SCOPE_UNKNOWN"))
    if isinstance(exc, ScopeForbiddenError):
        # ENGRAPHY_SCOPE -- the per-token scope restriction (api_tokens.
        # no_scope_all, migration 0023). Checked BEFORE ScopeUnknownError would
        # ever be reachable for the same call, and deliberately NOT collapsed
        # into it: not-found semantics exist to keep existence secret from a
        # principal, and this refusal tells the bearer something about its own
        # credential rather than about the space.
        return ToolError("SCOPE", _strip_code(exc, "SCOPE"))
    if isinstance(exc, ValidationError):
        return ToolError("VALIDATION", _strip_code(exc, "VALIDATION"))
    if isinstance(exc, PendingExpiredError):
        return ToolError("PENDING_EXPIRED", _strip_code(exc, "PENDING_EXPIRED"))
    if isinstance(exc, SupersedeUnresolvedBandError):
        # NOT a 07 error code by name (the exception is deliberately not
        # dressed up as one -- dedup.py's own docstring); the tool layer is
        # what assigns ENGRAPHY_SUPERSEDE_CONFLICT (E2-plan.md s.3 supersede row).
        return ToolError("SUPERSEDE_CONFLICT", str(exc))
    if isinstance(exc, ConfigError):
        # ENGRAPHY_INTERNAL-class per ConfigError's own docstring (operator/config
        # faults, not caller input) -- never leak the config detail to a caller.
        return ToolError("INTERNAL", "an unexpected error occurred")
    if isinstance(exc, psycopg.errors.CheckViolation):
        message = str(exc).strip()
        if message.startswith(_EDGE_RULE_PREFIX):
            return ToolError("EDGE_RULE", message)
        return ToolError("VALIDATION", message)
    if isinstance(exc, ValueError):
        # The codebase's other validation idiom, used where a typed
        # ValidationError would be overkill (traverse's direction enum,
        # resolve_duplicate's resolution enum / merge_into precondition) --
        # always caller-input-shaped, so always VALIDATION.
        return ToolError("VALIDATION", str(exc))
    if isinstance(exc, KeyError):
        # Every dispatcher reads required wire arguments via arguments["x"]
        # inside its own try/except Exception block (tools/write.py,
        # tools/read.py, ...) rather than pre-validating presence, so a missing
        # required argument reaches here as a bare KeyError rather than a
        # jsonschema error (`validate_input=False` is permanent -- see app.py).
        # Since 2026-07-21 wire_types.validate() refuses missing required
        # arguments at the funnel BEFORE dispatch, so this branch is now defense
        # in depth rather than the primary path: it still catches any direct
        # dispatcher call and any argument the table does not yet cover.
        # Caller-input-shaped, same as the ValueError branch above.
        return ToolError("VALIDATION", f"missing required argument: {exc}")
    return ToolError("INTERNAL", "an unexpected error occurred")


def _strip_code(exc: Exception, code: str) -> str:
    """dedup.py's typed exceptions carry the full 'ENGRAPHY_<CODE>: <sentence>'
    text as their message (so a bare `raise` elsewhere still reads correctly);
    ToolError re-adds the 'ENGRAPHY_<CODE>: ' prefix itself, so strip it here
    rather than double it."""
    text = str(exc)
    prefix = f"ENGRAPHY_{code}: "
    return text.removeprefix(prefix)
