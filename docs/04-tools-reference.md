# Tool / API Reference

Engraphy exposes its capabilities as **MCP tools** over `POST /mcp` (Streamable
HTTP, bearer-authenticated). The tool list is assembled **per space** — the twelve
core tools below, the four `admin_*` tools (unless a space sets
`space_admin_tools: false`), plus any aliases the space's pack declares.

- **Argument types are enforced server-side** against a fixed spec (unknown argument
  names are refused; explicit `null` is refused — omit a key instead; out-of-range
  integers are *clamped*, not rejected). The published `inputSchema` is generated
  from that same spec, so what a client sees and what the server accepts cannot
  drift.
- Every envelope carries `"v": 1` (a version field). Errors come back as
  `ENGRAPHY_<CODE>` strings (e.g. `ENGRAPHY_SCOPE_UNKNOWN`, `ENGRAPHY_VALIDATION`,
  `ENGRAPHY_ROLE`).

Examples below show the **arguments** object you send and the **envelope** you get
back.

---

## Read tools

### `search` — hybrid semantic + lexical retrieval

Vector leg + lexical leg fused with RRF, read-time near-duplicate collapse, cut to
`limit`.

| Param | Type | Required | Notes |
|---|---|---|---|
| `scope` | string | ✓ | a scope id, or `"all"` for every readable scope. |
| `query` | string | ✓ | natural-language query. |
| `types` | string[] |  | restrict to these node types. |
| `limit` | integer |  | clamped to 1–25 (default 25). |
| `include_inactive` | boolean |  | include non-`active` nodes. |
| `detail` | `"full"`\|`"summary"` |  | `summary` omits bodies. |

```jsonc
// arguments
{"scope": "personal-devon", "query": "descale the espresso machine", "limit": 5}

// envelope
{
  "v": 1,
  "detail": "full",
  "results": [
    {"node": {"id": "…uuid…", "type": "note", "title": "Coffee machine",
              "body": "Descale the office coffee machine monthly.", "attrs": {}},
     "score": 0.032, "similarity": 0.71, "edge_count": 0}
  ],
  "scopes_searched": ["personal-devon"],
  "truncated": false
}
```

`score` is the fused RRF score; `similarity` (present when the node hit the vector
leg) is cosine 0–1; `edge_count` hints how much is attached (walk it with
`traverse`).

### `traverse` — graph walk from a node

| Param | Type | Required | Notes |
|---|---|---|---|
| `start_id` | uuid | ✓ | the node to walk from. |
| `direction` | `"out"`\|`"in"`\|`"both"` | ✓ | edge direction. |
| `edge_types` | string[] |  | restrict to these edge types. |
| `max_depth` | integer |  | clamped downstream (default 4). |
| `limit` | integer |  | clamped downstream (default 50). |
| `detail` | `"summary"`\|`"full"` |  | default `summary` (no bodies). |

```jsonc
// arguments — every fact attached to a person, one hop
{"start_id": "…person-uuid…", "direction": "both", "max_depth": 1}

// envelope
{
  "v": 1, "detail": "summary",
  "nodes": [{"id": "…", "type": "fact", "title": "…", "depth": 1}],
  "edges": [{"src": "…", "dst": "…", "type": "involves"}],
  "truncated": false
}
```

Common patterns: depth-1 `both` from an entity lists everything about it;
`edge_types: ["same_topic"]` from a hit lists its merge-linked cluster siblings.

### `get` — full nodes by id

| Param | Type | Required | Notes |
|---|---|---|---|
| `ids` | uuid[] | ✓ | up to 25 ids. |

Unreadable or unknown ids come back in `missing` (existence is information; never an
error). Each node carries capped edge summaries (≤10 out, ≤10 in).

```jsonc
// arguments
{"ids": ["…uuid-a…", "…uuid-b…"]}

// envelope
{
  "v": 1,
  "nodes": [
    {"id": "…uuid-a…", "type": "note", "title": "…", "body": "…", "attrs": {},
     "status": "active", "created_at": "2026-07-31T…Z",
     "edges": {"out": [{"dst": "…", "type": "references"}], "in": []}}
  ],
  "missing": ["…uuid-b…"]
}
```

### `briefing` — pack-declared session-start sections

| Param | Type | Required | Notes |
|---|---|---|---|
| `scope` | string | ✓ | scope id, or `"all"`. |
| `hint` | string |  | seeds the pack's `semantic: true` sections. |

Returns `{"v": 1, "sections": [...], "footer": {"inbox_pending": N}}`, section
order = pack order. Without a `hint`, semantic sections are empty by design.

### `pending_list` — your unresolved duplicate-check writes (read-only)

When a `write` bands as a borderline duplicate it parks as a pending
duplicate-check verdict instead of committing. `pending_list` returns your own
parked writes so you can settle them with `resolve_duplicate`.

| Param | Type | Required | Notes |
|---|---|---|---|
| `limit` | integer |  | page size. |
| `offset` | integer |  | page offset. |

Read-only, and scoped to the calling principal: you only ever see your own pending
writes. Returns `{"v": 1, "pending": [{"id": "…", "scope": "…", "title": "…", "against": ["…uuid…"], "created_at": "…Z"}], "total": N}`.

### `stats` — usage metrics (read-only)

A totals block plus a zero-filled daily time series, for dashboards and quotas.

| Param | Type | Required | Notes |
|---|---|---|---|
| `group_by` | string |  | `"space"` (all principals in the space) or `"user"` (just you). Defaults to `"user"`. |
| `range_days` | integer |  | length of the daily series. |

Read-only. Returns `{"v": 1, "group_by": "user", "totals": {"writes": N, "reads": N, "nodes": N}, "series": [{"date": "2026-08-01", "writes": N, "reads": N}, …]}` with every day in range present (zero-filled), so a chart never has gaps.

---

## Write tools

### `write` — dedup-banded write

The core write. Embeds the content, bands it against existing memory, and returns
the outcome plus a **resonance** report (related nodes above `resonance.floor`,
excluding the node just written).

| Param | Type | Required | Notes |
|---|---|---|---|
| `scope` | string | ✓ | must be writable by the caller. |
| `type` | string | ✓ | a pack-declared node type. |
| `title` | string | ✓ | 3–200 chars. |
| `body` | string | ✓ | 1–8000 chars. |
| `attrs` | object |  | validated against the type's schema. |
| `links` | link-item[] |  | edges to attach to the new node (see *Link items*). |
| `session_id` | string |  | provenance grouping. |

The `outcome` is one of:

| outcome | meaning |
|---|---|
| `inserted` | new node created; envelope has `node`. |
| `merged` | absorbed into an existing canonical (a near-verbatim restatement with high token overlap); envelope has **`canonical`** (the node it merged into — `id`, `type`, `scope`, `title`, `body`, `attrs`, `status`, `author`, `created_at`), `similarity`, `addendum_added`, `links_attached`/`links_skipped`, and an `instruction` to call `supersede` if you were *correcting* rather than restating. |
| `merged_linked` | same topic but distinct wording/content — inserted as its **own** node and `same_topic`-linked; envelope has `node` (the new member) and `similarity`. |
| `needs_confirmation` | borderline (pending band) — nothing written yet; envelope has `pending_id`, `similarity`, and an `instruction` to call `resolve_duplicate`. |

> Which of `merged` / `merged_linked` / `needs_confirmation` you get depends on the
> *embedding similarity* and *novelty* of the text: an identical restatement absorbs
> (`merged`); a reworded note on the same topic is kept and linked (`merged_linked`);
> a related-but-less-similar write in the `[t_low, t_high)` band parks
> (`needs_confirmation`). The one thing you never get for a near-duplicate is a blind
> second `inserted` node.

```jsonc
// arguments
{"scope": "personal-devon", "type": "note",
 "title": "Coffee machine", "body": "Descale the office coffee machine monthly.",
 "attrs": {}}

// envelope (first write)
{"v": 1, "outcome": "inserted",
 "node": {"id": "…", "type": "note", "title": "Coffee machine", "status": "active"},
 "resonance": []}

// envelope (an identical re-write later — note the `canonical` key, not `node`)
{"v": 1, "outcome": "merged",
 "canonical": {"id": "…same id…", "type": "note", "title": "Coffee machine",
               "status": "active", "…": "…"},
 "similarity": 1.0, "addendum_added": false,
 "instruction": "…if you were correcting the fact, call supersede…",
 "resonance": [...]}
```

> **If a write comes back `merged` but you were updating/contradicting the stored
> fact, call `supersede`.** Auto-merge cannot tell a correction from a restatement.

### `supersede` — replace a node, preserving history

Atomically inserts a new node, flips the old node's `status` to `superseded`, and
attaches a `supersedes` edge. Takes `old_id` (uuid, required) plus all of `write`'s
fields (`scope`, `type`, `title`, `body`, `attrs`, `links`, `session_id`). Must be
the same node type as the replacement.

```jsonc
{"old_id": "…old-uuid…", "scope": "personal-devon", "type": "preference",
 "title": "Prefers tea", "body": "Devon now prefers tea over coffee.",
 "attrs": {"strength": "soft"}}
```

### `update` — edit in place

| Param | Type | Required |
|---|---|---|
| `id` | uuid | ✓ |
| `title` | string |  |
| `body` | string |  |
| `attrs` | object |  |

Re-embeds only if the searchable text actually changed. Use `update` for
copy-edits; use `supersede` when the *fact* changed.

### `link` — attach edges between existing nodes

| Param | Type | Required |
|---|---|---|
| `edges` | link-item[] | ✓ |

**Link items** are `{type, src_id, dst_id}`. For `link.edges`, **both** endpoints
are required (they connect two existing nodes). For `write.links` / `supersede.links`,
supply exactly **one** endpoint — the other is the node being written. Edges are
rule-checked against the pack's `edge_rules`.

```jsonc
{"edges": [{"type": "references", "src_id": "…note…", "dst_id": "…project…"}]}
```

### `resolve_duplicate` — settle a pending verdict

| Param | Type | Required | Notes |
|---|---|---|---|
| `pending_id` | uuid | ✓ | from a `needs_confirmation` write. |
| `resolution` | `"distinct"`\|`"merge"` | ✓ | |
| `merge_into` | uuid | required when `resolution == "merge"` | the canonical to fold into. |

`distinct` promotes the parked write to its own node (attaching a "declared
distinct" `relates_to`); `merge` folds it into `merge_into`. Returns the write
envelope of the final outcome.

---

## Scope tools

### `scope_guide` — the routing manifest (read-only)

The tool an agent calls to decide **where** a memory belongs. It returns every
scope the caller can *write* to, each with the description that scope was created
with — a routing table, not just a list of names.

No arguments. Returns `{"v": 1, "scopes": [{"id": "work", "display_name": "Work",
"description": "Projects, decisions, and people at my job. Write here for anything
job-related."}, …]}`. Because every scope carries a mandatory description (set at
`scope_create` time and by the operator), the guide is self-explaining: an agent
fetches it once, reads the descriptions, and picks the right scope for each write
instead of guessing or dumping everything into one bucket. Marked `readOnlyHint`.

Pair it with `scope_list` (what you can *read*) — `scope_guide` is the write-side
counterpart, and the reason scope descriptions are mandatory.

### `scope_list` — scopes you can read

No arguments. Returns the scopes this token can read — your starting point for
knowing where retrieval will look.

### `scope_create` — create a private scope

| Param | Type | Required | Notes |
|---|---|---|---|
| `id` | string | ✓ | new scope id. |
| `display_name` | string | ✓ | |
| `description` | string | ✓ | non-empty; this is what `scope_guide` surfaces for routing, so describe *what belongs here*. |
| `confirm` | boolean | ✓-ish | must be `true` to actually create (a guard against typo-scoped writes). |

Creates a new private scope owned by the caller.

---

## Inbox tool

### `inbox_review` — triage captured items

| Param | Type | Required | Notes |
|---|---|---|---|
| `action` | `"list"`\|`"promote"`\|`"discard"` | ✓ | |
| `limit`, `offset` | integer |  | for `list`. |
| `id` | uuid | required for `promote`/`discard` | the inbox item. |
| `type`, `scope`, `title`, `body` | string | required for `promote` | the reviewer authors the promoted node. |
| `attrs`, `links`, `session_id` | | | as for `write`. |

`list` shows pending captures (fed by `POST /inbox`); `promote` writes one through
the standard dedup pipeline (so a promoted item can itself merge); `discard` drops
one.

---

## Admin tools (space_admin only)

Present unless the space sets `space_admin_tools: false` (then they are absent
entirely — the operator does everything via the `engraphy-admin` CLI). Each requires
the caller's principal to have the `space_admin` role.

| Tool | Params | Effect |
|---|---|---|
| `admin_member_add` | `id`✓, `display_name`✓, `role` (`member`\|`space_admin`) | add a principal to this space. |
| `admin_token_create` | `principal`✓, `client_name`✓, `role`✓ (`readwrite`\|`readonly`) | mint a display-once bearer token. |
| `admin_scope_visibility` | `scope_id`✓, `visibility`✓ (`private`\|`team-read`\|`team-write`) | change a scope's visibility. |
| `admin_grant` | `scope_id`✓, `principal`✓, `level`✓ (`read`\|`write`) | grant a principal access to a scope. |

> Space **bootstrapping** (`space create`) and the initial token are CLI-only —
> there is deliberately no network path for them. See the
> [Deployment guide](05-deployment.md).

---

## The `/inbox` HTTP endpoint

Separate from the MCP tools, `POST /inbox` (same bearer auth) captures a raw item
for later triage:

```jsonc
// POST /inbox   Authorization: Bearer <token>
{"kind": "note", "payload": {"text": "look into the new pgvector HNSW tuning"},
 "scope": "personal-devon"}
```

It returns the created inbox item; review it later with `inbox_review`.

Next: the [end-to-end tutorial](06-tutorial.md) puts these together on a real pack.
