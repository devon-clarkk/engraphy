# Engraphy — Teams, Principals, and Shared Memory

How multiple people share one memory space without sharing everything: principals, scope-level visibility, cross-member queries ("the designer checks the coder's memory"), shared project contexts, and the access rules that keep private things private inside a shared graph.

**Status:** Living document — owns the tenancy-v2 model; docs 01–05 were revised in place to match (design-phase iteration, July 2026)
**Last updated:** July 2026
**Scope:** Principals, visibility levels, grants, personal scopes, edge/traversal/dedup visibility rules, space administration, team-scale bounds
**Out of scope:** Cross-**space** anything (still impossible by design — see below), cloud deployment mechanics ([04](04-operations-and-governance.md)), DDL details ([01](01-core-data-model.md) carries the revised tables)

---

## Table of contents

1. [Goals](#goals)
2. [The key reframing: spaces are trust boundaries](#the-key-reframing-spaces-are-trust-boundaries)
3. [Principals](#principals)
4. [Visibility model](#visibility-model)
5. [Personal scopes: the "own root" answer](#personal-scopes-the-own-root-answer)
6. [Read semantics across members](#read-semantics-across-members)
7. [Edge and traversal visibility](#edge-and-traversal-visibility)
8. [Dedup under visibility](#dedup-under-visibility)
9. [Space administration](#space-administration)
10. [Scale bounds, stated honestly](#scale-bounds-stated-honestly)
11. [Testing and validation](#testing-and-validation)
12. [Acceptance criteria](#acceptance-criteria)
13. [Deferred work](#deferred-work)

---

## Goals

### Primary

- A team shares one space: each member has **private-by-default personal memory**, the team has **shared contexts** (projects) both can read and write, and a member can **opt their memory open** to teammates ("the designer checks the coder's memory" — legal only because the coder chose `team-read`).
- An update to a shared project context is immediately visible to every member who can read that scope — no sync, no copies; it's one graph.
- Privacy failures are structural: a node, an edge, or a dedup candidate that touches something a principal cannot read **never appears in any response to them**, including indirectly (edge lists, resonance reports, briefing sections).
- Single-person spaces (`nova`, `alex`) reduce *exactly* to the previous model with zero behavioral change.

### Non-goals

- **Cross-space sharing.** Unchanged and worth restating: spaces remain hard walls. Teams are multi-principal *within* one space — the isolation promise from [01](01-core-data-model.md)/[03](03-api-auth-and-tenancy.md) survives intact because sharing was placed inside the boundary, not through it.
- Enterprise IAM (groups-of-groups, attribute-based policies, delegation chains). The model is: members, three visibility levels, per-principal grant exceptions. If a team needs more, they need a different product.
- Concurrent-editing semantics beyond Postgres transactions (no CRDTs, no locks; last-write-wins on `update`, and dedup arbitrates near-duplicate writes as always).

---

## The key reframing: spaces are trust boundaries

Before: space = one human. Now: **space = one trust boundary** — a person, a household, or a team that has *chosen* to share an instance-mediated memory. Within it, principals and visibility do the fine-grained work. Between spaces, nothing changes: no query, token, edge, or dedup comparison crosses a space, ever.

This placement is the load-bearing decision. The alternative (grants *between* single-user spaces) was rejected because it makes isolation conditional — every cross-space code path is a potential leak in the product's most important guarantee, and the mental model ("my space is mine, full stop") collapses into ACL archaeology. Inside a space, members already trust each other socially; the visibility model expresses *boundaries within trust*, which is a much safer thing to get slightly wrong.

## Principals

```sql
CREATE TABLE principals (
  space_id   text NOT NULL REFERENCES spaces(id),
  id         text NOT NULL CHECK (id ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  display_name text NOT NULL,
  role       text NOT NULL DEFAULT 'member' CHECK (role IN ('member','space_admin')),
  archived   boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (space_id, id)
);
```

- Tokens now name **(space, principal, client)** — `(acme, jess, jess-workstation)`. The token still *is* the space selection; it is now also the principal selection. Nothing principal-like appears in any tool argument.
- Nodes gain `author_principal` (server-set from the token) alongside `source_client` — team memory needs *who*, not just *which device*.
- Every space has ≥ 1 principal; single-person spaces have exactly one, created automatically at `space create` (`--principal devon`).

## Visibility model

Visibility lives on **scopes** (not nodes — per-node ACLs are how systems become unauditable; a node's privacy is its scope's privacy, and moving a node between scopes is an explicit, logged act):

| Level | Owner | Other members | Typical use |
|-------|-------|---------------|-------------|
| `private` (default) | read/write | nothing — including existence | Personal memory, drafts |
| `team-read` | read/write | read | "My professional memory, open to teammates" — the designer-checks-coder case |
| `team-write` | read/write | read/write | Shared project context; updates visible to all instantly because there's one graph |

```sql
ALTER TABLE scopes ADD COLUMN owner_principal text,       -- NULL for ownerless team-write scopes
                   ADD COLUMN visibility text NOT NULL DEFAULT 'private'
                     CHECK (visibility IN ('private','team-read','team-write'));

CREATE TABLE scope_grants (                                -- per-principal exceptions
  space_id  text NOT NULL,
  scope_id  text NOT NULL,
  principal text NOT NULL,
  level     text NOT NULL CHECK (level IN ('read','write')),
  PRIMARY KEY (space_id, scope_id, principal),
  FOREIGN KEY (space_id, scope_id)  REFERENCES scopes (space_id, id),
  FOREIGN KEY (space_id, principal) REFERENCES principals (space_id, id)
);
```

Effective access = max(visibility-derived level, grant level). Grants cover the real-world exceptions cheaply ("share my `health` scope with my partner only") without inventing groups. Visibility changes and grants are audited and owner-or-space-admin-only.

The readable/writable sets are computed by one SQL function pair (`engraphy_readable_scopes()`, `engraphy_writable_scopes()`) reading the session GUCs — used identically by application queries and the revised RLS policies ([01](01-core-data-model.md)), so app and backstop cannot disagree.

## Personal scopes: the "own root" answer

"Each person has their own root node in the graph" maps to a mechanism, not a literal root node: **every principal gets a personal scope at member-creation** — `personal-<principal>`, `private`, `ambient`, owned by them. *(Deferred in part, Devon 2026-07-19: as shipped, auto-creation happens only on the CLI `space create` path — the founding principal. A member added later via `admin_member_add` gets no personal scope automatically (the pinned envelope returns the principal row only, and migration 0016's scopes policy deliberately doesn't let a space_admin create a scope owned by someone else) and creates their own via `scope_create`. Revisit at E6: honoring this sentence for MCP-added members needs `admin_member_add` to create the scope in the same transaction plus a scoped widening of the scopes INSERT policy.)* Their preferences, habits, and self-knowledge live there and ride along on all their queries (ambience is per-reader: an ambient scope joins the scope-set of principals *who can read it*, so private-ambient = personal always-on context, team-read-ambient = org-wide always-on context like a glossary). A pack that wants a literal self-node (`person` type, "this is Jess") writes one in the personal scope — convention, not schema.

## Read semantics across members

For a principal P:

- `search(scope=X)` → X ∪ P-readable ambient scopes, if P can read X (else a named permission error).
- `search(scope='all')` → **all P-readable scopes** — this is the "check the coder's memory" query: it naturally spans the coder's `team-read` scopes without P knowing or naming them.
- `briefing(scope)` → sections run against the same readable set; a shared project's briefing is identical for both members *except* each sees their own private/personal overlays.
- `get(ids)` → non-readable ids return not-found (never permission-denied — existence is information).
- Responses annotate provenance: `author_principal` and scope on every returned node, so "who said this" is always visible in team contexts.

## Edge and traversal visibility

The subtle leak surface. Rules:

- **Creation:** an edge may be created by P iff P can *read both* endpoints and *write at least one* endpoint's scope. (The v1 "one endpoint must be ambient" trigger rule is retired — it was the single-user approximation of pollution control; visibility now does that job properly. Single-principal spaces lose nothing: all their scopes are readable to their one principal, and packs can still keep cross-scope hygiene as protocol.)
- **Visibility:** an edge exists *for P* only if P can read **both** endpoints. A private→shared edge is invisible to teammates even though they see the shared node — edge lists, edge counts, and traversal all filter through both-endpoint readability. This is enforced in the traversal CTE and the hydration layer, and by the RLS policy on `edges` (join to both endpoint scopes).
- **Traversal:** walks simply never cross into unreadable territory; the walk continues around it (no "redacted node" placeholders — again, existence is information).

## Dedup under visibility

Dedup candidates for a write by P into scope X = same-type, active nodes in **X ∪ P-readable ambient scopes** — exactly the read surface of the write's context, never wider. Consequences, deliberate:

- A resonance report can never show P a private node of another member.
- Two members writing similar private notes do **not** dedup against each other (correct: their memories are theirs).
- Shared scopes dedup normally across authors — the second writer gets the handshake, which is precisely the "someone already updated the project context" moment surfacing at write time.
- Accepted cost: the same fact can exist in two members' private scopes. That is not duplication, it's privacy.

## Space administration

The instance-admin-is-local-CLI rule ([03](03-api-auth-and-tenancy.md)) survives, but a *cloud team* can't SSH to invite a teammate. Split:

| Operation | Who | Surface |
|-----------|-----|---------|
| Create/archive spaces, apply/upgrade packs, migrations, imports, instance config | Instance operator | `engraphy-admin` local CLI only (unchanged) |
| Add/archive members, mint/revoke tokens *within the space*, create scopes, set visibility, manage grants | `space_admin` principals | **Space-admin MCP tools** (`admin_member_add`, `admin_token_create` — returns display-once token, `admin_scope_visibility`, `admin_grant`), available only to tokens whose principal has the role; all audited |
| Change own scopes' visibility, grant own scopes | Scope owner | Same tools, owner-scoped — **deferred** (see below) |

Two E2-shipped narrowings/widenings against this table, both deliberate (Devon, 2026-07-19; revisit at E6):

- **Owner self-service is deferred.** All four admin tools are `space_admin`-only as shipped — a plain member who owns a scope gets `ENGRAPHY_ROLE` from `admin_scope_visibility`/`admin_grant` on their own scope. Restrictive is the safe direction on a security gate (a functionality gap, not a leak); honoring the owner row later means branching both the tool-layer gate and migration 0016's RLS predicates on `owner_principal = caller OR space_admin`.
- **A space_admin reads all scope *metadata*.** Postgres applies the SELECT policy to `UPDATE … RETURNING`'s row-read, so `admin_scope_visibility` could not target an unreadable private scope at all; the fix (`scopes_admin_read`, migration 0016) lets a space_admin read every scope's metadata — id, name, visibility, owner, **including the existence of members' private scopes** — which is what "space_admin: set visibility" presupposes. Node visibility is untouched: `nodes_read` gates on `engraphy_readable_scopes()`, not the scopes policy, so no node in a private scope becomes readable. This is a bounded, tested carve-out from "existence is information", scoped to the admin role.

This is the minimal set that lets a team self-operate day-to-day while the blast radius of any network-reachable credential stays inside its space. Token minting via MCP is a real widening for space admins — accepted for cloud teams, and a config flag (`space_admin_tools: false`) lets a paranoid local deployment keep the old everything-via-CLI posture.

## Scale bounds, stated honestly

Designed and tested for **2–25 principals per space**. The visibility model is O(scopes×grants) with tiny constants; nothing breaks at 100 members except the social model — three visibility levels assume members mostly trust each other, and the product refuses (in documentation, not code) to pretend otherwise. Enterprise features (SSO, groups, compliance exports) are explicitly some other product's job — or a future major version that earns them.

## Testing and validation

| Test | Assert |
|------|--------|
| Visibility matrix | Every (level, grant, role) × (read, write, edge-create) combination against fixtures — exhaustive, generated |
| Existence hiding | Non-readable ids: not-found; searches: absent; edge counts exclude invisible edges; timing not obviously divergent |
| Both-endpoint edge rule | Private→shared edge invisible to a teammate who reads the shared node; visible to owner |
| Traversal boundary | Walk from a shared node never enters teammate-private scopes; walk *around* works |
| Dedup privacy | Similar private nodes of two members never co-candidate; resonance never crosses readability |
| Ambience per-reader | Personal ambient joins only owner's queries; team-read ambient joins everyone's |
| Space-admin tools | Role-gated; audited; disabled by the config flag |
| Single-principal reduction | Full pre-v2 test suite passes unchanged on a one-member space |
| RLS parity | The readable-set function used by app and RLS produce identical sets under fuzzing |

## Acceptance criteria

- [ ] The three scenario stories demonstrated end-to-end on a live two-member space: (1) designer's `search(all)` surfaces the coder's `team-read` architecture note; (2) member A updates a `team-write` project node — member B's next briefing shows it, attributed to A; (3) member A's private scope is invisible to B in every read path (the generated matrix, green).
- [ ] Single-principal spaces bit-identical in behavior to tenancy v1 (`nova` and `alex` unaffected).
- [ ] Dedup-privacy and edge-visibility suites green.
- [ ] A space-admin (not the instance operator) onboards a third member entirely via MCP tools from a normal client.
- [ ] Cross-**space** fuzz suite re-run, still zero leaks — sharing added nothing across the hard wall.

## Deferred work

| Item | Rationale |
|------|-----------|
| Named sub-teams / groups within a space | Grants cover exceptions at ≤ 25 members; groups are complexity that waits for demand |
| Node-level visibility overrides | Scope-level is auditable; punching node-holes in it is how ACL systems rot |
| Notifications on shared-scope writes ("B updated the project context") | Wants the event/webhook surface (03 deferred) — design together |
| SSO/OIDC for team login | Rides with the cloud-hardening item in [04](04-operations-and-governance.md) |
