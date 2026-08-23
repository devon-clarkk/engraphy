# End-to-End Tutorial: A Reading-Log App on Engraphy

This walks through building a small but real application on Engraphy: a **reading
log** that remembers books, their authors, and your notes — and, crucially, dedups
your memory as you go so re-telling is safe and genuinely-new thoughts are kept
distinct. It uses the `reading` pack from [Build your own pack](03-packs.md).

Prerequisites: a running server and the app installed, per [Setup](02-setup.md).
We assume the superuser URL is exported:

```bash
export ENGRAPHY_DATABASE_URL="postgres://postgres:devpw@127.0.0.1:5432/engraphy?sslmode=disable"
```

## 1. Provision the space, pack, and a token

```bash
# a space + its founding admin principal + a personal scope + restore sentinel
engraphy-admin space create --id reading --display-name "Reading log" --principal reader

# give it the ontology (the reading pack from doc 3)
engraphy-admin pack apply reading-pack.yaml --space reading

# a bearer token for our app/client
engraphy-admin token create --space reading --principal reader --client-name app --role readwrite
#   -> prints the token once; put it in your MCP client config
```

Everything from here is over the MCP tool surface, authenticated with that token.
We'll write into the founding principal's personal scope, `personal-reader`. (For a
shared library you'd `scope_create` a `team-write` scope instead.)

## 2. Confirm where we can write

```jsonc
// tool: scope_list   arguments: {}
// -> the token resolves and we can write to personal-reader
```

## 3. Store an author, then a book, and link them

Write the author first so we have its id to link to:

```jsonc
// tool: write
{"scope": "personal-reader", "type": "author",
 "title": "Ursula K. Le Guin",
 "body": "American author of speculative fiction; born in Berkeley, California.",
 "attrs": {"country": "USA"}}
// -> {"v":1,"outcome":"inserted","node":{"id":"AUTH-1","type":"author",...},"resonance":[]}
```

Now the book, linked to the author with a `by` edge in the same write (`links`
takes exactly one endpoint — the other is the new node):

```jsonc
// tool: write
{"scope": "personal-reader", "type": "book",
 "title": "The Left Hand of Darkness",
 "body": "A 1969 novel set on the planet Gethen, whose inhabitants are ambisexual.",
 "attrs": {"author": "Ursula K. Le Guin", "rating": "5"},
 "links": [{"type": "by", "dst_id": "AUTH-1"}]}
// -> {"v":1,"outcome":"inserted","node":{"id":"BOOK-1",...},"resonance":[...]}
```

Because `author` is a **string** attribute it is *searchable* — a later search for
"Le Guin" will find this book even though the name is only in `attrs` and the linked
author node (that's Engraphy's universal-searchability property at work). `rating`
is an enum, so it's stored for filtering but not searchable.

## 4. Add a note about the book

```jsonc
// tool: write
{"scope": "personal-reader", "type": "note",
 "title": "Gethenian ambisexuality",
 "body": "The novel uses Gethen's ambisexual biology to interrogate gender as a social construct.",
 "links": [{"type": "about", "dst_id": "BOOK-1"}]}
// -> {"v":1,"outcome":"inserted","node":{"id":"NOTE-1",...}}
```

## 5. Watch the dedup pipeline work

This is the part a naive key-value store can't do. Write a **re-worded note on the
same topic** — same idea, different wording:

```jsonc
// tool: write
{"scope": "personal-reader", "type": "note",
 "title": "Ambisexuality and gender",
 "body": "The book uses Gethen's ambisexual biology to examine gender as a social construct.",
 "links": [{"type": "about", "dst_id": "BOOK-1"}]}
// -> {"v":1,"outcome":"merged_linked","node":{"id":"NOTE-2",...},"similarity":0.96}
```

It banded as a near-duplicate (≈0.96) but its **wording is distinct**, so Engraphy
kept it as its **own** node `NOTE-2` and joined it to `NOTE-1` with a `same_topic`
edge — nothing was absorbed, nothing was silently swallowed. You now have two nodes
on one topic, discoverable together. This is the non-destructive dedup guarantee.

Now write a **near-identical restatement** — same words, trivially rephrased:

```jsonc
// tool: write
{"scope": "personal-reader", "type": "note",
 "title": "Gender as social construct",
 "body": "This novel uses the ambisexual biology of Gethen to interrogate gender as a social construct.",
 "links": [{"type": "about", "dst_id": "BOOK-1"}]}
// -> {"v":1,"outcome":"merged","canonical":{"id":"NOTE-1",...},"similarity":0.99,
//     "instruction":"…if you were correcting the fact, call supersede…"}
```

This time it **absorbed** into `NOTE-1` (the envelope's `canonical.id`) — a
restatement with near-identical tokens, so no new node. Re-telling is safe.

> Which outcome you get depends on how similar and how novel the text is: a
> near-identical restatement **`merged`** (absorbs), a re-worded same-topic note
> **`merged_linked`** (kept + linked), and a related-but-less-similar write lands in
> the pending band and returns **`needs_confirmation`** for you to resolve with
> `resolve_duplicate`. A genuinely *different* fact — say a note about the book's
> winter setting — simply comes back **`inserted`** as its own node. What you never
> get for a near-duplicate is a blind second copy.
>
> And if a write lands as `merged` but you were *correcting* the stored fact rather
> than restating it, call `supersede` — auto-merge can't tell a correction from a
> restatement.

## 6. Query it back

**Semantic search** — a paraphrase, not the stored words:

```jsonc
// tool: search
{"scope": "personal-reader", "query": "how does the book handle sex and society"}
// -> results include NOTE-1 (ambisexuality/gender) ranked by fused RRF score
```

**Search that hits the attribute surface** — the author's name lives in `attrs`:

```jsonc
// tool: search
{"scope": "personal-reader", "query": "Le Guin"}
// -> finds BOOK-1 and AUTH-1
```

**Graph walk** — everything attached to the book, one hop:

```jsonc
// tool: traverse
{"start_id": "BOOK-1", "direction": "both", "max_depth": 1}
// -> nodes: AUTH-1 (via `by`), NOTE-1 and NOTE-2 (via `about`)
//    edges: [{src:BOOK-1,dst:AUTH-1,type:by}, {src:NOTE-1,dst:BOOK-1,type:about}, …]
```

**See the whole topic cluster** — from either note, walk `same_topic`:

```jsonc
// tool: traverse
{"start_id": "NOTE-2", "direction": "both", "max_depth": 1, "edge_types": ["same_topic"]}
// -> NOTE-1 (its merge-linked sibling)
```

**Full detail** — pull complete bodies + edge summaries:

```jsonc
// tool: get
{"ids": ["BOOK-1", "NOTE-1"]}
// -> {"v":1,"nodes":[{...,"edges":{"out":[…],"in":[…]}}],"missing":[]}
```

**Session-start briefing** — the pack's declared sections:

```jsonc
// tool: briefing
{"scope": "personal-reader", "hint": "Le Guin gender themes"}
// -> {"v":1,"sections":[
//      {"name":"recent_notes","nodes":[NOTE-1, NOTE-2]},
//      {"name":"relevant","nodes":[BOOK-1, NOTE-1]}   // semantic, seeded by hint
//    ],"footer":{"inbox_pending":0}}
```

## 7. What just happened (and why it matters)

In a flat store, steps 4–5 would have produced three separate note rows, one of them
a duplicate, with no relationship to the book or each other, and search would only
match on stored keywords. On Engraphy:

- **The pack** made `book`/`author`/`note` first-class types with validated attrs
  and rule-checked edges — enforced in the database.
- **The write path** merged the restatement, kept the genuinely-new note as a
  linked-but-distinct member, and never silently discarded anything.
- **Retrieval** matched paraphrases (semantic leg), found a fact that lived only in
  an attribute (universal searchability), and let you walk from a book to its
  author and every note about it (the graph) — including the same-topic cluster.

That's the whole value proposition: memory that stays *correct* (dedup + supersede),
*findable* (hybrid + universal searchability), and *connected* (the typed graph), on
infrastructure you run yourself.

## Where to go next

- Tune dedup strictness per space with `engraphy-admin config set --key dedup.t_high`
  ([packs doc](03-packs.md#dedup-thresholds-are-per-space-config-not-pack-keys)).
- Bulk-load an existing dataset with `engraphy-admin import <file.jsonl>` — it runs
  through this same dedup pipeline and is idempotent.
- Harden for production with the [Deployment guide](05-deployment.md).
