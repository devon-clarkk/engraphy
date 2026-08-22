# Retrieval — which tool when

Engraphy gives you four read tools. They are not interchangeable; each answers a
different question. Pick by intent.

| Tool | Use it to… |
|------|-----------|
| `briefing` | Load session-start context — "what should I know right now?" |
| `search` | Recall specific memory — "what do I know about X?" |
| `traverse` | Follow relationships between memories — "what connects to this?" |
| `get` | Hydrate one or more full records by id, including merge history. |

Every response is a JSON envelope carrying `"v": 1`. Nodes carry their `id`,
`type`, `scope`, `title`, `body`, `attrs`, `status`, `author`, and
`created_at` — see [contracts-and-reserved-names.md](contracts-and-reserved-names.md)
for the shared conventions.

## `briefing(scope, hint?)` — session start

Call `briefing` at (or near) the start of a conversation that could benefit from
memory. It returns a set of **named sections** defined by the space's pack —
things like due commitments, standing preferences, and recent notes — in a fixed
order, so you begin already knowing what matters.

- **Pass a `hint`.** Some sections are *semantic*: they surface memory relevant
  to what you're about to work on. Those sections search against your `hint`, so
  **without a hint they come back empty.** Give a short description of the topic
  or task ("planning the Q3 offsite", "debugging the auth flow") and the
  semantic sections will populate; omit it and you lose them.
- The `footer` may report `inbox_pending` — a count of captured items awaiting
  review (see the inbox note in [writing-and-dedup.md](writing-and-dedup.md)).
- `scope` is a scope id, or `"all"` for everything you can read.

## `search(scope, query, types?, limit?, include_inactive?, detail?)` — direct recall

`search` is hybrid: it runs a semantic (meaning-based) and a lexical (keyword)
retrieval and fuses them, so it catches both paraphrases and exact identifiers.
Use it whenever you need a specific fact and don't already have its id.

- **`scope`**: a scope id, or `"all"` to search everything you can read.
  `"all"` is the natural cross-member query in a team space — it spans
  teammates' shared memory without you naming their scopes.
- **`query`**: natural language; you don't need the exact words the memory used.
- **`types`** (optional): restrict to certain node types. An unknown type name
  simply matches nothing — it is not an error.
- **`limit`** (optional): up to 25 (the default and the cap).
- **`include_inactive`** (optional): by default only active memory is searched.
- **`detail`** (optional): `"full"` (default) returns bodies; `"summary"` omits
  them.
- Each hit carries `score` (fused rank) and, for semantic hits, `similarity`.
  **`search` does not judge relevance for you** — it returns its best matches and
  leaves you to weigh them. `truncated: true` means there were more than `limit`.

## `traverse(start_id, direction, edge_types?, max_depth?, limit?, detail?)` — follow relationships

When you have a node and want what connects to it — the decision a note supports,
the person a commitment involves, the error a fix derived from — `traverse` walks
the typed edges of the graph in one call instead of many.

- **`start_id`**: the node to walk from.
- **`direction`** (required): `"out"`, `"in"`, or `"both"`.
- **`edge_types`** (optional): restrict to certain edge types; omit to follow all.
- **`max_depth`** / **`limit`**: bounded (defaults 4 and 50) — the graph walk is
  capped so it can't flood your context.
- **`detail`** defaults to **`"summary"`** here (bodies omitted): a walk can
  return many nodes, so it returns them light. When you've found the nodes you
  care about, **hydrate them with `get`** to read their bodies.
- Returns `nodes` (each with its `depth` from the start) and the `edges` walked.

### Traverse with intent — name the edges you care about

An unfiltered walk returns the *whole* neighbourhood — every edge of every kind,
out to the depth limit. When your question is about one **specific** relationship,
that breadth works against you: the answer gets buried among generic-association
edges and everything else the entity happens to touch. `edge_types` is the fix.
Pass it a list naming only the relationships relevant to the question, and the
walk follows just those, so what comes back is focused enough to reason over.

The pattern for "what is *this specific relationship* about this entity?":

1. **Resolve the entity** — `search` for it (or `get` it if you already have the
   id) to get the node you want to walk from.
2. **Traverse with an `edge_types` filter** naming only the relationships the
   question is about — the person's preferences, their employer, who they know,
   the events they attended — rather than an open walk that returns all of it.
3. **Reason over that focused result** (hydrating bodies with `get` as needed).

For example, to judge whether someone would like a certain kind of thing: first
find that person's node, then traverse *their preference/interest edges
specifically* — not every edge they have — and weigh what those return. A broad
walk would bury the two or three relevant nodes under everything else the person
is connected to.

Which edge-type names to pass is a property of the **applied pack**, not of this
skill — the relationship vocabulary differs per deployment. Discover the legal
edge types from the connected server's tool surface (and the pack's agent guide),
then name the ones your question needs. The *pattern* — resolve the entity, then
walk only the edges that matter — is the same whatever those names are.

## `get(ids)` — hydrate full records

`get` returns complete records for up to 25 ids: full body, full `attrs`, and
two things no other tool includes:

- **`edges`** — the node's inbound and outbound links.
- **`addenda`** — the node's **merge history**. When re-tellings of a fact are
  absorbed into a canonical node, each is recorded as an addendum. **Addenda are
  `get`-only**: they never appear in `search`, `traverse`, or `briefing` output,
  and never inside any tool's `attrs`. If you need to see everything that has
  been folded into a memory over time — including corrections stored as addenda —
  `get` it.
- Ids that don't exist or that you can't see come back in `missing` (never an
  error). See [answer-discipline.md](answer-discipline.md).

`get` is how you go from "I found a reference to this" (a hit or a walked node)
to "I have the whole record in hand."
