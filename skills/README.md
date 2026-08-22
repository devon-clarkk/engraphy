# Engraphy agent skills

The instructions a calling agent follows to use an Engraphy memory space correctly.
Engraphy is a typed knowledge graph with embedding-native deduplication, hybrid
retrieval, real graph traversal, and multi-principal visibility. These skills
describe the *protocol* an agent should follow on top of the tool surface — the
things the per-tool descriptions are too short to carry.

They are **product documentation, not configuration**. They describe shipped
behaviour that holds for every Engraphy deployment and every pack. The concrete
vocabulary of a given space — which node types and edge types exist, which
scopes you can write to — comes from the *applied pack* and is visible in the
connected server's tool list and error messages, not hard-coded here.

## The skills

| Skill | Covers |
|-------|--------|
| [answer-discipline.md](answer-discipline.md) | Answering from memory: use only what retrieval returned; say so plainly when the answer isn't there. |
| [retrieval.md](retrieval.md) | Which read tool for which job — `search`, `briefing`, `traverse`, `get` — and what each returns. |
| [writing-and-dedup.md](writing-and-dedup.md) | Writing memory, the dedup bands and the handshake, and the **contradiction contract**: when a merge means you must `supersede`. |
| [scopes-and-visibility.md](scopes-and-visibility.md) | Where memory lives, what you can and cannot see, and how to write to the right place. |
| [contracts-and-reserved-names.md](contracts-and-reserved-names.md) | Envelope and error conventions an agent can trip, and the reserved names (`engram_sentinel`, `attrs.addenda`) the engine owns. |

## The shape of a good session

A typical, well-behaved memory session is only a few calls:

1. **Start with `briefing`** (pass a `hint` describing the topic) to load
   session-start context.
2. **`search` when you need to recall** something specific; **`traverse`** to
   follow how memories relate; **`get`** to hydrate a full record.
3. **Answer only from what came back.** If it isn't in memory, say so — do not
   fill the gap from guesswork. See [answer-discipline.md](answer-discipline.md).
4. **`write` what is durably worth keeping.** Re-telling the engine something it
   already knows is safe — it is absorbed, not duplicated. When a write comes
   back `merged`, read the returned `instruction`: if you were *correcting* the
   stored fact rather than restating it, `supersede` it. See
   [writing-and-dedup.md](writing-and-dedup.md).

Engraphy **retrieves; it never generates**. Judgment — what a memory means,
whether it answers the question, whether a new fact overturns an old one —
belongs to you, the calling agent. These skills are how you hold up your half.
