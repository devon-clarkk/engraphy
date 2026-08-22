# Conversational pack — agent guide

The **conversational** pack is for an assistant that holds **personal memory
across many sessions of conversation**: the people in someone's life, events that
happened at particular times, the preferences they state, the things and
relationships that matter to them, and the decisions and opinions they express
over time.

This is its cheat sheet — the pack-specific complement to the generic
[skills/](../../skills/README.md) set, which covers *how* to use memory
(answering only from what retrieval returns, which read tool when, the
write/dedup/`supersede` contract, scopes and visibility). Read those first; this
page only names this pack's vocabulary and the conventions particular to it.

## Recommended inference stance: `grounded`

This pack recommends the **`grounded`** inference stance (see
[skills/answer-discipline.md](../../skills/answer-discipline.md)). Personal
memory is exactly the setting where a declared, sourced inference earns its keep:
knowing someone well means being able to say "most likely yes — you've told me
they collect classic children's books" rather than a flat "not in memory". So
when memory supports a reasonable inference but not the fact outright, offer it —
hedged, and citing the memory it rests on.

This is a recommendation, not a rule: a deployment may set `strict` instead, and
the hard invariant is unaffected either way — you never state as known fact
anything memory doesn't support, under any stance. The knob only decides whether
you may *offer a declared, sourced inference* or must decline instead.

## Node types — what to write, and when

| Situation | Write a… |
|-----------|----------|
| A person who matters across conversations (the anchor their facts and relationships attach to) | `person` |
| A non-person entity that recurs and gets related to — a place, organization, object, pet, product | `thing` |
| A discrete durable fact about a person or thing (or the user) | `fact` |
| Something that happened at a particular time | `event` |
| A stated like / dislike / default | `preference` |
| A choice someone made and expressed | `decision` |
| A view or judgement someone expressed | `opinion` |

Write **one fact per node**. A person's details are not one big `person` node —
they are a `person` anchor plus separate `fact` nodes linked to it, so each fact
can be recalled, deduped, and corrected on its own.

`decision` vs `opinion` vs `preference`: a **decision** is a choice ("we're going
with Postgres"); an **opinion** is a view or judgement ("I think Postgres is
great"); a **preference** is a standing like/dislike/default ("I prefer tea").
When in doubt between an opinion and a preference, ask whether it's a one-off
judgement (opinion) or a durable disposition (preference).

## Attributes

The attribute set follows standard personal-knowledge / contact schemas
(schema.org `Person`, `Event`, etc.), so the canonical fields are where you'd
expect. All types are `closed`, so only these keys are allowed; anything else
goes in the body, and open-ended facts about a person become linked `fact` nodes.

- **`person`** — all optional: `full_name`, `pronouns`, `relationship` (to the
  user — "sister", "manager"), `occupation` (their role), `location` (where they
  live), `birthday`, `email`, `phone`. Fill what you're told; leave the rest.
  The employer as an organization is an edge (`works_at` → a `thing`), not a
  field.
- **`thing`** — optional `kind` (what it is: "restaurant", "employer", "pet"),
  `location`, `url`.
- **`event`** — **required** `occurred_on` (the date it happened); optional
  `ended_on` (for multi-day events) and `location`. An undated happening isn't an
  event — write it as a `fact`. Who was there is an `involves` edge.
- **`fact`**, **`decision`**, **`opinion`** — optional `as_of` (the date the fact
  holds from, or the decision/opinion was made/expressed). Set it when you know
  it; it's how "as of when did the user believe this" stays answerable.
- **`preference`** — optional `strength` (`hard` = a firm rule, don't override;
  `soft` = a default), `sentiment` (`like` / `dislike`), and `domain` (the area,
  e.g. "food", "scheduling"). Set `strength: hard` when the user is emphatic.

**Restate attribute values in the body.** Attributes are filterable fields,
not searchable text: search covers a node's title and body only. Whatever
you record as an attribute — an occupation, a location, a date — say it in
the body too, in natural words, or it will not be findable by search. (The
engine will make attributes searchable directly in a later version; until
then the body restatement is the contract.)

## Edges — how memories connect

| Edge | Meaning | Draw it from → to |
|------|---------|-------------------|
| `knows` | An interpersonal relationship (bidirectional) | `person` → `person` |
| `works_at` | This person works at / is a member of this organization | `person` → `thing` |
| `involves` | This memory concerns, or was stated by, this person | any → `person` |
| `references` | This memory concerns this thing | any → `thing` |
| `relates_to` | A generic association, either direction (bidirectional) | any → any |
| `supersedes` | This node replaces an older one | any → any |
| `same_topic` | Same topic, distinct content — attached automatically when the engine keeps both of two near-duplicates | any → any (automatic; don't draw it yourself) |

Two conventions worth holding:

- **Attribution and aboutness share `involves`.** Link a `preference`,
  `opinion`, or `decision` to the `person` it belongs to with `involves`; link a
  `fact` or `event` to the `person` it concerns the same way. If a memory has no
  `involves` edge, treat it as **the user's own** — their preference, their
  decision, their fact. (`involves` does not, in v1, distinguish "held by" from
  "about"; when that matters, make it explicit in the body.)
- **Correct with `supersede`, not a second write.** Personal memory changes —
  people move, jobs change, opinions shift. When new information *replaces* an
  older memory, use `supersede` so the old one is retired and the new one becomes
  current. This is exactly the contradiction contract from
  [skills/writing-and-dedup.md](../../skills/writing-and-dedup.md): a plain
  re-write of a changed fact is likely to be *merged* into the stale one, leaving
  the old version current.

## At session start

`briefing` here returns, in order: **standing preferences** (the ones marked
`strength: hard`, so you honour them), a **relevant** semantic section over
people, facts, preferences, opinions, and decisions (**pass a `hint`** describing
the topic or it comes back empty), **recent events** (last ~30 days), and
**recent decisions** (last ~30 days). The footer reports aged pending captures.

## Scope

Personal memory is private by default — it lives in the user's own scope(s), and
the `personal` scope is ambient (always in play) when present. Don't write one
person's memory anywhere a different principal could read it unless the user has
deliberately shared that scope. See
[skills/scopes-and-visibility.md](../../skills/scopes-and-visibility.md).
