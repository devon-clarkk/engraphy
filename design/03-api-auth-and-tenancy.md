# Engraphy — API, Authentication, and Tenancy

The server surface: the MCP tool set, how tokens bind clients to spaces, roles, rate limits, audit, and the isolation model that makes "different accounts and devices" a guarantee instead of a feature request.

**Status:** Living document
**Last updated:** July 2026
**Scope:** Transport, tool surface, tool aliases, tokens/roles, isolation, rate limiting, audit, health
**Out of scope:** Network placement (a deployment concern — Engraphy is transport-security-agnostic but refuses plaintext auth on a public interface without an explicit opt-in — see [Transport](#transport)), data shapes ([01](01-core-data-model.md)), behaviors ([02](02-retrieval-and-dedup.md))

> **Design note:** Stack decision: Python 3.12, official MCP SDK/FastMCP, Streamable HTTP, psycopg 3, pydantic v2, one process. What changed: tools are pack-generic, tokens carry a space, `memory_log_error` became an alias, admin gained pack/space/import commands. **Revised July 2026 (tenancy v2):** tokens now carry a principal; space-admin tools added ([06](06-teams-and-sharing.md)).

---

## Table of contents

1. [Goals](#goals)
2. [Transport](#transport)
3. [The tool surface](#the-tool-surface)
4. [Tool aliases](#tool-aliases)
5. [Tokens, roles, and the isolation model](#tokens-roles-and-the-isolation-model)
6. [Rate limiting and caps](#rate-limiting-and-caps)
7. [Audit](#audit)
8. [Health and status](#health-and-status)
9. [File reference](#file-reference)
10. [Testing and validation](#testing-and-validation)
11. [Acceptance criteria](#acceptance-criteria)
12. [Deferred work](#deferred-work)

---

## Goals

### Primary

- Any MCP-capable client — Claude Desktop, Claude mobile, Claude Code, any future harness — connects over HTTP with a token and gets its space's memory. **Zero dependence on any vendor's conversation sync**: continuity lives here, so the phone↔desktop sync bug class is irrelevant by construction.
- Cross-space access is **inexpressible**, not just forbidden: no tool takes a space or principal parameter — the token is the identity.
- Within a team space, every read/write is filtered by the principal's readable/writable scope sets ([06](06-teams-and-sharing.md)); permission failures on specific ids present as not-found (existence is information).
- A small, stable tool contract that packs can *rename and sugar* but not change — clients written against Engraphy v1 tools keep working across pack changes.

### Non-goals

- REST/GraphQL parity (MCP + `/inbox` + `/healthz` + local admin CLI is the v1 surface; REST is deferred until a real non-MCP consumer exists).
- OAuth/OIDC in v1. Long-random bearer tokens over encrypted transport suit personal and small-team deployments, local or cloud ([04](04-operations-and-governance.md)). MCP-spec OAuth is the first team-hardening item on the deferred list — it matters most for cloud team instances and rides with SSO ([06](06-teams-and-sharing.md)).
- Instance administration over the network: token minting, space/pack management, imports, and hard deletes exist **only** as a local CLI (`engraphy-admin`) on the host. No code path, no bug surface.

## Transport

Streamable HTTP MCP at `/mcp`, bearer auth on every request; `POST /inbox` (same auth); `GET /healthz` (unauthenticated, deployment-gated). TLS is the deployment's choice (WireGuard/Tailscale, reverse proxy, or `tailscale cert`); Engraphy **refuses to start** when bound to a non-loopback, non-RFC1918/CGNAT interface without `insecure_transport_ok: true` — it exits nonzero naming the interface, it does not merely warn. *(Reconciled July 2026: this doc previously said "logs a prominent warning"; [04](04-operations-and-governance.md)'s "refuses" was the deliberate policy and is what shipped — `app.py::check_transport_security`. An unparseable hostname is classified public: fail closed. Note the check classifies the bind host only — `0.0.0.0` inside a container counts as private, so the real cloud boundary is the compose port mapping + reverse proxy, as `deploy/checklist.md`'s transport note states.)*

## The tool surface

Twelve core tool groups — the generic descendants of the original twelve plus the space-admin group (briefing absorbed the pack-specificity; `memory_log_error` became an alias; `scope_create` folded into `scope_list`'s module with `confirm: true`):

| Tool | Summary | Spec |
|------|---------|------|
| `briefing(scope, hint?)` | Pack-driven session-start sections | [02](02-retrieval-and-dedup.md) |
| `search(scope|'all', query, types?, limit?, include_inactive?, detail?)` | Hybrid + RRF; `detail: full|summary` (default `full`) | [02](02-retrieval-and-dedup.md), [07](07-implementation-contracts.md) |
| `traverse(start_id, edge_types?, direction, max_depth?, limit?, detail?)` | Recursive walk; `detail: summary|full` (default `summary` — bodies hydrated via `get`) | [02](02-retrieval-and-dedup.md), [07](07-implementation-contracts.md) |
| `get(ids ≤ 25)` | Full nodes + edge summaries | |
| `write(scope, type, title, body, attrs, links?, session_id?)` | Dedup-banded write; returns node or PENDING verdict + **resonance report** | [02](02-retrieval-and-dedup.md) |
| `link(edges[])` | Typed edges, rule-checked | |
| `update(id, title?, body?, attrs?)` | Re-embeds on text change | |
| `supersede(old_id, …write fields)` | Atomic replace + status flip | |
| `resolve_duplicate(pending_id, distinct|merge, merge_into?)` | The handshake's second half | [02](02-retrieval-and-dedup.md) |
| `scope_list()` / `scope_create(id, display_name, confirm: true)` | Scope management (create requires `readwrite`; new scopes are `private` to their creator by default) | |
| `admin_member_add` / `admin_token_create` / `admin_scope_visibility` / `admin_grant` | Space-admin tools — role-gated, audited, config-disableable | [06](06-teams-and-sharing.md) |
| `inbox_review(action: list|promote|discard, …)` | Staging queue | [02](02-retrieval-and-dedup.md) |

Signature notes pinned at the July 2026 fold-back: `write.session_id` is a boundary rename of the storage column `nodes.source_session` ([01](01-core-data-model.md)'s provenance pair; it also survives into parked pending-writes and merge addenda so `purge-session` can reach late-resolved and merged-away writes). `update` **never re-runs dedup banding** — pure content replacement, re-embedding iff supplied `title`/`body` changes the stored `title + "\n" + body`; the banded edit tool is `supersede`, which runs the full pipeline excluding `old_id` and **refuses any non-INSERT band** (whole call rolls back, `ENGRAPHY_SUPERSEDE_CONFLICT` — [07](07-implementation-contracts.md)). That (update, supersede) split is the design's division of labor, not an omission.

Contract stability rule: these signatures are **semver-major surface** ([04](04-operations-and-governance.md)). Tool *descriptions* — the text the model reads — are assembled per space: engine base text + pack `tool_descriptions` overrides, so the example pack's descriptions can speak a session protocol's language ("call briefing before your first action…") while Alex's speak plainly. Descriptions are explicitly *not* contract.

## Tool aliases

Packs may declare aliases — additional MCP tool names that bind to a core tool with preset/renamed arguments:

```yaml
tool_aliases:
  log_error:
    binds: write
    preset: {type: error}
    description: "Record something that went wrong, after it is understood…"
```

An alias is pure sugar: same validation, same audit identity (logged as `write via log_error`). This preserves the ergonomics of `memory_log_error` (a one-call habit) without packs touching code, and it's the full extent of pack-defined behavior in v1 — the deliberate ceiling from [01](01-core-data-model.md).

Alias semantics, pinned (July 2026 fold-back): a **preset always wins** over a caller-supplied value for the same argument — the preset is the alias's fixed identity (`log_error` is always `type: error`), never a default. An alias name that shadows a core tool name is rejected at `pack validate`/`pack apply` (silently rerouting the stable core surface would break the contract-stability rule above). Argument *renaming* is not supported in v1 — the pack schema accepts only `binds`/`preset`/`description`.

## Tokens, roles, and the isolation model

The app-auth layer (256-bit random bearers, SHA-256 hashes at rest, display-once minting, `last_used_at`, instant revocation) with the tenancy binding added:

```
api_tokens: id, space_id, principal, client_name, token_hash, role, revoked, last_used_at, created_at
            UNIQUE (space_id, principal, client_name)
```

- **A token names (space, principal, client, role).** `client_name` (`jess-workstation`, `alex-ipad`) becomes `source_client` provenance; `principal` becomes `author_principal` and drives all visibility filtering ([06](06-teams-and-sharing.md)).
- Roles: `readwrite`, `readonly`. Instance-admin does not exist in the token table; **space-admin** is a *principal* role (not a token property) gating the space-admin tools ([06](06-teams-and-sharing.md)) — disable-able per deployment via `space_admin_tools: false` for CLI-only postures.
- The isolation chain, each layer independent: token→(space, principal) binding (no identity parameters anywhere in the API) → every query filtered through `engram_readable_scopes()` → RLS backstop ([01](01-core-data-model.md)) → per-space packs mean even *type names* aren't shared implicitly.
- Revocation: `engraphy-admin token revoke --space alex --principal alex --client alex-ipad` (or the space-admin tool, within a space) — effective next request, no cache window.
- `engraphy-admin` verbs: `space create|list`, `principal add|archive`, `pack validate|apply|upgrade`, `token create|rotate|revoke|list`, `import`, `config get|set`, `purge-session` (the poisoning-cleanup tool: archive everything from a given `source_session`).

## Rate limiting and caps

Carried over and now per-token: sliding-window 60 reads/30 writes per minute (config-table overridable per space via `rate.read_per_min` / `rate.write_per_min`), result caps on every read tool, `max_result` truncation with explicit markers. Purpose unchanged — a stolen token bulk-exfiltrates slowly and loudly, and one space's runaway agent cannot starve another space (a new multi-tenant reason).

## Audit

`audit_log` gains `space_id` and `client_name`. Every mutating call, one row; reads of `scope='all'` also logged (carried-over exfiltration tripwire). Per-space audit extract: `engraphy-admin audit --space X --since …`.

## Health and status

`/healthz`: `{status, version, schema_version, spaces: N, embedding_model, last_backup_at?}` — `last_backup_at` read from an operator-configured status file path ([04](04-operations-and-governance.md)); no per-space data leaks through health.

## File reference

| File | Contents |
|------|----------|
| `engraphy/server/app.py` | MCP server app (the official MCP SDK's **low-level `Server`**, not the `FastMCP` decorator class — the tool list is per-space and per-request: pack aliases add names, admin tools are conditionally *absent*; a decoration-time-fixed list cannot express that), auth middleware, GUC-setting transaction wrapper, `/inbox`, `/healthz` |
| `engraphy/server/tools/*.py` | The eleven tools |
| `engraphy/server/aliases.py` | Pack alias binding |
| `engraphy/server/auth.py` | Token resolution, roles, rate limiter |
| `engraphy/admin/cli.py` | `engraphy-admin` |
| `engraphy/tests/test_{auth,isolation,aliases,tools}*.py` | Below |

## Testing and validation

The tool tests, generically, plus:

| Test | Assert |
|------|--------|
| Token isolation | `alex` token: every tool, every argument shape — zero `nova` rows reachable; fuzzed including crafted `ids` from the other space (`get` must 404 them, not leak) |
| No-space-parameter audit | API schema review: no tool accepts anything space-like |
| Alias parity | `log_error` == `write(type=error)` in validation, dedup, audit |
| Description assembly | Two spaces, same engine: different tool descriptions served |
| Revocation, roles, rate limits, readonly-vs-write | Carried-over suite |
| `purge-session` | Archives exactly the session's rows, leaves an audit trail |

## Acceptance criteria

- [ ] Claude Desktop, Claude mobile, and Claude Code each complete a full tool round-trip against one server with three different tokens.
- [ ] Cross-space fuzz suite: zero leaks, including via `get(ids)`, `traverse(start_id)`, and dedup candidates.
- [ ] Within-space visibility suite ([06's matrix](06-teams-and-sharing.md#testing-and-validation)): zero private-scope leaks between principals across every tool.
- [ ] Two spaces run different packs simultaneously; both briefings correct.
- [ ] Admin impossible over the network **by absence of code path** (review + scan).
- [ ] The example pack's aliases bind and round-trip against core tools (standalone CI). (Full reproduction of the twelve-tool contract is the E4 adoption gate — [05](05-roadmap.md).)

## Deferred work

REST surface for non-MCP consumers; MCP-spec OAuth for hosted deployments; per-scope token restriction; webhook/event stream for external automations.
