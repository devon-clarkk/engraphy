# Wire golden fixtures (design/07 §Canonical tool I/O + §Error codes)

Canonical request/response JSON per tool per outcome. The response envelopes are
**byte-exact** to design/07's examples except for the fields each file lists under
`volatile` — server-minted ids and timestamps that cannot be fixed in a golden
file. The E2 tool-test harness compares the response to `response` after replacing
every `volatile` path with a type-check ("any lowercase UUID", "any RFC-3339 UTC
seconds-precision timestamp", "any non-negative integer"); everything not listed is
matched literally.

`volatile` paths use dotted names with `[]` for "every element of this array"
(e.g. `response.results[].node.id`).

Files:
- write_inserted / write_merged / write_needs_confirmation — the three write bands
- search_full — hybrid + RRF result envelope (detail: full)
- traverse_summary — walk envelope (detail: summary = node envelope without body)
- get — full nodes + addenda + edges, with a `missing` id
- briefing — sectioned envelope (section order = pack order; empty sections kept)
- resolve_duplicate_merged / resolve_duplicate_distinct_inserted — the handshake's
  second half returns the write envelope of the final outcome
- errors — the `ENGRAPHY_<CODE>: <sentence>` contract, one case per code

NEEDS_CONFIRMATION is a normal result, not an error (see write_needs_confirmation.json).
