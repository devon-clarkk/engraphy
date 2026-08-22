"""Bearer auth: token -> (space, principal, client, role). Normative: design/03
s.Tokens, roles, and the isolation model; error codes in design/07.

The isolation model's first link: the bearer token IS the identity. No tool takes
a space or principal argument (cross-space access is inexpressible, not merely
forbidden). This module turns a raw bearer into an AuthContext the transport layer
then pins onto the RLS transaction (engraphy/server/db.py sets the GUCs from it).

What lives here (design/03 file reference "Token resolution, roles, rate limiter"):
* hash_token / resolve_token -- SHA-256 lookup in api_tokens (an instance-level
  table, queried BEFORE any space GUC is set, so a plain connection, not the RLS
  transaction() wrapper), revocation check with no cache window, last_used_at stamp.
* Unauthorized vs ToolError -- a missing/invalid/revoked token is a transport 401
  (Unauthorized), NOT an ENGRAPHY_ tool error; rate-limit and role failures ARE tool
  errors (ENGRAPHY_RATE_LIMITED / ENGRAPHY_ROLE, design/07).
* RateLimiter -- per-token sliding 60s window, 60 reads / 30 writes per minute
  (design/03), per-space config-overridable via rate.read_per_min / rate.write_per_min.
* Tool classification + require_write -- readonly tokens cannot call write tools.
* FailureTracker -- the auth-failure ban list: repeated bad bearers from one source
  are briefly banned so a stolen-or-guessed-token loop is slow and loud (design/03
  "a stolen token bulk-exfiltrates slowly and loudly").

Space-admin gating (admin_* tools read principals.role honoring config
space_admin_tools) needs an RLS-identified connection, so it is enforced in the
admin-tools module where that connection exists; the ADMIN_TOOLS set below names
the surface it applies to.
"""

import dataclasses
import hashlib
import secrets
import time
from collections import defaultdict, deque

# design/03 s.The tool surface -- which core tools mutate. inbox_review is split:
# action=list is a read, action in {promote, discard} is a write (classified per
# call by classify_kind, since one tool name spans both).
WRITE_TOOLS = frozenset({
    "write", "link", "update", "supersede", "resolve_duplicate", "scope_create",
    "admin_member_add", "admin_token_create", "admin_scope_visibility", "admin_grant",
})
ADMIN_TOOLS = frozenset({
    "admin_member_add", "admin_token_create", "admin_scope_visibility", "admin_grant",
})
_INBOX_WRITE_ACTIONS = frozenset({"promote", "discard"})

DEFAULT_READ_PER_MIN = 60
DEFAULT_WRITE_PER_MIN = 30
_WINDOW_SECONDS = 60.0

# FailureTracker defaults: after this many bad bearers from one source inside the
# window, ban the source for the window's length.
_FAIL_THRESHOLD = 10
_FAIL_WINDOW_SECONDS = 60.0


class Unauthorized(Exception):
    """Transport-level 401: no bearer, unknown hash, or revoked token. Deliberately
    carries no ENGRAPHY_ code -- it is not a tool error, and its message never
    distinguishes 'unknown' from 'revoked' (no oracle for token guessing)."""


class ToolError(Exception):
    """An MCP tool error whose text is exactly `ENGRAPHY_<CODE>: <sentence>`
    (design/07 s.Error codes). `extra` carries structured fields the wire contract
    attaches (e.g. RATE_LIMITED's retry_after_ms)."""

    def __init__(self, code: str, message: str, **extra):
        self.code = code
        self.message = message
        self.extra = extra
        super().__init__(f"ENGRAPHY_{code}: {message}")


@dataclasses.dataclass(frozen=True)
class AuthContext:
    """The resolved identity. `client_name` becomes source_client provenance;
    `principal` becomes author_principal and drives every visibility filter."""

    token_id: str
    space_id: str
    principal: str
    client_name: str
    role: str  # 'readwrite' | 'readonly'
    # The per-token scope restriction (api_tokens.no_scope_all, migration 0023):
    # True means this CREDENTIAL may not ask for scope='all' on any tool that
    # takes a scope, and the tool refuses with ENGRAPHY_SCOPE rather than resolving
    # the call to the principal's readable set and auditing it. It is a second
    # per-token capability alongside `role`, and it is the first field here the
    # tool layer consults about the bearer rather than about the identity: role
    # says what the token may DO, this says how broadly it may ASK.
    #
    # Defaulted so that every hand-built AuthContext (tests, fixtures) keeps the
    # unrestricted behaviour it had before the field existed -- the restriction
    # is only ever acquired deliberately, at mint time.
    no_scope_all: bool = False


def hash_token(raw_token: str) -> str:
    """SHA-256 hex of the raw bearer. Tokens are 256-bit random, stored only as
    this hash (design/03: hashes at rest, display-once minting)."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_raw_token() -> str:
    """A fresh 256-bit URL-safe bearer, from `secrets` (CSPRNG, never `random`).
    32 bytes = 256 bits of entropy (design/03: "Tokens are 256-bit random")."""
    return secrets.token_urlsafe(32)


async def mint_token(conn, space_id: str, principal: str, client_name: str, role: str,
                     no_scope_all: bool = False) -> tuple[str, dict]:
    """Display-once mint: the ONE place a plaintext bearer comes into existence
    and gets persisted. Generates the raw token, stores only its SHA-256 in
    `api_tokens.token_hash`, and returns `(raw_plaintext, metadata)`. The raw
    value is the return's first element ONLY -- it is never written to any log,
    the audit trail, or a second column, and there is no retrieve-later path (a
    lost token is revoked + re-minted). Both `admin_token_create` and the admin
    CLI's `token` verb call this, so the never-store-plaintext invariant lives
    in exactly one function (design/03 §Tokens; design/06 §Space administration;
    E2-plan.md §5.5).

    `conn` may be a plain OR an RLS-identified connection -- `api_tokens` is
    instance-level (not RLS-covered), so the INSERT does not depend on any space
    GUC. Callers are responsible for wrapping this in whatever transaction and
    audit-logging their surface requires.

    `no_scope_all` marks the token as unable to use scope='all' (migration 0023;
    enforced in core.scope_set, consulted via AuthContext.no_scope_all). It is a
    MINT-TIME property and there is no path that flips it on an existing token:
    narrowing a live credential in place would silently change what an already-
    running client can do, and widening one in place would be a privilege grant
    with no mint record. Re-mint instead, exactly as for `role`. It defaults
    False so every existing caller mints the unrestricted token it always did."""
    raw = generate_raw_token()
    cur = conn.cursor()
    await cur.execute(
        "INSERT INTO api_tokens (space_id, principal, client_name, token_hash, role, no_scope_all) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING created_at",
        (space_id, principal, client_name, hash_token(raw), role, no_scope_all),
    )
    (created_at,) = await cur.fetchone()
    metadata = {
        "principal": principal,
        "client_name": client_name,
        "role": role,
        "no_scope_all": no_scope_all,
        "created_at": created_at.isoformat(),
    }
    return raw, metadata


async def resolve_token(conn, raw_token: str) -> AuthContext:
    """Resolve a raw bearer to an AuthContext, or raise Unauthorized. `conn` is a
    plain async connection (api_tokens is instance-level, not RLS-covered, and this
    runs before any space GUC is set). Stamps last_used_at on success -- instant
    revocation means the row is re-read every request, no cache window."""
    if not raw_token:
        raise Unauthorized("missing bearer token")
    token_hash = hash_token(raw_token)
    cur = conn.cursor()
    await cur.execute(
        "SELECT id, space_id, principal, client_name, role, revoked, no_scope_all "
        "FROM api_tokens WHERE token_hash = %s",
        (token_hash,),
    )
    row = await cur.fetchone()
    if row is None:
        raise Unauthorized("unknown or invalid token")
    token_id, space_id, principal, client_name, role, revoked, no_scope_all = row
    if revoked:
        raise Unauthorized("token revoked")
    await cur.execute("UPDATE api_tokens SET last_used_at = now() WHERE id = %s", (token_id,))
    return AuthContext(
        token_id=str(token_id), space_id=space_id, principal=principal,
        client_name=client_name, role=role, no_scope_all=no_scope_all,
    )


def classify_kind(tool_name: str, arguments: dict | None = None) -> str:
    """'write' or 'read' -- the rate-limit bucket and role gate. inbox_review's
    kind depends on its action argument (list = read; promote/discard = write)."""
    if tool_name == "inbox_review":
        action = (arguments or {}).get("action")
        return "write" if action in _INBOX_WRITE_ACTIONS else "read"
    return "write" if tool_name in WRITE_TOOLS else "read"


def require_write(ctx: AuthContext, tool_name: str, arguments: dict | None = None) -> None:
    """Raise ENGRAPHY_ROLE if a readonly token calls a write tool (design/07)."""
    if classify_kind(tool_name, arguments) == "write" and ctx.role != "readwrite":
        raise ToolError("ROLE", f"this token is readonly and cannot call {tool_name}")


async def read_rate_limits(conn, space_id: str) -> tuple[int, int]:
    """(reads_per_min, writes_per_min) for a space -- config overrides the 60/30
    defaults (design/03 'config-table overridable per space'). config is not
    RLS-covered; a plain SELECT by space_id is correct here."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT key, value FROM config WHERE space_id = %s "
        "AND key IN ('rate.read_per_min', 'rate.write_per_min')",
        (space_id,),
    )
    limits = {"rate.read_per_min": DEFAULT_READ_PER_MIN, "rate.write_per_min": DEFAULT_WRITE_PER_MIN}
    for key, value in await cur.fetchall():
        try:
            limits[key] = int(value)
        except (TypeError, ValueError):
            pass  # a malformed config value falls back to the default, never 0/unbounded
    return limits["rate.read_per_min"], limits["rate.write_per_min"]


class RateLimiter:
    """In-process per-token sliding window (design/03: 'now per-token'). One process
    serves the whole instance, so in-memory state is authoritative; a restart resets
    windows, which only ever forgives, never over-limits. Separate read/write buckets
    so a burst of reads can't starve a write and vice versa."""

    def __init__(self, now=time.monotonic):
        self._now = now
        self._events: dict[tuple[str, str], deque] = defaultdict(deque)

    def check(self, token_id: str, kind: str, read_limit: int = DEFAULT_READ_PER_MIN,
              write_limit: int = DEFAULT_WRITE_PER_MIN) -> None:
        """Record one call of `kind` ('read'|'write'); raise ENGRAPHY_RATE_LIMITED
        with retry_after_ms if it would exceed the window's limit."""
        limit = write_limit if kind == "write" else read_limit
        now = self._now()
        bucket = self._events[(token_id, kind)]
        cutoff = now - _WINDOW_SECONDS
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after_ms = int((bucket[0] + _WINDOW_SECONDS - now) * 1000) + 1
            raise ToolError(
                "RATE_LIMITED",
                f"{kind} window exceeded ({limit}/min), retry after {retry_after_ms}ms",
                retry_after_ms=retry_after_ms,
            )
        bucket.append(now)


class FailureTracker:
    """The auth-failure ban list. Repeated Unauthorized from one source (the
    transport passes a key, e.g. remote address) within the window trips a ban for
    the window's length -- turning token guessing into a slow, loud loop."""

    def __init__(self, threshold: int = _FAIL_THRESHOLD,
                 window_seconds: float = _FAIL_WINDOW_SECONDS, now=time.monotonic):
        self._threshold = threshold
        self._window = window_seconds
        self._now = now
        self._fails: dict[str, deque] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> None:
        bucket = self._fails[key]
        cutoff = now - self._window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def is_banned(self, key: str) -> bool:
        now = self._now()
        self._prune(key, now)
        return len(self._fails[key]) >= self._threshold

    def record_failure(self, key: str) -> None:
        now = self._now()
        self._prune(key, now)
        self._fails[key].append(now)
