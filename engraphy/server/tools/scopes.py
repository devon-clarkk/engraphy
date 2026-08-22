"""scope_list / scope_create(confirm=true) / scope_guide. New scopes default
private to creator and require a description.

Each function here is a thin dispatcher: (pool, ctx, arguments) -> the 07
wire envelope. Auth (require_write), rate-limiting, and alias resolution are
the transport layer's job (app.py, E2-plan.md s.6) -- see write.py's module
docstring for the same note; it applies identically here.
"""
from engraphy.core.scopes import scope_create as core_scope_create
from engraphy.core.scopes import scope_guide as core_scope_guide
from engraphy.core.scopes import scope_list as core_scope_list
from engraphy.server.tools.errors import to_tool_error


async def scope_list(pool, ctx, arguments: dict) -> dict:
    """CORE_TOOLS['scope_list']: no arguments."""
    try:
        return await core_scope_list(pool, ctx.space_id, ctx.principal)
    except Exception as exc:
        raise to_tool_error(exc) from exc


async def scope_guide(pool, ctx, arguments: dict) -> dict:
    """CORE_TOOLS['scope_guide']: no arguments. Read-only routing manifest --
    every scope this token can read, each with its description. RLS-scoped."""
    try:
        return await core_scope_guide(pool, ctx.space_id, ctx.principal)
    except Exception as exc:
        raise to_tool_error(exc) from exc


async def scope_create(pool, ctx, arguments: dict) -> dict:
    """CORE_TOOLS['scope_create']: {id, display_name, description, confirm}.
    `description` is read via .get so a dispatcher-level caller that omits it
    (bypassing wire_types.validate's required check) still collapses to the
    core's non-empty ENGRAPHY_VALIDATION rather than a KeyError/INTERNAL."""
    try:
        return await core_scope_create(
            pool, ctx.space_id, ctx.principal, arguments["id"], arguments["display_name"],
            arguments.get("description", ""), arguments.get("confirm", False),
        )
    except Exception as exc:
        raise to_tool_error(exc) from exc
