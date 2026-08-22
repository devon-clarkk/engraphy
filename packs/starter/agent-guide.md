# Starter pack — agent guide

The **starter pack** is the shipped default for a new space: a general
assistant's memory, without any domain specialisation. This is its cheat sheet —
the pack-specific complement to the generic
[skills/](../../skills/README.md) set, which covers *how* to use memory
(answering only from what retrieval returns, which read tool when, the
write/dedup/`supersede` contract, scopes and visibility). Read those first; this
page only names the starter pack's own vocabulary.

## Node types — what to write, and when

| Situation | Write a… |
|-----------|----------|
| A stable, useful fact worth keeping, when no more specific type fits | `note` |
| Someone whose context matters across conversations | `person` |
| A durable like / dislike / default the user states | `preference` |
| A dated or recurring obligation | `commitment` |
| A pointer to an ongoing effort or external resource | `project_ref` |

Prefer the most specific type that fits; fall back to `note` only when nothing
else does.

### Required and optional attributes

Most content goes in the node's `title` and `body`. These types add a few
structured fields (all node types are `closed`, so only the keys below are
allowed):

- **`person`** — **required** `relation` (their relationship to the user, e.g.
  "sister", "manager"); optional `contact_notes`.
- **`preference`** — **required** `strength`: `hard` (never override) or `soft`
  (a default); optional `domain` (the area it applies to, e.g. "food").
- **`commitment`** — **required** `cadence` (`once`, or a human-readable
  recurrence like "every Friday") and `next_due` (a date); optional `channel`.
- **`project_ref`** — **required** `location` (where it lives — a URL or path);
  optional `kind`.
- **`note`** — no attributes; put the knowledge in the body.

If a required attribute isn't known yet, you don't have enough to write that
type — capture it as a `note`, or ask.

## Edges — how memories connect

| Edge | Meaning | Draw it from → to |
|------|---------|-------------------|
| `involves` | This memory concerns a person | any → `person` |
| `references` | This memory points at a project/resource | any → `project_ref` |
| `relates_to` | A weak generic association (bidirectional) | any → any |
| `supersedes` | This node replaces an older one | any → any |
| `same_topic` | Same topic, distinct content — attached automatically when the engine keeps both of two near-duplicates | any → any (automatic; don't draw it yourself) |

`relates_to` is also what the duplicate-check handshake attaches when you resolve
a possible duplicate as *distinct*. `supersedes` is attached for you by the
`supersede` tool — use `supersede` (not a plain `write`) when a memory *changes*,
per the contradiction contract in
[skills/writing-and-dedup.md](../../skills/writing-and-dedup.md).

## At session start

`briefing` here returns, in order: **due commitments** (those due within ~3
days), a **relevant** semantic section over preferences, notes, and people
(pass a `hint` describing the topic, or it comes back empty), and **recent
notes** (last ~7 days). The footer reports the count of aged pending captures.

## Scope

New spaces start with your personal scope (`personal-<you>`) as the only writable
place; the pack ships node *types*, not scopes. Create more with `scope_create`
as your work areas grow. See
[skills/scopes-and-visibility.md](../../skills/scopes-and-visibility.md).
