"""The one place a caller-supplied `scope` argument becomes a scope SET, and the
one place a per-token scope restriction is enforced.

Every tool that accepts `scope` (today: search, briefing) resolves it the same
way -- `'all'` means "every scope this identity can read", anything else means
"that scope plus the readable ambient scopes, intersected with the readable
set". That resolution used to be copy-pasted into each tool, which is exactly
the shape a security guard gets forgotten in: a third tool that accepts `scope`
would have inherited the copy and not the check. It lives here now, so the
guard comes free with the resolution.

**The guard.** `api_tokens.no_scope_all` (migration 0023) marks a credential
that may not ask the broad question at all. The restriction is a property of the
TOKEN, not of the principal: `scope_grants` already bounds what `scope='all'`
RETURNS for a principal, but it never denies the call, and a later grant for an
unrelated reason widens the bearer's reach with no diff to review. A restricted
token is REFUSED (ENGRAPHY_SCOPE) rather than resolved-and-audited, so the attempt
is a failure in the caller's face rather than one more routine `scope='all'` row
in `audit_log`. This is the reading the token-scope-restriction design settled on.

A refusal writes NO audit row: it raises inside the caller's transaction, which
rolls back, and Engraphy's other credential-level refusal (`require_write` ->
ENGRAPHY_ROLE) records nothing either. The `scope='all'` audit row remains what it
always was -- the exfiltration tripwire for calls that are ALLOWED to happen.
"""
from engraphy.core.visibility import ambient_scope_set_async, readable_scopes_async


class ScopeForbiddenError(Exception):
    """ENGRAPHY_SCOPE -- this TOKEN may not use this scope argument. Distinct from
    ScopeUnknownError (ENGRAPHY_SCOPE_UNKNOWN), which is not-found-shaped and says
    nothing about the bearer: a scope that does not exist, or that the PRINCIPAL
    cannot reach, collapses into not-found because existence is information. A
    restricted token asking for `scope='all'` is the opposite case -- the scope
    set is perfectly real and the principal could read it; the credential is what
    is being refused, so saying so leaks nothing the holder does not already know.
    tools/errors.py maps this to the ENGRAPHY_SCOPE wire error."""


def refuse_scope_all(scope: str, no_scope_all: bool, tool: str) -> None:
    """Raise ScopeForbiddenError if a restricted token asked for `scope='all'`.

    Pure: no cursor, no database, no transaction -- so it is unit-testable
    without a live Postgres, and so it stays cheap enough to call on every
    resolution rather than only on the paths somebody remembered."""
    if scope == "all" and no_scope_all:
        raise ScopeForbiddenError(
            f"ENGRAPHY_SCOPE: this token may not use scope='all' "
            f"(name the scope you want on {tool})"
        )


async def resolve_scope_set(cur, scope: str, no_scope_all: bool, tool: str) -> set[str]:
    """The caller's `scope` argument as a set of scope ids, guard applied first.

    `'all'` -> every readable scope. Anything else -> that scope unioned with the
    readable ambient scopes (design/06's query-time scope-set expansion), then
    intersected with the readable set, so a requested-but-unreadable scope
    contributes nothing and never leaks via `scopes_searched`.

    `cur` must already be inside `server.db.transaction()` (the GUC protocol is
    the caller's, per visibility.py). The guard runs BEFORE either SELECT: a
    refused call does no visibility work."""
    refuse_scope_all(scope, no_scope_all, tool)
    readable = await readable_scopes_async(cur)
    if scope == "all":
        return set(readable)
    return (await ambient_scope_set_async(cur, {scope})) & set(readable)
