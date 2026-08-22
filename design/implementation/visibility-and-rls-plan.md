# Implementation Plan — Visibility Functions and Row-Level Security

The privacy kernel: the readable/writable-scope functions, the session GUC protocol, and the RLS policies that back-stop them. This is the component where a subtle mistake leaks a teammate's private memory — so the design forces one authority (a pair of SQL functions) that the application, the RLS policies, and the tests all share, and this plan spells out the two classic Postgres traps (policy recursion, owner bypass) before anyone hits them.

**Normative inputs:** [06 §Visibility model / §Edge and traversal visibility](../06-teams-and-sharing.md), [01 §Row-level security](../01-core-data-model.md#row-level-security), [03 §Tokens](../03-api-auth-and-tenancy.md)
**Fixture:** `engraphy/tests/fixtures/visibility_matrix.py` (generator committed; outputs exhaustive)

---

## The single-authority rule

All access decisions reduce to two set-valued functions. **No other code may re-derive visibility logic** — not a Python helper, not an inline SQL predicate. Application queries filter with them; RLS policies call them; the test matrix validates them once.

```sql
-- Which scopes can the current session's principal READ?
CREATE FUNCTION engram_readable_scopes() RETURNS SETOF text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = engraphy AS $$
  SELECT s.id FROM scopes s
  WHERE s.space_id = current_setting('engram.space_id', true)
    AND s.archived = false
    AND ( s.visibility IN ('team-read', 'team-write')
          OR s.owner_principal = current_setting('engram.principal', true)
          OR EXISTS (SELECT 1 FROM scope_grants g
                     WHERE g.space_id = s.space_id AND g.scope_id = s.id
                       AND g.principal = current_setting('engram.principal', true)) )
$$;

-- Which scopes can the principal WRITE?  (write grant or team-write or owner)
CREATE FUNCTION engram_writable_scopes() RETURNS SETOF text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = engraphy AS $$
  SELECT s.id FROM scopes s
  WHERE s.space_id = current_setting('engram.space_id', true)
    AND s.archived = false
    AND ( s.visibility = 'team-write'
          OR s.owner_principal = current_setting('engram.principal', true)
          OR EXISTS (SELECT 1 FROM scope_grants g
                     WHERE g.space_id = s.space_id AND g.scope_id = s.id
                       AND g.principal = current_setting('engram.principal', true)
                       AND g.level = 'write') )
$$;
```

Design notes an implementer must not "improve":

- **`SECURITY DEFINER` is deliberate** — it is what breaks the RLS recursion loop: policies on `nodes`/`edges` call these functions; if the functions were subject to the caller's RLS on `scopes`, evaluating a policy would evaluate a policy. Definer runs as the schema owner, sees `scopes` plainly, and the functions expose only scope *ids* the session may act on — no content. The definer functions are the **only** SECURITY DEFINER objects in the schema; adding another requires a design-doc change.
- **`current_setting(..., true)` returns NULL when unset → empty result set → deny-all.** Missing GUC = no access, never full access. There is deliberately no default value.
- **`STABLE`, not IMMUTABLE** (reads tables) — Postgres evaluates once per statement, which is the caching we want. Do not memoize in Python across statements: a visibility change must take effect on the next statement.

## Session GUC protocol

One place sets identity — the transaction wrapper in `engraphy/server/db.py`:

```python
async with pool.connection() as conn:
    async with conn.transaction():
        await conn.execute("SELECT set_config('engram.space_id', %s, true), "
                           "set_config('engram.principal', %s, true)",
                           (token.space_id, token.principal))   # true = LOCAL (txn-scoped)
        ...tool body...
```

Rules: `set_config(..., true)` (transaction-local — leaks nothing to the pooled connection after commit/rollback); every tool body runs inside exactly one such transaction; the wrapper is the **only** code that touches these GUCs (grep-enforced in CI: `set_config.*engraphy\.` appears once outside tests); admin CLI paths connect as a separate role with its own explicit settings.

## RLS policies (exact shapes)

```sql
ALTER TABLE nodes  ENABLE ROW LEVEL SECURITY;
ALTER TABLE nodes  FORCE  ROW LEVEL SECURITY;   -- FORCE: even the table owner obeys
-- (same pair for edges, inbox, pending_writes, dedup_log, scopes, scope_grants, principals)

CREATE POLICY nodes_read ON nodes FOR SELECT
  USING (space_id = current_setting('engram.space_id', true)
         AND scope_id IN (SELECT engram_readable_scopes()));

CREATE POLICY nodes_write ON nodes FOR INSERT
  WITH CHECK (space_id = current_setting('engram.space_id', true)
              AND scope_id IN (SELECT engram_writable_scopes()));
-- UPDATE gets BOTH: USING (readable — you must see it) AND WITH CHECK (writable target)

CREATE POLICY edges_read ON edges FOR SELECT
  USING (space_id = current_setting('engram.space_id', true)
         AND EXISTS (SELECT 1 FROM nodes n WHERE n.id = edges.src_id)     -- n is RLS-filtered
         AND EXISTS (SELECT 1 FROM nodes n WHERE n.id = edges.dst_id));   -- ⇒ both-endpoint rule
```

The `edges_read` trick is load-bearing: the inner `nodes` subqueries are themselves RLS-filtered for the session, so "both endpoints readable" falls out of composition rather than being re-implemented — but note it therefore costs two index probes per edge row; see the performance check below. Support tables: `scopes` policy = space-pinned only (scope *rows* are listable within the space — members can see that a private scope exists **as a name** via `scope_list`? **No** — [06](../06-teams-and-sharing.md) says existence is information: `scopes` SELECT policy is `space match AND id IN readable`; `scope_grants`/`principals` are space-pinned, and `principals` is readable by all members — a team roster is not secret within its space).

`pending_writes` policy: space + `author_principal = current principal` — a parked write is visible **only to its author** (it may quote private content).

## Traps

1. **Forgetting `FORCE ROW LEVEL SECURITY`** — the table owner (often the app's migration role) silently bypasses RLS. Every data table gets ENABLE + FORCE; the test suite asserts `relforcerowsecurity` for all of them.
2. **Policy recursion** — solved by SECURITY DEFINER above; the regression test drops the definer property and asserts the suite *fails* (guards against a well-meaning "security hardening" revert).
3. **INSERT policies need `WITH CHECK`, not `USING`** — a USING-only policy allows arbitrary inserts.
4. **Merge-chain leak** — resolving `canonical_id` chains must re-check readability of the *target* (chase in SQL through RLS-filtered `nodes`, never in Python from cached rows).
5. **`not-found` semantics live in the app layer** — RLS silently filters; tools translate empty results for named ids into `ENGRAPHY_NOT_FOUND` uniformly, so a permission miss and a true miss are indistinguishable ([07 §Error codes](../07-implementation-contracts.md)).
6. **Connection pooling** — transaction-local `set_config` only; a session-level `SET` on a pooled connection is a cross-user identity leak. Test: two interleaved simulated principals on one pool, 500 iterations, zero bleed.
7. **Sequences/aggregates leak counts** — no tool exposes raw counts across scopes; `edge_count` in search results is computed through RLS-filtered edges.

## Performance check (part of this component's DoD)

`EXPLAIN ANALYZE` on the six canonical queries (search both legs, briefing section, traverse step, get, edge hydration) against the 10k-node bench space with RLS active: every plan uses the `(space_id, scope_id, type, status)` index; the readable-set subquery appears as an InitPlan/hashed subplan (once per statement), not a per-row re-execution; RLS overhead vs a superuser baseline ≤ 25% per query. If any query exceeds it, the sanctioned fix is materializing `engram_readable_scopes()` into a txn-local temp set once per transaction — **not** weakening a policy.

## Test plan

| Test | Assert |
|------|--------|
| `visibility_matrix` (generated) | Every (visibility, grant, role, relation-to-owner) × (select, insert, update, edge-create, edge-read, traverse-through) — expected allow/deny, exhaustively |
| Parity | App-layer helper `Readable(token)` (a thin SELECT of the SQL function) equals direct function output under fuzz — no second implementation exists to diverge, this asserts the plumbing |
| RLS probe | Filter-free `SELECT * FROM nodes` under each fixture principal returns exactly the readable rows |
| FORCE audit | All data tables have `relrowsecurity AND relforcerowsecurity` |
| Pool-bleed | Trap 6's interleaving test |
| Existence hiding | `get` on a teammate-private id ≡ `get` on a random UUID (same code, same shape, comparable timing) |
| Definer-guard | Trap 2's regression |

## Build order

1. Migration: functions → ENABLE/FORCE → policies (one migration file, reviewed as a unit).
2. `db.py` transaction wrapper + CI grep rule.
3. Visibility matrix generator + expected outcomes (this is spec work — review carefully before implementing against it).
4. Matrix green under raw SQL (no server yet — psql-level truth first).
5. Tool-layer not-found translation; existence-hiding tests.
6. EXPLAIN performance check against the bench space.
