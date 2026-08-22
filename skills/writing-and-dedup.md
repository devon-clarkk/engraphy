# Writing memory, dedup, and the contradiction contract

Writing to Engraphy is not a plain insert. Every write runs through
deduplication, so the same fact told twice becomes one memory, not two. Most of
the time this is invisible and free. But it means a write can come back three
different ways, and **one of them requires you to act.** This skill covers all
three, and the one contract you must not miss.

## Writing

`write(scope, type, title, body, attrs?, links?, session_id?)`

- **`scope`**: a scope you can write to (see
  [scopes-and-visibility.md](scopes-and-visibility.md)).
- **`type`**: a node type defined by the space's pack. The available types come
  from the connected server's tool list, not from these skills; an unregistered
  type is rejected.
- **`title`** and **`body`**: the memory itself. Write one durable fact per node.
  Titles and bodies have length bounds; a good title is a short, retrievable
  summary and the body carries the detail.
- **`attrs`** (optional): typed structured fields the pack defines for that type.
- **`links`** (optional): typed edges to existing nodes, created with the node.
- **`session_id`** (optional): provenance for where this write came from.

Write what is *durably worth remembering* — a lasting preference, a person's
context, a commitment, a decision, a hard-won fact. Don't write transient
conversation state.

**Re-telling is safe.** If you write something the space already knows, dedup
absorbs it rather than creating a second row. You do not need to search-before-
write to avoid duplicates — the write path does that for you.

## The three outcomes of a write

The write envelope's `outcome` field tells you what happened:

### `inserted` — new memory

No close match existed. The node was created. The envelope also carries a
**resonance report**: the top few *existing* memories most similar to what you
just wrote, with their links. Read it — it is how you notice "you already know
something about this": a recurrence of an old error, a decision this relates to,
a fact worth linking. Acting on resonance (e.g. adding a `link`) is optional but
often valuable.

### `merged` — absorbed into an existing memory (**read the instruction**)

Your write was close enough to an existing *canonical* node that the engine
folded it in rather than creating a duplicate. The envelope returns that
canonical node, an `addendum_added` flag, and — importantly — an **`instruction`
field**.

**This is the contract that matters most. Read and act on the `instruction`
the envelope returns.** Its substance:

> Compare your write against the canonical node's body. If your text
> **contradicts or updates** the stored fact rather than **restating** it, call
> `supersede` with `old_id` set to the canonical node's id, to make your version
> the current fact.

Why this is on you and not on the engine: deduplication matches on *aboutness*,
not agreement. "Priya is a paediatric nurse" and "Priya is no longer a
paediatric nurse" are about the same thing and score as near-identical — so a
correction gets **merged into the very fact it overturns.** When that happens:

- your new text is stored, at best, as an **addendum** — which is `get`-only and
  is **not re-embedded**, so `search` and `briefing` keep returning the *old*
  body as the current fact;
- the contradicted node stays active and canonical;
- and if your wording was very close to the original, your text may not even be
  kept as an addendum (`addendum_added: false`).

The engine cannot tell a restatement from a contradiction — that is a judgment,
and judgment is yours. So whenever you get `merged`, ask: *was I restating, or
was I changing the fact?* If you were changing it, `supersede` the canonical node
(next section). `supersede` works exactly as well after an absorbing merge as
before it. If you were merely restating, do nothing — the merge is correct and
complete.

One sentence to carry: **re-telling the engine something it already knows is
free; telling it something has *changed* is a `supersede`, not a plain `write`.**

### `needs_confirmation` — a possible duplicate (**you decide**)

The write landed in the uncertain middle band: similar enough to existing memory
that the engine won't silently insert *or* merge. **This is a normal result, not
an error.** No node was created yet. The envelope gives you a `pending_id`, the
`candidates` it matched against, and an expiry.

Resolve it with `resolve_duplicate`:

- `resolve_duplicate(pending_id, resolution: "distinct")` — these really are
  different; create the new node (it is linked to the nearest candidate).
- `resolve_duplicate(pending_id, resolution: "merge", merge_into: <candidate id>)`
  — it's the same memory; fold it into that candidate. **`merge_into` is
  required when resolving as `merge`.**

Resolve promptly: a pending write expires (a `PENDING_EXPIRED` error on a late
resolve means you must simply write again).

## Correcting and editing existing memory

- **`supersede(old_id, …write fields)`** — the repair verb. It writes your new
  node *and* flips the old one's status, atomically, so the correction becomes
  the current fact and the stale one is retired. Use it whenever new information
  **replaces** an old memory — including after a `merged` outcome told you your
  correction was absorbed. If your replacement text is itself a near-duplicate of
  some *third* node, `supersede` refuses the whole operation
  (`SUPERSEDE_CONFLICT`) rather than half-applying — resolve that collision and
  retry.
- **`update(id, title?, body?, attrs?)`** — pure editing of a node in place. Use
  it to fix a typo, add detail, or refine wording of a memory that is *still
  correct*. `update` does **not** run deduplication and does not retire anything;
  it just changes the record. (It preserves the node's merge history for you —
  you don't need to carry `addenda` back in.)

Rule of thumb: **`supersede` when the fact changed; `update` when the wording
changed.**

## Linking memory

`link(edges)` attaches typed edges between existing nodes — each edge is
`{type, src_id, dst_id}` with both endpoints given. Edges are rule-checked
against the pack: an edge type with no matching rule for those node types is
rejected (`EDGE_RULE`). (When creating a node, you can attach edges in the same
call via `write`'s `links`, where you give only the *other* endpoint and the new
node is the implied one.)

## The inbox (capture vs. memory)

Some deployments *capture* raw material to a staging inbox automatically. Capture
is not memory. `inbox_review` lets you `list` pending items and, for the ones
worth keeping, `promote` them — and **promotion is authoring**: you supply the
real `type`, `title`, `body`, and `attrs` for the node, running the full dedup
pipeline just like `write`. The captured payload is raw material, not the node.
Discard the rest.
