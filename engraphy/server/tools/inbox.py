"""inbox_review(list|promote|discard). Promotion runs the FULL dedup pipeline.

QUESTIONS.md "inbox-promote-envelope" (resolved 2026-07-18): one tool, three
actions, each dispatching to its own core.inbox function. No wire fixture
pins this tool's argument shape (E2-plan.md s.3), so the per-action argument
names below mirror `write`'s own wire names as the boring, defensible
default -- `promote` literally IS authoring (core.inbox.promote's own
docstring: "the reviewer supplies the write fields").

`POST /inbox` on app.py is a SEPARATE capture endpoint, not this MCP tool --
core.inbox.capture is never dispatched from here.
"""
from engraphy.core.dedup import embed_with_attr_surface
from engraphy.core.inbox import discard as core_discard
from engraphy.core.inbox import list_pending as core_list_pending
from engraphy.core.inbox import promote as core_promote
from engraphy.server.auth import ToolError
from engraphy.server.tools.errors import to_tool_error

_ACTIONS = ("list", "promote", "discard")


async def inbox_review(pool, ctx, arguments: dict) -> dict:
    """CORE_TOOLS['inbox_review']: {action, ...}. `action` selects the
    per-action argument set:
    - list: {limit?, offset?}
    - promote: {id, type, scope, title, body, attrs?, links?, session_id?} --
      `id` names the inbox item; the rest are the reviewer's authored write
      fields, same wire names as `write`.
    - discard: {id}
    """
    action = arguments.get("action")
    if action not in _ACTIONS:
        raise ToolError("VALIDATION", f"action must be one of {'|'.join(_ACTIONS)}")

    try:
        if action == "list":
            return await core_list_pending(
                pool, ctx.space_id, ctx.principal,
                limit=arguments.get("limit", 25), offset=arguments.get("offset", 0),
            )
        if action == "discard":
            return await core_discard(pool, ctx.space_id, ctx.principal, arguments["id"])

        # promote: the reviewer's authored write fields, embedded outside the
        # transaction (trap 3) exactly as the write tool does.
        title = arguments["title"]
        body = arguments["body"]
        attrs = arguments.get("attrs") or {}
        # Phase C: render the attr surface + embed searchable_text outside the txn.
        embedding_vector, extra = await embed_with_attr_surface(
            pool, ctx.space_id, arguments["type"], title, body, attrs)
        return await core_promote(
            pool, ctx.space_id, ctx.principal, arguments["id"], arguments["type"], arguments["scope"],
            title, body, attrs, ctx.client_name,
            links=arguments.get("links"), embedding_vector=embedding_vector,
            source_session=arguments.get("session_id"), extra_search=extra,
        )
    except Exception as exc:
        raise to_tool_error(exc) from exc
