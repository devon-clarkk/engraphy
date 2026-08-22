"""List the caller's pending (needs-confirmation) duplicate writes.

The read side of the write/dedup PENDING band: when a write lands close enough
to an existing node to need a human call, dedup.py parks the incoming payload +
candidate snapshot in `pending_writes` with a 24h TTL and returns a
`pending_id`, and `resolve_duplicate` later consumes it (distinct | merge). This
function is the missing LIST between those two -- it lets a client (the VS Code
extension's confirm-queue) enumerate the rows waiting to be resolved.

Read-only and side-effect-free: a single SELECT, no writes, no recall-stat
bumps (it reads `pending_writes`, never `nodes`). RLS scopes it to the caller:
`pending_writes` is FORCE ROW LEVEL SECURITY with a `pending_writes_read` policy
of `space_id = current principal's space AND author_principal = current
principal` (migration 0008) -- "a parked write is visible only to its author (it
may quote private content)". The run goes through the non-superuser app-role
pool inside `transaction()`, which sets those GUCs, so the policy is live; the
explicit space+author WHERE below is belt-and-suspenders matching get.py's idiom
(`WHERE id = %s AND space_id = %s`), never the sole guard.
"""
from engraphy.server.db import transaction

_DEFAULT_LIMIT = 25
_MAX_LIMIT = 100
_BODY_PREVIEW_CHARS = 280


def _clamp_limit(limit: int) -> int:
    """Silent clamp to 1.._MAX_LIMIT, the same boring over-limit handling
    search()/get() use -- a well-typed out-of-range integer clamps, never
    refuses (wire_types keeps range out of validation on purpose)."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(limit, _MAX_LIMIT))


def _clamp_offset(offset: int) -> int:
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        return 0
    return max(0, offset)


def _payload_preview(payload: dict) -> str:
    """A single-string title/body snippet of the incoming write -- what a
    confirm-queue row shows at a glance. Deliberately a STRING, not an object: a
    field literally named `_preview` is safest to render directly, and this
    keeps the incoming title and a capped body in one line ("title — body…").
    The body is capped so the list stays light even when a parked body is long;
    full text is available via the row's resolve_duplicate path. Both parts are
    read defensively (.get) so an older/newer parked payload degrades to a
    shorter preview rather than raising."""
    title = payload.get("title")
    body = payload.get("body")
    title = title.strip() if isinstance(title, str) else ""
    body = body.strip() if isinstance(body, str) else ""
    if len(body) > _BODY_PREVIEW_CHARS:
        body = body[:_BODY_PREVIEW_CHARS].rstrip() + "…"
    if title and body:
        return f"{title} — {body}"
    return title or body


def _to_item(pending_id, payload: dict, expires_at, created_at) -> dict:
    """Shape one pending_writes row for the confirm-queue wire item. `payload`
    is the jsonb dedup.py parked (see _do_pending): title/body of the incoming
    write, and a nearest-first `candidates` snapshot of {id, title, body,
    similarity}. `.get()` throughout so a payload written by an older/newer
    write path can never make the list raise -- a preview degrades rather than
    500ing the whole queue."""
    payload = payload or {}
    candidates = []
    for cand in payload.get("candidates", []) or []:
        candidates.append({
            "id": cand.get("id"),
            "title": cand.get("title"),
            # similarity is why the row is pending at all -- keep it so the
            # queue can rank/annotate; a client that only wants id+title ignores it.
            "similarity": cand.get("similarity"),
        })
    return {
        "id": str(pending_id),
        "payload_preview": _payload_preview(payload),
        "candidates": candidates,
        "expires_at": expires_at.isoformat() if expires_at is not None else None,
        "created_at": created_at.isoformat() if created_at is not None else None,
    }


async def pending_list(pool, space_id: str, principal: str,
                       limit: int = _DEFAULT_LIMIT, offset: int = 0) -> dict:
    """The caller's own pending duplicate-check writes, newest first.

    Returns the 07-style envelope `{v, pending}` where `pending` is a list of
    `{id, payload_preview (str), candidates:[{id, title, similarity}],
    expires_at, created_at}`, newest first. `expires_at` is surfaced (never
    filtered) so a client can show staleness -- expired rows currently age out
    with no sweeper, and resolve_duplicate rejects an expired row at consume
    time, so surfacing them is a display concern, not a correctness one (see the
    branch report's follow-up note). Empty list when the caller has none."""
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)

    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()
        await cur.execute(
            "SELECT id, payload, expires_at, created_at FROM pending_writes "
            "WHERE space_id = %s AND author_principal = %s "
            "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            (space_id, principal, limit, offset),
        )
        rows = await cur.fetchall()

    pending = [_to_item(pid, payload, expires_at, created_at)
               for pid, payload, expires_at, created_at in rows]
    return {"v": 1, "pending": pending}
