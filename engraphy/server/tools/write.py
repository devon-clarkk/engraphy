"""write / link / update / supersede / resolve_duplicate — all through core.dedup's
pipeline. See design/implementation/dedup-write-path-plan.md for the ONLY allowed shape.

Each function here is a thin dispatcher: (pool, ctx, arguments) -> the 07 wire
envelope. Auth (require_write), rate-limiting, and alias resolution are the
transport layer's job (app.py, E2-plan.md s.6) -- uniform across all twelve
tools, so they live in exactly one place, not copy-pasted per tool. What
belongs here: wire-name -> core-fn-argument mapping, the pre-transaction embed
step (trap 3: embed() must never run inside write()'s transaction), and
exception -> ToolError translation via tools/errors.py.
"""
from engraphy.core.dedup import embed_with_attr_surface
from engraphy.core.dedup import resolve_duplicate as dedup_resolve_duplicate
from engraphy.core.dedup import supersede as dedup_supersede
from engraphy.core.dedup import write as dedup_write
from engraphy.core.link import link as core_link
from engraphy.core.update import update as core_update
from engraphy.server.tools.errors import to_tool_error


async def write(pool, ctx, arguments: dict, action: str = "write") -> dict:
    """CORE_TOOLS['write']: {scope, type, title, body, attrs?, links?,
    session_id?}. `scope`/`type`/`session_id` are the wire names for
    dedup.write()'s `scope_id`/`node_type`/`source_session` (design/03's
    scope -> scope_id boundary rename, mirrored for session_id).

    action: passed straight through to dedup.write()'s audit_log override.
    A direct `write` call never sets it (plain "write"); app.py sets it to
    `aliases.resolve_alias_call`'s audit identity ("write via log_error")
    when this dispatch came from a pack alias."""
    node_type = arguments["type"]
    scope_id = arguments["scope"]
    title = arguments["title"]
    body = arguments["body"]
    attrs = arguments.get("attrs") or {}
    links = arguments.get("links")
    source_session = arguments.get("session_id")

    # Phase C: render the searchable attr surface + embed searchable_text, OUTSIDE
    # the transaction (trap 3). extra_search is stored on whatever node this write
    # creates (insert / merge-link member).
    embedding_vector, extra = await embed_with_attr_surface(
        pool, ctx.space_id, node_type, title, body, attrs)
    try:
        return await dedup_write(
            pool, ctx.space_id, ctx.principal, node_type, scope_id, title, body,
            attrs, embedding_vector, ctx.client_name,
            links=links, source_session=source_session, action=action,
            extra_search=extra,
        )
    except Exception as exc:
        raise to_tool_error(exc) from exc


async def resolve_duplicate(pool, ctx, arguments: dict) -> dict:
    """CORE_TOOLS['resolve_duplicate']: {pending_id, resolution, merge_into?}.
    Direct passthrough -- no wire-name rename, no pre-transaction embed step
    (the pending write's embedding was already computed and parked at write()
    time). 07: "returns the write envelope of the final outcome" (inserted
    with relates_edge_added, or merged); the core fn's own ValueError for a
    bad `resolution` or a missing `merge_into` maps to ENGRAPHY_VALIDATION via
    errors.py's ValueError branch."""
    try:
        return await dedup_resolve_duplicate(
            pool, ctx.space_id, ctx.principal, arguments["pending_id"], arguments["resolution"],
            merge_into=arguments.get("merge_into"),
        )
    except Exception as exc:
        raise to_tool_error(exc) from exc


async def update(pool, ctx, arguments: dict) -> dict:
    """CORE_TOOLS['update']: {id, title?, body?, attrs?}. No pre-transaction
    embed step here -- core.update.update() decides internally whether
    re-embedding is even needed (title/body unsupplied, or a byte-identical
    repeat, skip it entirely) and does its own embed-outside-the-transaction
    dance only when it is (E2-plan.md s.4)."""
    try:
        return await core_update(
            pool, ctx.space_id, ctx.principal, arguments["id"],
            title=arguments.get("title"), body=arguments.get("body"), attrs=arguments.get("attrs"),
        )
    except Exception as exc:
        raise to_tool_error(exc) from exc


async def link(pool, ctx, arguments: dict) -> dict:
    """CORE_TOOLS['link']: {edges}. `edges` is the wire name for a list of
    {type, src_id, dst_id} items, both endpoints always required (unlike
    write's links)."""
    try:
        return await core_link(pool, ctx.space_id, ctx.principal, arguments["edges"])
    except Exception as exc:
        raise to_tool_error(exc) from exc


async def supersede(pool, ctx, arguments: dict) -> dict:
    """CORE_TOOLS['supersede']: {old_id, scope, type, title, body, attrs?,
    links?, session_id?} -- same wire-name mapping as write, plus old_id
    passthrough. Returns the inserted node's write envelope plus
    "superseded": <old_id> (07 gives supersede no canonical shape of its
    own). dedup.supersede()'s SupersedeUnresolvedBandError (a fail-closed
    guard over an unspecified band collision, deliberately not dressed up as
    an ENGRAPHY_ code itself) is what errors.py names ENGRAPHY_SUPERSEDE_CONFLICT
    at this layer (E2-plan.md s.3's supersede row)."""
    old_id = arguments["old_id"]
    node_type = arguments["type"]
    scope_id = arguments["scope"]
    title = arguments["title"]
    body = arguments["body"]
    attrs = arguments.get("attrs") or {}
    links = arguments.get("links")
    source_session = arguments.get("session_id")

    embedding_vector, extra = await embed_with_attr_surface(
        pool, ctx.space_id, node_type, title, body, attrs)  # OUTSIDE the transaction (trap 3)
    try:
        return await dedup_supersede(
            pool, ctx.space_id, ctx.principal, old_id, node_type, scope_id, title, body,
            attrs, embedding_vector, ctx.client_name, source_session=source_session,
            links=links, extra_search=extra,
        )
    except Exception as exc:
        raise to_tool_error(exc) from exc
