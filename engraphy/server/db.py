"""Connection pool + THE transaction wrapper.

The wrapper below is the ONLY code that sets the two identity settings this
product's row-level security depends on, via a LOCAL config call ==
transaction-local. CI greps that the config-setting call appears nowhere
else outside tests. See design/implementation/visibility-and-rls-plan.md
§Session GUC protocol.
"""

import contextlib

from psycopg_pool import AsyncConnectionPool

_pool: AsyncConnectionPool | None = None


def get_pool(conninfo: str) -> AsyncConnectionPool:
    """Process-wide pool, lazily created. Callers open() it once at startup."""
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(conninfo, open=False)
    return _pool


@contextlib.asynccontextmanager
async def transaction(pool: AsyncConnectionPool, space_id: str, principal: str):
    """Every tool body runs inside exactly one of these. The third argument
    to Postgres's config-setting call below is `true`, meaning LOCAL
    (transaction-local) -- it leaks nothing to the pooled connection after
    commit/rollback, which is what makes reusing connections across
    different (space, principal) sessions safe (visibility-and-rls-plan.md
    §Session GUC protocol, trap 6: a session-level SET on a pooled connection
    would be a cross-user identity leak).
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            # Pinned, not left to the server default: the dedup write path's
            # advisory-lock serialization argument (design/implementation/
            # dedup-write-path-plan.md) depends on READ COMMITTED -- a writer
            # that acquires the lock second must see the first writer's
            # already-committed row in its candidate query. Under REPEATABLE
            # READ or SERIALIZABLE the snapshot would freeze before the lock
            # is even acquired, silently defeating the lock's whole purpose.
            # Must be the transaction's first statement (Postgres requirement).
            await conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            await conn.execute(
                "SELECT set_config('engram.space_id', %s, true), set_config('engram.principal', %s, true)",
                (space_id, principal),
            )
            yield conn
