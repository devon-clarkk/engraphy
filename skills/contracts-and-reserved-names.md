# Contracts and reserved names

The conventions every Engraphy response and request follows, and the handful of
rules a well-behaved agent can still trip over. None of this is pack-specific.

## Envelope conventions

- **Every response carries `"v": 1`.** It's the envelope version.
- **Optional fields are omitted when absent, never sent as `null`.** If a field
  isn't in the response, it doesn't apply — don't read `null` into it.
- **On input, an explicit `null` is rejected.** To leave an optional argument
  unset, *omit the key* — do not pass `null` for it.
- **Only declared arguments are accepted.** Passing an argument name a tool
  doesn't define is a validation error, not a silently-ignored extra. Send
  exactly the fields the tool declares.
- **Timestamps** are UTC, seconds precision; **ids** are lowercase UUIDs.

## Errors are written to be acted on

Errors come back as `ENGRAPHY_<CODE>: <sentence>`, and the sentence names the field
and the rule so you can correct and retry. The codes you may encounter:

| Code | Meaning | What to do |
|------|---------|-----------|
| `VALIDATION` | An argument broke a rule (bad type, missing required field, reserved name, unknown argument). | Fix the named field and retry. |
| `NOT_FOUND` | An id doesn't exist **or** you can't see it (deliberately indistinguishable). | Treat as unavailable; don't retry blindly. |
| `SCOPE_UNKNOWN` | A scope doesn't exist, or you can't read/write it. | Use `scope_list`; write to a scope you own. |
| `EDGE_RULE` | An edge's `(type, src, dst)` isn't allowed by the pack. | Use an edge type whose rule permits those node types. |
| `ROLE` | A read-only token tried to write, or a non-admin called an admin tool. | Expected without the needed role; don't retry as-is. |
| `RATE_LIMITED` | You exceeded the per-token rate window. | Back off; the message includes `retry_after_ms`. |
| `PENDING_EXPIRED` | You resolved a pending write after its TTL. | Just `write` again. |
| `SUPERSEDE_CONFLICT` | A `supersede`'s replacement collided with a *third* node. | Resolve that duplicate, then retry the supersede. |
| `INTERNAL` | An unexpected server-side fault. | Not your input; retry later or report it. |

**`needs_confirmation` is not in this table on purpose.** It is a normal `write`
result (a possible duplicate to resolve), never an error — see
[writing-and-dedup.md](writing-and-dedup.md).

Rate limits are per token, roughly 60 reads and 30 writes per minute — generous
for real use, a brake on a runaway loop. Batch reads (e.g. `get` up to 25 ids at
once, `traverse` instead of many `get`s) rather than hammering single calls.

## Reserved names the engine owns

Two names are reserved for the engine. A correct client never needs them; using
them is rejected with `VALIDATION`.

- **The node type `engraphy_sentinel` is engine-only.** It is a marker the engine
  mints for its own internal bookkeeping. You may not `write`, `supersede`,
  promote, or import a node of this type — every write path refuses it. It will
  not appear as an available type in the tool surface; don't try to create it.
- **The attrs key `addenda` is engine-managed.** `addenda` holds a node's merge
  history (the record of re-tellings folded into it). The engine writes and owns
  it; you may not set `attrs.addenda` on a `write`, `update`, or import — doing so
  is rejected. This is also why merge history is surfaced only by `get` (as its
  top-level `addenda` array) and is stripped from the `attrs` of every other
  tool's output: if you round-trip a node's `attrs` from `get` back into
  `update`, you don't need to carry `addenda` — the engine preserves it for you.

## The contract is stable; the vocabulary is the pack's

These tool names, argument shapes, envelopes, and rules are a stable contract —
they hold across pack changes and deployments. What varies per space is the
*vocabulary*: which node types, edge types, and scopes exist. Learn the
vocabulary from the connected server — its tool list and its error messages name
what's valid — rather than assuming a fixed set. Tool *descriptions* may also be
tailored per deployment, so read the descriptions the server actually serves you;
they, plus these skills, are your instructions.
