# Build Your Own Pack

A **pack** is the schema for a space: the node types, their attribute schemas, the
edge types, and the rules for which node types an edge may connect. The engine is
mechanism-only — it enforces whatever the pack declares, in the database — so a
pack is how you turn Engraphy into *your* memory application (a coding assistant's
memory, a CRM, a reading log, a research notebook).

A pack is a YAML (or JSON) file validated against `packs/schema.json`. You
`engraphy-admin pack validate` it, then `engraphy-admin pack apply` it into a space.

## Anatomy of a pack

| Key | Required | What it declares |
|---|---|---|
| `pack` | ✓ | pack name, `^[a-z][a-z0-9-]{1,40}$`. |
| `version` | ✓ | the pack's own content revision (an integer ≥ 1). |
| `node_types` | ✓ | the memory types and their attribute schemas. |
| `edge_types` | ✓ | the relationship types (and whether each is bidirectional). |
| `edge_rules` |  | which node types each edge may connect. |
| `ambient_scopes` |  | scopes unioned into every read/dedup candidate set (see note). |
| `briefing` |  | the session-start sections `briefing` returns. |
| `tool_aliases` |  | pack-specific tool names that bind to core tools with presets. |
| `tool_descriptions` |  | per-tool description overrides shown in `tools/list`. |
| `pack_format` |  | the pack *file format* version (distinct from `version`); absent = 1. |

### Node types

Each node type has a `description` and an optional attribute schema:

```yaml
node_types:
  book:
    description: "A book someone has read or is reading."
    attrs:
      required:
        author: {type: string}
      optional:
        finished_on: {type: date}
        rating: {enum: ["1", "2", "3", "4", "5"]}
      closed: true          # reject any attr not declared here (the default)
```

- An attribute is declared with **either** `type` (`string` | `int` | `number` |
  `bool` | `date`) **or** `enum` (a fixed list of allowed string values).
- `closed: true` (the default) rejects any attribute not in `required`/`optional`.
  Set `closed: false` for a free-form bag.
- Attribute validation is enforced by a **database trigger**, so a write with a
  bad attr is rejected at the row regardless of which path wrote it.

### The `searchable` attribute flag

Content-bearing attributes enter the **searchable surface** (the embedding + the
lexical tsvector), so a fact stored only in an attribute is *found by search*, not
merely retrievable by `get`. The default is by construct:

- `string` and `date` attributes are **searchable by default** (they carry
  content — an author's name, a place, a date).
- `enum`, `bool`, `int`, `number` are **not** searchable by default (they
  discriminate rather than describe).

Override per attribute with `searchable: true`/`false`:

```yaml
attrs:
  optional:
    occupation: {type: string}                    # searchable (string default)
    department: {type: string, searchable: false} # excluded on purpose
    role:       {enum: [lead, ic], searchable: true}  # include a low-cardinality enum
```

> Applying a pack that uses `searchable` warns unless it declares `pack_format: 2`.
> After changing searchability on a **live** space, run `engraphy-admin surface
> rebuild --space <space>` to re-index existing rows — the flag only affects new
> writes until a rebuild.

### Edge types and rules

```yaml
edge_types:
  about:      {description: "This note is about this book.",  bidirectional: false}
  by:         {description: "This book was written by this author.", bidirectional: false}
  relates_to: {description: "Weak generic association.",      bidirectional: true}
  supersedes: {description: "Replacement; old node becomes superseded.", bidirectional: false}
  same_topic: {description: "Same topic, distinct content; attached automatically on merge.", bidirectional: true}

edge_rules:
  - {type: about,      src: "*",  dst: book}
  - {type: by,         src: book, dst: author}
  - {type: relates_to, src: "*",  dst: "*"}
  - {type: same_topic, src: "*",  dst: "*"}
  - {type: supersedes, src: "*",  dst: "*"}
```

- `edge_rules` constrain `(type, src_type → dst_type)`; `"*"` is a wildcard. A
  `link` or auto-attached edge that violates the rules is rejected in the database.
- **Always declare `same_topic` and `supersedes`** (both `"*" → "*"` is fine): the
  engine's merge path attaches `same_topic` automatically, and the `supersede`
  core tool inserts `supersedes` — the pack must permit the edges the engine
  itself writes. `pack validate` warns if `same_topic` is missing.

### Ambient scopes

`ambient_scopes: [personal]` unions the named scope into every read and every dedup
candidate set. Useful for a "personal" scope that should always be in play — but
know that it makes those scopes' facts merge candidates across the space, so use it
deliberately.

### Briefing sections

`briefing` declares the session-start read. Sections run in pack order:

```yaml
briefing:
  sections:
    - {name: currently_reading, type: book,
       where_attr: {key: finished_on, after: "+0d"}, order_by_attr: finished_on}
    - {name: relevant, semantic: true, types: [book, note], top_k: 5}
    - {name: recent_notes, type: note, recent: 7d}
  footer: {inbox_pending_count: true}
```

- A `semantic: true` section runs hybrid search over the caller's `hint` (with a
  relevance floor); non-semantic sections filter/sort by type, `where_attr`,
  `recent: Nd`, etc. `top_k` caps at 10.

### Tool aliases & descriptions

`tool_aliases` lets a pack expose its own tool names bound to core tools with preset
arguments (e.g. a `remember` alias that presets `write`'s type). `tool_descriptions`
overrides the one-line description a client sees for a tool. Both are optional.

## Dedup thresholds are per-space *config*, not pack keys

The three dedup/resonance thresholds are **not** in the pack — they are per-space
`config` rows, so you can tune them for a space without editing its ontology:

| Config key | Default | Effect |
|---|---|---|
| `dedup.t_high` | `0.95` | at/above → merge band (auto-merge or merge-link) |
| `dedup.t_low` | `0.80` | `[t_low, t_high)` → pending (caller resolves) |
| `resonance.floor` | `0.75` | related-memory report threshold on writes |

Set them with the operator CLI (`value` is parsed as JSON):

```bash
engraphy-admin config set --space demo --key dedup.t_high --value 0.97
```

Raise `t_high` toward 1.0 to make auto-merge stricter (more things stay distinct);
raise `t_low` to shrink the pending band. Bands must satisfy
`0 < t_low ≤ t_high ≤ 1`.

## A complete worked example

Save as `reading-pack.yaml`:

```yaml
pack: reading
version: 1
pack_format: 2            # uses the `searchable` attr flag

node_types:
  book:
    description: "A book someone has read or is reading."
    attrs:
      required:
        author: {type: string}                 # searchable (string)
      optional:
        finished_on: {type: date}              # searchable (date)
        rating: {enum: ["1","2","3","4","5"]}  # not searchable (enum)
      closed: true
  author:
    description: "A book's author: who they are and where they're from."
    attrs:
      required:
        country: {type: string}
      closed: true
  note:
    description: "A durable thought, quote, or takeaway worth keeping."
    attrs: {closed: false}                     # free-form

edge_types:
  about:      {description: "This note is about this book.",  bidirectional: false}
  by:         {description: "This book was written by this author.", bidirectional: false}
  relates_to: {description: "Weak generic association.",      bidirectional: true}
  supersedes: {description: "Replacement; old node becomes superseded.", bidirectional: false}
  same_topic: {description: "Same topic, distinct content; attached automatically on merge.", bidirectional: true}

edge_rules:
  - {type: about,      src: "*",  dst: book}
  - {type: by,         src: book, dst: author}
  - {type: relates_to, src: "*",  dst: "*"}
  - {type: same_topic, src: "*",  dst: "*"}
  - {type: supersedes, src: "*",  dst: "*"}

briefing:
  sections:
    - {name: recent_notes, type: note, recent: 14d}
    - {name: relevant, semantic: true, types: [book, note], top_k: 5}
  footer: {inbox_pending_count: true}

tool_descriptions:
  write: "Save a book, an author, or a reading note. The server deduplicates — re-telling is safe."
```

Validate and apply it:

```bash
engraphy-admin pack validate reading-pack.yaml
# -> pack 'reading-pack.yaml' is valid

engraphy-admin pack apply reading-pack.yaml --space demo
# -> applied pack 'reading-pack.yaml' to space 'demo'
```

`pack validate` checks the file against the schema plus cross-checks (edge rules
reference declared types, briefing sections reference declared types, a
`same_topic`-declared warning). `pack apply` validates then writes the node/edge
registries, rules, and pack config into the space. It runs **once** per space — to
change an applied pack use `engraphy-admin pack upgrade`, which diffs the new pack
against the space and applies changes by class (additive immediately; tightening
only after a conformance scan or `--allow-nonconforming`; destructive refused while
active rows of that type exist).

## Keeping a space healthy

`engraphy-admin doctor --space <space>` is a read-only hygiene report: stale pending
verdicts, attribute-nonconforming counts, registry-vs-pack drift, orphaned merges,
canonical chains longer than 3, nodes with more than 20 addenda. Run it after an
upgrade or when something looks off.

Next: the [Tool reference](04-tools-reference.md) for calling into your pack, or the
[Tutorial](06-tutorial.md) to see one end-to-end.
