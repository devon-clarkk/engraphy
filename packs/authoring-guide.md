# Authoring an Engraphy pack

A **pack** is how you teach one Engraphy space *your* vocabulary — the kinds of
memory you keep, how they relate, and what a well-formed memory looks like. It
is a single declarative YAML file. When applied to a space it installs a type
system that the database itself enforces: an unknown node type, an illegal edge,
or a malformed attribute is rejected at write time, for a schema the engine has
never seen before.

This guide is how you design one. The companion artifacts:

- **[pack-template.yaml](pack-template.yaml)** — a small, complete, valid pack
  that exercises every construct. Copy it and edit.
- **[starter/pack.yaml](starter/pack.yaml)** and
  **[conversational/pack.yaml](conversational/pack.yaml)** — two shipped packs
  to read as worked examples.
- **[../skills/](../skills/README.md)** — the generic agent-usage skills. A pack
  is only half the product; the other half is the agent guidance you pair with
  it (see [Pair the pack with agent guidance](#pair-the-pack-with-agent-guidance)).

## What a pack can and can't do

A pack is **declarative — there is no executable code in it.** It defines data
shapes and validation, plus some presentation (briefing sections, tool
descriptions, alias sugar). It cannot add new behaviour: the tools, the dedup
write path, retrieval, and visibility are the engine's, identical under every
pack. That boundary is deliberate — **mechanisms live in the engine, opinions
live in packs.** If your use case seems to need a genuinely new *operation*,
that's an engine feature request, not a pack.

## Design principles

Before the mechanics, the mindset that makes a pack age well:

1. **One fact per node.** Engraphy's dedup, correction (`supersede`), and retrieval
   all operate on whole nodes. A node should be one atomic, self-contained piece
   of knowledge — not a dossier. "Priya is a nurse" and "Priya lives in Leeds"
   are two nodes, so either can be recalled, deduped, or corrected on its own.
2. **The subset is the product — keep structured attrs few.** Attributes exist
   for the handful of fields you *filter or sort on* (a due date, a status, a
   strength). Everything else — the actual content — belongs in the node's
   `title` and `body`, which are embedded and searched. A wide attribute row is
   almost always a sign that knowledge that should be prose (or a separate node)
   is being forced into columns. The attr-spec grammar is intentionally small;
   if you find yourself wanting nesting, arrays, or regex, that is the signal to
   move the data into the body.
3. **Types earn their place.** Two node types should represent genuinely
   different *kinds* of memory — different enough that you'd never want one to
   dedup-merge into the other and you'd plausibly retrieve them separately.
   Don't split a type just to record a flag (use an attribute); don't merge two
   that mean different things.
4. **Write descriptions for the agent.** Every `description` is read by the model
   deciding which type to write or which edge to draw. Say what the type *is* and
   when to use it, in plain language.

## Anatomy

Read this next to [pack-template.yaml](pack-template.yaml), which shows each
construct in place.

### Identity

```yaml
pack: my-pack        # lowercase, hyphens ok, 2-41 chars
version: 1           # your pack's content revision; bump when you edit it
```

### Node types and the attr-spec

Each node type has a `description` and an optional `attrs` block. The attr-spec
is the deliberately small validation language a database trigger can interpret:

| Construct | Meaning |
|-----------|---------|
| `{type: string}` | A string, up to 2000 characters. |
| `{type: int}` | A whole number. |
| `{type: number}` | Any number. |
| `{type: bool}` | `true` / `false`. |
| `{type: date}` | A string in ISO-8601 date form (`YYYY-MM-DD`). |
| `{enum: [a, b, c]}` | A string from a fixed list. |
| `required:` / `optional:` | Presence classes — a map of attr-name → rule. |
| `closed: true` | Reject any attribute key not declared (the default; recommended). |
| `requires: [{key: X, when: {key: Y, equals: Z}}]` | The one conditional form: require `X` only when `Y` equals `Z`. |

That is the whole grammar: no nesting, no arrays, no regex, no cross-field
arithmetic. Attribute keys are lowercase/underscore, 2-41 chars. A `date` is
day-granularity; if you need a time of day, put it in the body.

### Edge types and rules

`edge_types` names your relationships; `bidirectional: true` marks the symmetric
ones (walked both ways in traversal). `edge_rules` then declares which
`(type, src-type, dst-type)` triples are **legal** — an edge with no matching
rule is rejected at write time. `"*"` means "any node type in this pack" and is
expanded to concrete rows when the pack is applied.

Two edges worth including in almost every pack:

- **`relates_to`** (usually `src: "*", dst: "*"`, bidirectional). Besides being a
  handy generic association, it is the edge the duplicate-check handshake
  attaches when a possible duplicate is resolved as *distinct*. Define it and
  those links get drawn for free; omit it and that step is silently skipped.
- **`supersedes`** (usually `src: "*", dst: "*"`). `supersede` is a core tool
  available under every pack, and it inserts a `supersedes` edge. If your pack
  doesn't permit that edge, corrections fail with an edge-rule error. A wildcard
  rule is safe: the engine already refuses cross-type supersession on its own.
- **`same_topic`** (usually `src: "*", dst: "*"`, bidirectional). Engine-attached
  like `supersedes`: when a write is a near-duplicate of an existing memory
  (≥ the merge threshold) but carries distinct content, the engine keeps *both*
  and links them with `same_topic` — a fact cluster you can walk to see every
  stored statement on a topic. Declare the type and a wildcard rule and the link
  is drawn for free. Omitting it degrades gracefully: the merge path still saves
  and searches both memories, only the cluster edge is skipped — and
  `pack validate` warns so the omission is never silent.

### Ambient scopes, briefing, and presentation

- **`ambient_scopes`** lists scope ids to treat as "ambient" — always joined to a
  reader's queries (a personal always-on context). This names scopes; it does not
  create them.
- **`briefing`** defines the session-start sections `briefing` returns, in order
  (max 8). The section grammar is a small, closed filter set — type/status
  filters, one attribute predicate, an edge presence/absence predicate, linked-
  node inclusion, a semantic top-k, recency. A `semantic: true` section surfaces
  nodes relevant to the caller's hint (and needs one). Every `type`/`types` must
  be a defined node type and every `edge` a defined edge type.
- **`tool_aliases`** add extra tool names that bind a core tool with a preset
  argument — pure sugar, no new behaviour; an alias may only preset its target
  tool's own arguments. **`tool_descriptions`** override the engine's default
  one-line text per tool; this is the main channel for teaching a hook-less
  client the protocol (see [deploy/clients.md](../deploy/clients.md)).

### Reserved names

Two names are the engine's and a pack may not declare them (validation rejects
it): the node type **`engraphy_sentinel`** and the attribute key **`addenda`**
(engine-managed merge history). See
[../skills/contracts-and-reserved-names.md](../skills/contracts-and-reserved-names.md).

## Validate, apply, evolve

Authoring is a loop with a validator you should run constantly:

```
engraphy-admin pack validate my-pack.yaml     # JSON Schema + cross-references; run this often
engraphy-admin pack apply --space <space> my-pack.yaml   # install into a space (local CLI only)
```

`pack validate` checks the file against `packs/schema.json` and then the
cross-references this guide describes (every edge rule and briefing type names a
defined type; aliases bind only core tools; names match the required pattern; no
reserved names). Empty output means valid.

Packs evolve with `pack upgrade`, which classifies each change:

- **Additive** (a new type, edge, or rule; a spec change no existing row
  violates) — applied immediately.
- **Tightening** (a spec change some existing active row would now fail) — applied
  only after a conformance scan; violators are reported and refused unless you
  opt in explicitly.
- **Destructive** (removing a type, edge, or rule) — refused while active data
  depends on it; the pattern is archive-the-data-first, remove across two pack
  versions.

Design forward-compatibly: adding is cheap, removing is deliberately not.

## Pair the pack with agent guidance

A pack defines the *shape* of memory; it does not by itself tell an agent how to
use memory well. That is what the **[skills/](../skills/README.md)** set does —
answer discipline, which read tool when, the write/dedup/`supersede` contract,
scopes and visibility — and it is pack-independent, so it applies to your pack
unchanged.

What is specific to *your* pack is a short **companion** that names your actual
types and edges and says when to write each. Ship one beside your pack, in the
voice of the skills. The two shipped examples —
[starter/agent-guide.md](starter/agent-guide.md) and
[conversational/agent-guide.md](conversational/agent-guide.md) — are the pattern
to copy: a table of "this situation → this type", the edges, and the
briefing/attr notes an agent needs, and nothing the generic skills already cover.

## A design checklist

- [ ] Every node type is a genuinely distinct *kind* of memory, with an
      agent-readable description.
- [ ] Structured attrs are only the fields you filter or sort on; everything else
      is in the body. `closed: true` unless you have a reason.
- [ ] Every edge you use has an `edge_rules` entry; `relates_to` and `supersedes`
      are defined.
- [ ] No reserved names (`engraphy_sentinel`, `addenda`).
- [ ] Briefing sections reference only defined types/edges; semantic sections are
      worth a context-window slot.
- [ ] `pack validate` returns clean.
- [ ] A pack companion pairs with the generic skills.
