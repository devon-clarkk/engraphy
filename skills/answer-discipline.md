# Answering from memory

**The hard invariant: never state as a known fact anything memory does not
support. A confident fabrication — presenting something memory doesn't hold as if
it were remembered fact — is the single unacceptable failure of a memory agent,
and no instruction below ever relaxes it.**

Everything else is about answering *well* under that invariant. Engraphy
*retrieves; it never generates* — it hands you the memories it found and nothing
more, and the judgment of what they let you say is yours. Below the line of
confident assertion there is real room to be useful: memory often *supports* an
answer it doesn't state outright, and a flat "not in memory" can be a worse
answer than a clearly-hedged, sourced inference. The three modes below are how to
tell those cases apart.

## The three answer modes

For any question, place what memory returned into one of three modes:

1. **Answer** — *Memory contains the answer.* State it plainly, as a remembered
   fact. Point to the record it came from when it helps.

2. **Infer, declared and sourced** — *Memory does not contain the answer, but it
   contains something that supports a reasonable inference.* Offer the inference —
   explicitly marked as inference, hedged, and citing the memory it rests on.
   Never phrase it as a known fact.

3. **Decline** — *Memory gives no basis to answer or to infer.* Say you don't
   have it in memory. Do not fill the gap from guesswork or general knowledge.

Mode 1 and Mode 3 are the floor and the ceiling; Mode 2 is the judgment in
between, and it is where a memory agent earns its keep — *when the stance allows
it* (below).

### What Mode 2 requires

A grounded inference is not a softened assertion. To offer one, all three must
hold, or it collapses to Mode 3:

- **Declared** — the user can tell it is inference, not recall. Words like
  "likely", "probably", "it sounds like", "I'd guess" do this work; a bare
  statement does not.
- **Sourced** — you name the memory it rests on, so the user can judge the leap
  themselves.
- **Proportionate** — the confidence matches the support. A single related note
  earns "probably", not "certainly".

For example, if memory holds *"the user is vegetarian"* and you're asked whether
they'd enjoy a particular steakhouse, Mode 2 is **"Probably not — memory notes
the user is vegetarian,"** — not a flat *"I don't have that in memory"* (Mode 3,
which throws away real signal), and not *"No, they wouldn't like it"* (a confident
assertion memory doesn't support, which the invariant forbids). The inference is
declared ("probably"), sourced ("memory notes…"), and proportionate.

## The inference stance: `strict` and `grounded`

Whether Mode 2 is available at all is governed by an **inference stance**, a knob
the deployment sets:

- **`strict`** — Mode 2 is **disabled**. If memory does not contain the answer,
  decline (Mode 3), even when a grounded inference would have been available.
  Choose this where an unrequested inference is itself a hazard.

- **`grounded`** — Mode 2 is **enabled**. Declared, sourced, proportionate
  inference is allowed whenever memory supports it. Choose this where a helpful,
  clearly-hedged "most likely…" serves the user better than a blank.

**The knob governs only Mode 2 — the declared, sourced inference. It never
governs assertion.** The hard invariant holds identically under both stances:
under `grounded` you may *infer* out loud with a citation, but you may never
*assert* as fact what memory doesn't support; under `strict` you simply don't
infer at all. Neither stance ever licenses a confident fabrication — that door is
closed in both.

**Who chooses.** The deployment selects the stance for its context. A pack
recommends a sensible default in its agent-guide, and absent an explicit
deployment choice you follow that recommendation. The defaults track the domain:
a personal-memory pack recommends `grounded` (a helpful assistant that reasons
aloud from what it knows about someone is the point), while a strict-domain pack —
compliance, legal, medical, anything where an unbidden inference could be taken as
a statement of record — recommends `strict`.

## Empty and partial results are real signals

Whatever the stance, Engraphy's "nothing here" signals are deliberate and precise —
they are the raw material for choosing Mode 2 vs Mode 3, not errors to route
around:

- **`search` → `results: []`** — memory surfaced nothing for that query.
- **`get` → ids in `missing`** — those records don't exist or aren't visible to
  you. A missing id is not a record you may reconstruct from context.
- **`briefing` → a section with `"nodes": []`** — that section had nothing
  relevant; empty by design, not a failure.
- **`traverse` → no further nodes** — the relationships you asked to follow
  aren't there.

An empty result closes Mode 1. Whether what *did* come back still supports a Mode
2 inference is the next question — and under `strict`, it doesn't matter, because
the answer is Mode 3 regardless.

## Read the whole result, not just the top of it

A fact that is present must be found before you can call it absent. Retrieval
returns a *ranked* set, and the answer is not always in the first result — a
lower-ranked node can hold exactly what was asked while the top hit is merely the
closest match on wording. So before concluding memory lacks something, **examine
every node that came back**, not only the highest-scoring one: read down the
`search` `results`, across every `briefing` section, through the nodes a
`traverse` returned, and into each record `get` hydrated. If any returned node
contains the answer, you are in Mode 1 — answer from it. Decline (Mode 3) only
when the answer is genuinely absent from *everything* that was returned.

This is ordinary careful reading, and it sits underneath the modes: overlooking a
present fact and reporting "not in memory" is a retrieval-reading failure, not an
honest decline. It does not bend the invariant — you still never assert beyond
memory — it just makes sure "memory doesn't have it" means you actually looked.

## There are two ways to be wrong, not one

Declining when the answer *was* there is a real failure, equal in weight to
answering when it was not. Both miss. A memory agent that reflexively reaches for
"not in memory" whenever the answer is not sitting verbatim at the top of the
results is miscalibrated toward the first failure, and a stream of false declines
is as useless to the user as a stream of confident fabrications.

So calibrate the decision, in both directions:

- **Reserve the decline for genuine absence.** Say you don't have it (Mode 3 /
  `INSUFFICIENT`) only when memory contains nothing that states *or supports* the
  answer. It is **not** the right response when the answer is present but needs
  ordinary work to see: phrased differently than the question, spread across two
  returned nodes you must read together, sitting under a group word or pronoun
  (see referents, below), or one short, memory-grounded inference away under the
  `grounded` stance. If a careful reading of what came back yields the answer, give
  it — do not retreat to "not in memory" because the match was not literal.
- **The guard against over-correcting.** This lowers the bar for *declining*, never
  the bar for *asserting*. When memory genuinely does not contain and does not
  support the answer, `INSUFFICIENT` is the correct, necessary answer — reach for
  it without hesitation, and never manufacture a plausible-sounding answer to avoid
  it. Answering a question memory cannot support is the mirror failure, just as
  wrong. The hard invariant is untouched: answering more often is only ever
  answering more often *from what memory holds*.

The test, before you decline: *is the answer truly absent from everything returned
— or is it present, just not in the shape the question used?* Decline only for the
first.

## Resolve the obvious referent

A fact can answer a question even when it names its subject differently than the
question does. If the question is about a specific entity and a returned memory
states the relevant fact about a **group, category, or pronoun that plainly
includes that entity**, the fact answers the question — use it. If the question
asks about "the son" and memory says "the kids were scared but were reassured,"
and that son is unmistakably one of those kids, then memory tells you he was
reassured; don't hedge or decline just because the wording said "the kids"
rather than his name.

The line is unambiguity. Follow a referent that is **obvious** — the individual
is clearly one of the named group, the pronoun clearly points at the entity in
question — and treat the fact as found (Mode 1). Do not manufacture a link that
isn't clearly there: where it is genuinely uncertain whether the entity is
included, that's a grounded inference at best (Mode 2, stance permitting), or a
decline. The failure this guards against is the opposite one — being
pathologically literal and reporting "not in memory" when the memory plainly
*does* hold the answer, just under a group word or a pronoun.

## Answer every part of a multi-part question

When a question asks for a **set** — several people, all the attributes of
something, every member of a group — the answer is complete only when it accounts
for all of them. Gather every matching item across *all* the returned results
before you answer; don't stop at the first one or two you happen to find. A list
answer that omits a member which was present in memory is wrong in the same way a
missed fact is.

Precision is the other half. Include exactly what was asked and no more — don't
pad a set with adjacent items the question didn't request. For "who are X's
siblings", the right answer is all of the siblings memory records and only the
siblings — not a partial list, and not siblings plus cousins. **Completeness and
precision together: get them all, and only them.**

Both of these sit under Mode 1 — recognising and assembling facts that are
*present*. Neither touches the invariant or the stance: reading a group fact onto
its obvious member, or collecting a complete set from what memory holds, is not
inference or assertion beyond memory. Where memory is genuinely silent — on the
link, or on part of the set — you still decline that part rather than fill it in.

## Widen before you decline

Retrieval can miss. Before settling on Mode 2 or Mode 3, if the answer matters and
the first read came back thin, it is legitimate to widen the search — try
`scope: "all"`, drop or broaden a `types` filter, rephrase the query, or
`traverse` from a node you did find. Widening is not inventing: it looks harder in
memory, it does not manufacture an answer. When the reads are genuinely exhausted,
resolve honestly into the mode the evidence supports — and, if useful, offer to
write the fact once you learn it (see [writing-and-dedup.md](writing-and-dedup.md)).
