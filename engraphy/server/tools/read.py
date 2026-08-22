"""briefing / search / traverse / get. Read paths bump recall stats (batched).

Each function here is a thin dispatcher: (pool, ctx, arguments) -> the 07 wire
envelope. Auth, rate-limiting, and alias resolution are the transport layer's
job (app.py, E2-plan.md s.6) -- see write.py's module docstring for the same
note; it applies identically here.

The two scope-taking tools (search, briefing) additionally thread
`ctx.no_scope_all` -- the per-token scope restriction (migration 0023) -- into
the core function, which enforces it in core.scope_set. The core layer takes
primitives rather than the AuthContext, so this dispatcher is where the bearer's
capability meets the call, the same way `require_write` reads `ctx.role` up in
app.py. A THIRD tool that accepts `scope` must pass it too; the guard itself
lives in the shared resolver so forgetting the argument is the only way to miss
it, and the argument defaults to unrestricted rather than to restricted because
every non-tool caller (admin, tests, the briefing engine's own recursion) holds
no token at all.
"""
from engraphy.core import metrics as core_metrics
from engraphy.core.briefing import briefing as core_briefing
from engraphy.core.get import get as core_get
from engraphy.core.pending import pending_list as core_pending_list
from engraphy.core.search import search as core_search
from engraphy.core.traverse import traverse as core_traverse
from engraphy.server.auth import ToolError
from engraphy.server.db import transaction
from engraphy.server.tools.errors import to_tool_error

_DETAIL_VALUES = ("full", "summary")


async def get(pool, ctx, arguments: dict) -> dict:
    """CORE_TOOLS['get']: {ids}. design/06: non-readable/unknown ids collapse
    into the `missing` list -- existence is information, never an error -- so
    core.get.get() itself never raises for a bad id; this dispatcher exists
    for the uniform (pool, ctx, arguments) shape and the catch-all INTERNAL
    translation of anything genuinely unexpected."""
    try:
        return await core_get(pool, ctx.space_id, ctx.principal, arguments["ids"])
    except Exception as exc:
        raise to_tool_error(exc) from exc


async def search(pool, ctx, arguments: dict) -> dict:
    """CORE_TOOLS['search']: {scope, query, types?, limit?, include_inactive?,
    detail?}. `detail` is validated here (full|summary) -- core.search.search()
    silently treats anything not 'full' as summary, which would otherwise let
    a typo through unnoticed; `limit` is already clamped inside the core fn,
    and an unregistered `types` entry just matches nothing (no error needed,
    same as any other filter on dynamic per-space data)."""
    detail = arguments.get("detail", "full")
    if detail not in _DETAIL_VALUES:
        raise ToolError("VALIDATION", f"detail must be one of {'|'.join(_DETAIL_VALUES)}")
    try:
        return await core_search(
            pool, ctx.space_id, ctx.principal, arguments["scope"], arguments["query"],
            ctx.client_name, types=arguments.get("types"),
            limit=arguments.get("limit", 25), include_inactive=arguments.get("include_inactive", False),
            detail=detail, no_scope_all=ctx.no_scope_all,
        )
    except Exception as exc:
        raise to_tool_error(exc) from exc


async def pending_list(pool, ctx, arguments: dict) -> dict:
    """CORE_TOOLS['pending_list']: {limit?, offset?}. Read-only list of the
    caller's own pending duplicate-check writes (the write/dedup PENDING band),
    scoped by RLS to space + principal. limit/offset are clamped inside the
    core fn (same boring over-limit handling as search/get). No confirmable
    argument surface -- this only reads."""
    try:
        return await core_pending_list(
            pool, ctx.space_id, ctx.principal,
            limit=arguments.get("limit", 25), offset=arguments.get("offset", 0),
        )
    except Exception as exc:
        raise to_tool_error(exc) from exc


async def traverse(pool, ctx, arguments: dict) -> dict:
    """CORE_TOOLS['traverse']: {start_id, edge_types?, direction, max_depth?,
    limit?, detail?}. direction is required (design/07); the core fn already
    raises ValueError for an invalid one, which to_tool_error maps to
    ENGRAPHY_VALIDATION. max_depth/limit are clamped inside the core fn."""
    detail = arguments.get("detail", "summary")
    if detail not in _DETAIL_VALUES:
        raise ToolError("VALIDATION", f"detail must be one of {'|'.join(_DETAIL_VALUES)}")
    try:
        return await core_traverse(
            pool, ctx.space_id, ctx.principal, arguments["start_id"], arguments.get("direction"),
            edge_types=arguments.get("edge_types"), max_depth=arguments.get("max_depth", 4),
            limit=arguments.get("limit", 50), detail=detail,
        )
    except Exception as exc:
        raise to_tool_error(exc) from exc


_GROUP_BY_VALUES = ("space", "user")


async def stats(pool, ctx, arguments: dict) -> dict:
    """CORE_TOOLS['stats']: {range_days?, group_by?}. Read-only, RLS-scoped usage
    metrics: totals + a zero-filled daily series over the last range_days UTC
    days (default 30), at the group_by grain -- 'space' (default; aggregate
    across all principals) or 'user' (the calling principal only). range_days is
    clamped inside the core fn (1.._MAX); group_by is enum-enforced by the wire
    layer, and re-checked here as defense in depth (mirroring search/traverse's
    own `detail` guard -- their messages were never pinned, only their codes). No
    confirmable surface -- this only reads (see core.metrics for the metric
    definitions and the frozen envelope shape)."""
    group_by = arguments.get("group_by", "space")
    if group_by not in _GROUP_BY_VALUES:
        raise ToolError("VALIDATION", f"group_by must be one of {'|'.join(_GROUP_BY_VALUES)}")
    try:
        return await core_metrics.stats(
            pool, ctx.space_id, ctx.principal,
            range_days=arguments.get("range_days", 30), group_by=group_by,
        )
    except Exception as exc:
        raise to_tool_error(exc) from exc


async def _resolve_pack_briefing_config(pool, ctx) -> dict:
    """The applied pack's `briefing:` fragment, persisted at `pack apply` time
    under config key 'pack.briefing' (packs.py::apply -- config is the
    existing per-space settings channel, not a new table or a filesystem pack
    registry). Absent (no pack applied yet, or a pack with no briefing
    section) -> an empty briefing config, same as briefing()'s own
    `sections_cfg = config.get("sections", [])` default."""
    async with transaction(pool, ctx.space_id, ctx.principal) as conn:
        cur = conn.cursor()
        await cur.execute(
            "SELECT value FROM config WHERE space_id = %s AND key = 'pack.briefing'",
            (ctx.space_id,),
        )
        row = await cur.fetchone()
    return row[0] if row else {}


async def briefing(pool, ctx, arguments: dict) -> dict:
    """CORE_TOOLS['briefing']: {scope, hint?}. The engine itself is
    pack-agnostic (core.briefing.briefing()'s own docstring: "the caller ...
    owns pack lookup"); this dispatcher is that caller."""
    config = await _resolve_pack_briefing_config(pool, ctx)
    try:
        return await core_briefing(
            pool, ctx.space_id, ctx.principal, arguments["scope"], arguments.get("hint"),
            ctx.client_name, config, no_scope_all=ctx.no_scope_all,
        )
    except Exception as exc:
        raise to_tool_error(exc) from exc
