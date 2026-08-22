"""Per-space MCP tool assembly. Two jobs app.py delegates here:

1. `list_tools_for_space`: the twelve core tools (design/03's table; admin_*
   tools are a later build step -- see CORE_DISPATCH's own note) plus this
   space's pack aliases, each with a description (engine base text, or the
   pack's `tool_descriptions` override -- design/03: "engine base text + pack
   tool_descriptions overrides") and an inputSchema GENERATED from
   `wire_types.SPEC`, the same transcription of 07's per-argument table that
   `app.py::handle_call_tool` enforces against. Advertised and enforced
   surfaces therefore cannot drift. The schema is still never a validation
   source -- `validate_input=False` is permanent design (see app.py's docstring
   for the two reasons) -- and dispatchers keep their own required-argument
   reads as defense in depth.
2. `resolve_dispatch`: one incoming `tools/call` name -> (core tool name,
   its dispatcher, merged arguments, audit action). Direct for a core tool
   name; through `aliases.resolve_alias_call` for a pack alias name.

Pack fragments (`pack.tool_aliases` / `pack.tool_descriptions`) are read
fresh from `config` on every call, not cached -- packs.apply() runs once per
space today (pack upgrade is a distinct, not-yet-built code path), so the
read is cheap and this avoids inventing a cache-invalidation story for a
mutation path that doesn't exist yet.
"""
from engraphy.server import aliases, wire_types
from engraphy.server.tools.admin import (
    admin_grant,
    admin_member_add,
    admin_scope_visibility,
    admin_token_create,
)
from engraphy.server.tools.inbox import inbox_review
from engraphy.server.tools.read import briefing, get, pending_list, search, stats, traverse
from engraphy.server.tools.scopes import scope_create, scope_guide, scope_list
from engraphy.server.tools.write import link, resolve_duplicate, supersede, update, write

# tool_name -> (pool, ctx, arguments) -> dict. The single dispatch table both
# list_tools_for_space and resolve_dispatch walk for the always-present core
# surface.
CORE_DISPATCH = {
    "write": write,
    "resolve_duplicate": resolve_duplicate,
    "update": update,
    "link": link,
    "supersede": supersede,
    "get": get,
    "search": search,
    "traverse": traverse,
    "briefing": briefing,
    "pending_list": pending_list,
    "stats": stats,
    "inbox_review": inbox_review,
    "scope_list": scope_list,
    "scope_guide": scope_guide,
    "scope_create": scope_create,
}

# The four space-admin tools (design/06 §Space administration, E2-plan.md §5.5).
# Kept OUT of CORE_DISPATCH so they can be conditionally present: they appear in
# tools/list and are dispatchable ONLY when this space's config leaves
# `space_admin_tools` unset or true. When it is false they are absent entirely
# (not registered-but-refusing) -- 03's "admin impossible over the network by
# absence of code path" applied to the config posture. The per-CALL space_admin
# role gate is separate and lives in admin.py (a non-space-admin sees the tools
# but gets ENGRAPHY_ROLE); this flag is the per-SPACE registration switch.
ADMIN_DISPATCH = {
    "admin_member_add": admin_member_add,
    "admin_token_create": admin_token_create,
    "admin_scope_visibility": admin_scope_visibility,
    "admin_grant": admin_grant,
}

# design/03 s.The tool surface -- the engine's own one-line text per tool.
_BASE_DESCRIPTIONS = {
    "briefing": "Pack-driven session-start sections: due commitments, relevant preferences and notes.",
    "search": "Hybrid + RRF search across one scope or 'all'.",
    "traverse": "Recursive graph walk from a starting node.",
    "get": "Full nodes plus edge summaries, up to 25 ids.",
    "pending_list": "List your pending duplicate-check writes awaiting confirmation (read-only).",
    "stats": "Usage metrics — totals + a zero-filled daily series, grouped by 'space' (all principals) or 'user' (you); read-only.",
    "write": (
        "Dedup-banded write; returns the written node or a duplicate-check verdict, plus a "
        "resonance report. If the result is 'merged' but your text contradicted or updated "
        "the stored fact rather than restating it, call supersede."
    ),
    "link": "Attach typed edges between existing nodes, rule-checked.",
    "update": "Update a node's title/body/attrs; re-embeds only if the text actually changed.",
    "supersede": "Atomically replace a node with a new one and flip the old one's status.",
    "resolve_duplicate": "Resolve a pending duplicate-check verdict as distinct or merge.",
    "scope_list": "List the scopes this token can read.",
    "scope_guide": (
        "The routing manifest: every scope you can write to, each with a description of what it "
        "governs and when to write there. Fetch this to decide where a new memory belongs. Read-only."
    ),
    "scope_create": "Create a new private scope (requires confirm: true and a non-empty description).",
    "inbox_review": "Review the capture inbox: list, promote, or discard pending items.",
    "admin_member_add": "Add a principal to this space (space_admin only).",
    "admin_token_create": "Mint a display-once bearer token for a principal (space_admin only).",
    "admin_scope_visibility": "Change a scope's visibility (space_admin only).",
    "admin_grant": "Grant a principal read/write on a scope (space_admin only).",
}

# MCP tool annotations (the optional `annotations` hints on a tools/list entry).
# Only tools that declare one appear here; the rest get `annotations: None`.
# scope_guide is the first to carry them (readOnlyHint + a human title); the
# mechanism is generic so the other read-only tools can adopt it later without
# more plumbing. Kept as plain dicts here (no mcp.types import in the registry);
# app.py constructs the typed ToolAnnotations from them.
_ANNOTATIONS = {
    "scope_guide": {"title": "Scope routing guide", "readOnlyHint": True},
}


def _input_schema(tool_name: str) -> dict:
    """The tool's published JSON Schema, generated from `wire_types.SPEC` --
    the same table `handle_call_tool` enforces against, so the advertised
    surface and the accepted one cannot drift.

    This replaces three hand-maintained dicts that used to live here
    (`_REQUIRED_ARGS`, `_ADMIN_TOOL_ARGS`, `_ADMIN_REQUIRED_ARGS`) plus a
    `{name: {}}` property body carrying no types at all. They were deleted
    rather than mirrored: a duplicate that agrees today is a duplicate that
    disagrees eventually, and the whole point of generating is that there is
    one place to change. `packs.CORE_TOOLS` stays where it is -- it is the
    alias-preset allow-list, a different surface with a different job.
    """
    return wire_types.input_schema(tool_name)


async def admin_tools_enabled(pool, space_id: str) -> bool:
    """Whether this space registers the four admin_* tools. True unless config
    `space_admin_tools` is explicitly false (design/06's paranoid-deployment
    switch: a paranoid deployment keeps the everything-via-CLI posture by setting it false).
    Absence of the key means enabled -- the cloud-team default."""
    async with pool.connection() as conn:
        cur = conn.cursor()
        await cur.execute(
            "SELECT value FROM config WHERE space_id = %s AND key = 'space_admin_tools'",
            (space_id,),
        )
        row = await cur.fetchone()
    # jsonb false -> Python False; tolerate a stringized "false" defensively.
    return not (row is not None and row[0] in (False, "false"))


async def _load_pack_fragment(pool, space_id: str, key: str) -> dict:
    """One of the config rows packs.apply() persists (pack.briefing /
    pack.tool_aliases / pack.tool_descriptions). Plain SELECT: config is not
    RLS-covered (same reasoning as auth.read_rate_limits)."""
    async with pool.connection() as conn:
        cur = conn.cursor()
        await cur.execute(
            "SELECT value FROM config WHERE space_id = %s AND key = %s", (space_id, key),
        )
        row = await cur.fetchone()
    return (row[0] if row else {}) or {}


async def load_aliases(pool, space_id: str) -> dict:
    """space_id's tool_aliases -> {alias_name: AliasBinding}."""
    fragment = await _load_pack_fragment(pool, space_id, "pack.tool_aliases")
    return aliases.build_aliases({"tool_aliases": fragment})


async def load_descriptions(pool, space_id: str) -> dict:
    """space_id's tool_descriptions override block (may be {})."""
    return await _load_pack_fragment(pool, space_id, "pack.tool_descriptions")


async def list_tools_for_space(pool, space_id: str) -> list[dict]:
    """This space's full tool list: {name, description, inputSchema} per
    entry -- the core tools (CORE_DISPATCH, plus ADMIN_DISPATCH when enabled)
    plus this space's pack aliases."""
    descriptions = await load_descriptions(pool, space_id)
    bindings = await load_aliases(pool, space_id)

    tool_names = list(CORE_DISPATCH)
    if await admin_tools_enabled(pool, space_id):
        tool_names += list(ADMIN_DISPATCH)

    entries = [
        {
            "name": name,
            "description": descriptions.get(name, _BASE_DESCRIPTIONS[name]),
            "inputSchema": _input_schema(name),
            "annotations": _ANNOTATIONS.get(name),
        }
        for name in tool_names
    ]
    for alias_name, binding in bindings.items():
        entries.append({
            "name": alias_name,
            "description": binding.description or _BASE_DESCRIPTIONS[binding.binds],
            "inputSchema": _input_schema(binding.binds),
            # An alias inherits the bound tool's annotations (a read-only tool
            # aliased stays read-only).
            "annotations": _ANNOTATIONS.get(binding.binds),
        })
    return entries


async def resolve_dispatch(pool, space_id: str, tool_name: str, arguments: dict):
    """One incoming tools/call -> (core_name, dispatcher, merged_arguments,
    action), or None if `tool_name` names neither a core tool nor a known
    alias for this space.

    `action` is the audit_log override the write tool dispatcher accepts
    (dedup.write()'s `action` param, "E2: close two app.py-adjacent gaps"
    commit) -- only meaningful when core_name == "write": that is the only
    core tool with an audit-identity seam (_locked_core's one always-fires
    audit_log row), and no shipped pack aliases anything else. None for a
    direct core-tool call or a non-write alias target."""
    if tool_name in CORE_DISPATCH:
        return tool_name, CORE_DISPATCH[tool_name], arguments, None
    if tool_name in ADMIN_DISPATCH:
        # Gated by the per-space config switch, not the caller's role: when
        # space_admin_tools=false the tool is unresolvable (None -> "unknown
        # tool", indistinguishable from a name that never existed). When
        # enabled, the space_admin role check happens inside the dispatcher.
        if await admin_tools_enabled(pool, space_id):
            return tool_name, ADMIN_DISPATCH[tool_name], arguments, None
        return None
    bindings = await load_aliases(pool, space_id)
    binding = bindings.get(tool_name)
    if binding is None:
        return None
    core_name, merged_args, audit_identity = aliases.resolve_alias_call(binding, arguments)
    action = audit_identity if core_name == "write" else None
    return core_name, CORE_DISPATCH[core_name], merged_args, action
