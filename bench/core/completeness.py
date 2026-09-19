"""Retrieval completeness: get every memory a question needs into the reader's context.

Two additions on top of plain hybrid search, each aimed at a measured failure
class of the typed conversational store, and each general: neither names a
benchmark, a category, or a question shape.

**Entity roster.** Top-k search ranks memories by similarity to the question's
wording. A set or synthesis question ("what activities does X do", "what has X
read") needs memories whose wording does not resemble the question at all, so
they rank below the cut. When a question names people or things the store
already knows, the roster adds every other active memory that mentions them,
title and attributes only, most query-similar first. The full bodies stay on the
top-k results; the roster is a compact index of everything else known about
those entities, so the reader sees the whole set at a bounded cost.

Membership is a TEXT filter: a memory belongs to an entity when its title or
body names it. It deliberately does not follow `involves` or any other edge.
Graph traversal was measured as a net loss on multi-hop (2026-08), and an edge
is only as complete as the extractor that attached it; the text is the ground
truth the edges were derived from.

Entities are recognised from the store's own registry, the titles of its
`person` and `thing` nodes, never from a list authored for a benchmark. A name
is the title up to its first comma or parenthesis ("Jon, aspiring dance studio
founder" is "Jon"), must begin with a capital letter, and matches the question
case-sensitively as a whole word. Only proper-noun entities qualify, so a
`thing` titled "pottery class" cannot turn every question about pottery into a
roster query.

**Source-turn backstop.** Typed extraction keeps the facts an extractor judges
durable and drops the rest; a detail it dropped can never be retrieved, however
wide the search. The backstop keeps the conversation's own turns as a second,
non-lossy layer (the shape Zep calls the episode subgraph) and adds the top
turns for the question, ranked by the engine's own hybrid arithmetic: a cosine
leg and a lexical leg, each capped at the engine's leg cap, fused by RRF at the
engine's k. A turn already quoted verbatim in a retrieved memory is skipped.

In this harness the turn layer is built from the corpus, because the engine does
not yet store turns. That is the one place the prototype reaches outside the
engine, and it is why shipping this needs an engine change
(design/analysis/retrieval-completeness.md).

The limits are fixed constants with stated reasons, set before measurement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from engraphy.core import embedding
from engraphy.core.dedup import _vector_literal
from engraphy.core.search import _LEG_CAP, rrf_fuse

# At most this many roster entries. A list a reader can scan in one pass; on a
# store of a few hundred memories it rarely binds, and on a large store it keeps
# the roster to a few thousand characters of titles. Not measured against any
# benchmark.
ENTITY_ROSTER_LIMIT = 100

# Source turns added per question: the shipped default search width, so the
# backstop never adds more items than a default search returns.
SOURCE_TURN_LIMIT = 10

_NAME_CUT = re.compile(r"[,(]")


def entity_names(titles: list[str]) -> list[str]:
    """Proper-noun names from person/thing titles, longest first, de-duplicated."""
    names: set[str] = set()
    for title in titles:
        name = _NAME_CUT.split(title or "", 1)[0].strip()
        if len(name) >= 2 and name[0].isupper():
            names.add(name)
    return sorted(names, key=lambda n: (-len(n), n))


def names_in(question: str, names: list[str]) -> list[str]:
    """The registry names a question mentions, as whole words, case-sensitively."""
    found = []
    for name in names:
        if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", question):
            found.append(name)
    return found


async def registry_names(cur, space_id: str, scope_set: list[str]) -> list[str]:
    await cur.execute(
        "SELECT title FROM nodes WHERE space_id = %s AND scope_id = ANY(%s) "
        "AND status = 'active' AND type IN ('person', 'thing')",
        (space_id, list(scope_set)),
    )
    return entity_names([t for (t,) in await cur.fetchall()])


async def entity_roster(cur, space_id: str, scope_set: list[str], names: list[str],
                        query_vec, exclude_ids: set[str],
                        limit: int = ENTITY_ROSTER_LIMIT) -> list[dict]:
    """Active memories naming any of `names` in title or body, excluding
    `exclude_ids`, most query-similar first, as summary node dicts (no body)."""
    if not names:
        return []
    pattern = "|".join(r"\m" + re.escape(n) + r"\M" for n in names)
    q = _vector_literal(query_vec)
    await cur.execute(
        "SELECT id, type, scope_id, title, attrs, author_principal, created_at, "
        "1 - (embedding <=> %s::vector) AS sim FROM nodes "
        "WHERE space_id = %s AND scope_id = ANY(%s) AND status = 'active' "
        "AND type NOT IN ('person', 'thing') "
        "AND (title ~* %s OR body ~* %s) AND NOT (id::text = ANY(%s)) "
        "ORDER BY embedding <=> %s::vector, id LIMIT %s",
        (q, space_id, list(scope_set), pattern, pattern, sorted(exclude_ids), q, limit),
    )
    out = []
    for nid, ntype, scope, title, attrs, author, created_at, sim in await cur.fetchall():
        attrs = {k: v for k, v in (attrs or {}).items() if k != "addenda"}
        out.append({"id": str(nid), "type": ntype, "scope": scope, "title": title,
                    "attrs": attrs, "author": author, "created_at": created_at.isoformat(),
                    "similarity": round(float(sim), 3)})
    return out


# ----------------------------------------------------------------- source turns
@dataclass(frozen=True)
class SourceTurn:
    turn_id: str
    speaker: str
    text: str
    when: str  # the session's date, as the corpus gives it

    @property
    def body(self) -> str:
        return f"{self.speaker}: {self.text}"


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(text).lower()).split())


class TurnIndex:
    """One conversation's turns, embedded once with the engine's document
    embedder, searched with the engine's hybrid arithmetic."""

    def __init__(self, turns: list[SourceTurn]) -> None:
        self.turns = turns
        self._vecs = None

    def _ensure_vectors(self):
        if self._vecs is None:
            import numpy as np
            self._vecs = np.asarray([embedding.embed_document(t.body) for t in self.turns],
                                    dtype="float32")
        return self._vecs

    async def search(self, cur, query: str, query_vec, retrieved_bodies: list[str],
                     limit: int = SOURCE_TURN_LIMIT) -> list[SourceTurn]:
        import numpy as np
        vecs = self._ensure_vectors()
        sims = vecs @ np.asarray(query_vec, dtype="float32")
        vector_ids = [str(i) for i in np.argsort(-sims, kind="stable")[:_LEG_CAP]]
        # The lexical leg runs the engine's own tsquery/ts_rank_cd functions over the
        # turn texts in-session; nothing is written.
        await cur.execute(
            "SELECT i - 1 FROM unnest(%s::text[]) WITH ORDINALITY AS u(t, i), "
            "websearch_to_tsquery('english', %s) q "
            "WHERE to_tsvector('english', t) @@ q "
            "ORDER BY ts_rank_cd(to_tsvector('english', t), q) DESC, i LIMIT %s",
            ([t.body for t in self.turns], query, _LEG_CAP),
        )
        lexical_ids = [str(i) for (i,) in await cur.fetchall()]
        fused = rrf_fuse(vector_ids, lexical_ids)
        quoted = [_norm(b) for b in retrieved_bodies]
        out = []
        for sid, _score in fused:
            turn = self.turns[int(sid)]
            key = _norm(turn.text)[:60]
            if key and any(key in b for b in quoted):
                continue
            out.append(turn)
            if len(out) >= limit:
                break
        return out


def turn_indexes(corpus) -> dict[str, TurnIndex]:
    """haystack_id -> TurnIndex, from the corpus's sessions."""
    out = {}
    for hs in corpus.haystacks:
        turns = []
        for session in hs.sessions:
            when = session.timestamp or ""
            for t in session.turns:
                if t.text:
                    turns.append(SourceTurn(t.turn_id or "", t.speaker, t.text, when))
        out[hs.haystack_id] = TurnIndex(turns)
    return out


def turn_dicts(turns: list[SourceTurn]) -> list[dict]:
    return [{"turn_id": t.turn_id, "speaker": t.speaker, "text": t.text, "when": t.when}
            for t in turns]


def render_roster(roster: list[dict], start: int) -> str:
    from bench.core.answer import _attrs_str, _readable_date
    lines = []
    for i, node in enumerate(roster, start=start):
        bits = [f"[{i}]"]
        if node.get("type"):
            bits.append(f"[{node['type']}]")
        astr = _attrs_str(node.get("attrs") or {})
        if astr:
            bits.append(f"({astr})")
        bits.append((node.get("title") or "").strip())
        rec = _readable_date(node.get("created_at")) if node.get("created_at") else ""
        if rec:
            bits.append(f"(recorded {rec})")
        lines.append(" ".join(bits))
    return "\n".join(lines)


def render_turns(turns: list[dict], start: int) -> str:
    return "\n".join(
        f"[{i}] {t['speaker']}" + (f" ({t['when']})" if t.get("when") else "") + f": {t['text']}"
        for i, t in enumerate(turns, start=start))

