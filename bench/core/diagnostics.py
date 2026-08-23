"""Per-question failure observability (added 2026-07-23).

Diagnostics only: nothing here changes config, retrieval, or scoring. It records,
for every graded question, enough to see *where* a miss happened rather than only
that it happened -- and it buckets each incorrect answer into a failure mode, so
"what was in Engraphy when it failed" is answerable from the durable record.

The three failure modes a wrong answer can be in, and the cheap heuristic that
tells them apart:

* **extraction-miss** — the supporting fact was never stored. The gold answer's
  content words are not found in the retrieved context *or* in a direct probe of
  the whole scope.
* **retrieval-miss** — the fact is in the store (a scope probe finds it) but this
  question's retrieval did not surface it.
* **reader-miss** — the fact WAS in the retrieved context, and the reader still
  answered wrong.

The gold-support check is a keyword-overlap heuristic, **not ground truth**: it
asks whether enough of the gold answer's content words appear in some body text.
It says nothing for a gold answer with no content words (a bare number or date),
which is labelled `unattributed` rather than guessed at.
"""

from __future__ import annotations

import re

__all__ = [
    "BODY_CHARS",
    "MAX_CONTEXT_NODES",
    "attribute_failure",
    "compact_context",
    "context_text",
    "gold_support",
]

MAX_CONTEXT_NODES = 12
BODY_CHARS = 700

# Deliberately small: only words that carry no discriminating signal. Kept short
# because an over-eager stop list would make the heuristic miss real support.
_STOP = {
    "the", "and", "that", "this", "with", "from", "have", "has", "had", "was",
    "were", "will", "would", "they", "them", "their", "there", "then", "than",
    "what", "when", "where", "which", "who", "whom", "into", "about", "also",
    "been", "being", "does", "did", "for", "are", "her", "his", "she", "him",
    "our", "out", "not", "but", "you", "your", "some", "more", "over", "after",
}


def _content_words(text: str) -> set[str]:
    # Require at least one letter: a pure-digit token (a bare year like "2023",
    # a count) is exactly the case where keyword overlap is unreliable, so the
    # heuristic must abstain on numeric/date golds rather than match a lone
    # number that appears everywhere. `gpt4`-style tokens keep their letters and
    # still count.
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) >= 4 and w not in _STOP and any(ch.isalpha() for ch in w)}


def compact_context(envelope: dict, *, max_nodes: int = MAX_CONTEXT_NODES,
                    body_chars: int = BODY_CHARS) -> list[dict]:
    """Flatten any retrieval envelope into the nodes the reader actually saw.

    Handles all three shipped envelope shapes -- search_only, search_then_traverse
    (search results + `traversed`), and briefing_then_search (briefing sections +
    search). Deduplicated by node id, source-tagged so a reader of the failure
    log can see whether a node came from search, the graph walk, or a briefing
    section. Bodies are truncated (the full envelope is kept raw in the sidecar).
    """
    out: list[dict] = []
    seen: set = set()

    def add(node, score, source):
        if not isinstance(node, dict):
            return
        nid = node.get("id")
        if nid in seen:
            return
        seen.add(nid)
        out.append({
            "id": nid,
            "title": node.get("title"),
            "type": node.get("type"),
            "score": score,
            "source": source,
            "body": (node.get("body") or "")[:body_chars],
        })

    for r in envelope.get("results", []) or []:
        add(r.get("node"), r.get("score") or r.get("similarity"), "search")
    for n in envelope.get("traversed", []) or []:
        add(n, None, "traverse")
    briefing = envelope.get("briefing")
    if isinstance(briefing, dict):
        for sec in briefing.get("sections", []) or []:
            for n in sec.get("nodes", []) or []:
                add(n, None, f"briefing:{sec.get('name')}")
    search = envelope.get("search")
    if isinstance(search, dict):
        for r in search.get("results", []) or []:
            add(r.get("node"), r.get("score") or r.get("similarity"), "search")
    return out[:max_nodes]


def context_text(compact: list[dict]) -> str:
    """The title+body text that was in front of the reader, for the heuristic."""
    return "\n".join(f"{n.get('title') or ''} {n.get('body') or ''}" for n in compact)


def gold_support(gold: str, text: str) -> dict:
    """Heuristic: does `text` contain the gold answer's supporting words?

    Returns `supported` (bool or None), the overlap `fraction`, and the matched /
    missing content words so a human reading the failure log can judge the
    heuristic's call. `None` means the gold answer has no content words to check
    (a bare number or date) -- the heuristic abstains rather than guesses.
    """
    gw = _content_words(gold)
    if not gw:
        return {"supported": None, "fraction": None,
                "tell": "gold answer has no content words (numeric/short); heuristic n/a"}
    low = (text or "").lower()
    present = {w for w in gw if w in low}
    frac = len(present) / len(gw)
    return {
        "supported": frac >= 0.5,
        "fraction": round(frac, 3),
        "matched": sorted(present),
        "missing": sorted(gw - present),
    }


def attribute_failure(*, correct: bool, abstain_expected: bool,
                      gold_in_context: dict | None, gold_in_store: dict | None) -> str:
    """Bucket one graded question into a failure mode (heuristic).

    `correct` questions return `correct`. Adversarial (abstain-expected) wrong
    answers are `reader-over-answered` -- the memory did not contain the answer
    and the reader should have declined but produced one. Everything else is the
    three-way split above.
    """
    if correct:
        return "correct"
    if abstain_expected:
        return "reader-over-answered"
    gic = (gold_in_context or {}).get("supported")
    gis = (gold_in_store or {}).get("supported")
    if gic is True:
        return "reader-miss"
    if gic is False and gis is True:
        return "retrieval-miss"
    if gic is False and gis is False:
        return "extraction-miss"
    return "unattributed"  # heuristic could not tell (e.g. numeric gold)
