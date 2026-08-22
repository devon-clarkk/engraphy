"""Question -> retrieval (design/09 §Retrieval).

A strategy is **fixed for a whole run and named in the manifest**. It is never
selected per question, per category, or per suite — that would be tuning to the
answer key, and it is the specific thing the "claimed vs observed" controversy
is about.

Three strategies ship, each existing to measure something different:

* `SearchOnly` — the baseline every vendor reports.
* `SearchThenTraverse` — search supplies the seed id, traverse walks from it.
  This is the only way graph structure reaches the answer, because `traverse`
  needs a `start_id` and a question is not one.
* `BriefingThenSearch` — the token story. Worth stating plainly, because it is
  easy to get wrong: **`search()` applies no relevance floor.** The floor is a
  briefing-only feature. So "the relevance floor wins on tokens" is a claim
  about briefing, and it is only observable through a briefing-based strategy.

What we must not do is add a floor to `search()` to improve the numbers. That is
precisely the non-shipping tuning design/09 §Neutrality forbids: it would
produce a figure no real deployment could reproduce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from engraphy.core import briefing as briefing_core
from engraphy.core import search as search_core, traverse as traverse_core
from engraphy.server import db

from bench.core.corpus import Question
from bench.core.meter import Meter

__all__ = [
    "Retrieval",
    "RetrievalStrategy",
    "SearchOnly",
    "SearchThenTraverse",
    "BriefingThenSearch",
    "STRATEGIES",
]

SOURCE_CLIENT = "engraphy-bench"


@dataclass
class Retrieval:
    """What the reader will be given, and what it cost to get."""

    envelope: dict
    strategy: str
    meter: Meter = field(default_factory=Meter)


@runtime_checkable
class RetrievalStrategy(Protocol):
    name: str

    async def retrieve(self, pool, run_space, question: Question, meter: Meter) -> Retrieval: ...


class SearchOnly:
    """Hybrid search at shipped defaults. The baseline."""

    name = "search_only"

    def __init__(self, limit: int = 10, detail: str = "full") -> None:
        self.limit = limit
        self.detail = detail

    async def retrieve(self, pool, run_space, question: Question, meter: Meter) -> Retrieval:
        scope = run_space.scope_for(question.haystack_id)
        # Embedding happens inside search_core, outside any transaction -- the
        # same path the MCP dispatcher takes.
        with meter.stage("search"):
            env = await search_core.search(
                pool, run_space.space_id, run_space.principal, scope, question.text,
                SOURCE_CLIENT, limit=self.limit, detail=self.detail,
            )
        return Retrieval(envelope=env, strategy=self.name, meter=meter)


class SearchThenTraverse:
    """Search to seed, then walk the graph from every seed hit.

    Traverse needs a `start_id`; a question isn't one. This composition is how
    written edges reach the answer at all, which is also why it scores no better
    than `SearchOnly` under the verbatim extractor -- that extractor writes no
    edges, so there is nothing to walk. That contrast is a designed output, not
    a disappointment.

    **Width-matched to `SearchOnly` by construction (2026-07-22).** The seed
    search uses the *same* `limit` and `detail` as the shipped `SearchOnly`
    baseline (10, full), and every seed is walked rather than only the top few.
    So `search_only` and `search_then_traverse` over the same questions differ
    in exactly one thing: whether the graph neighbours of the search hits are
    added. The earlier version seeded at limit=5 and walked only the top 3,
    which confounded "does the graph help" with "does a narrower search hurt" --
    a difference that is not the graph's, and that made the multi-hop regression
    on the first run uninterpretable. Matching the widths isolates the graph
    contribution, which is the whole point of running the arm.

    What is deliberately **not** changed, because changing it would be tuning:
    `traverse` keeps its shipped `detail="summary"` (no bodies). design/09 is
    explicit that overriding it for a better score is out of bounds -- it is the
    engine's own answer to a 50-node walk of long bodies, and the graph
    contribution must be measured as the engine would actually deliver it.
    """

    name = "search_then_traverse"

    def __init__(self, seed_limit: int = 10, max_depth: int = 2, walk_seeds: int | None = None) -> None:
        # seed_limit defaults to SearchOnly's shipped limit so the comparison is
        # width-matched without a second parameter to keep in sync.
        self.seed_limit = seed_limit
        self.max_depth = max_depth
        # None => walk every seed. A cap is available for cost control but is
        # not used by default, because walking only the top-k reintroduces the
        # very width confound this arm exists to remove.
        self.walk_seeds = walk_seeds

    async def retrieve(self, pool, run_space, question: Question, meter: Meter) -> Retrieval:
        scope = run_space.scope_for(question.haystack_id)
        with meter.stage("search"):
            seed = await search_core.search(
                pool, run_space.space_id, run_space.principal, scope, question.text,
                SOURCE_CLIENT, limit=self.seed_limit, detail="full",
            )
        walked: list[dict] = []
        seen: set[str] = {r["node"]["id"] for r in seed.get("results", [])}
        hits = seed.get("results", [])
        if self.walk_seeds is not None:
            hits = hits[: self.walk_seeds]
        for hit in hits:
            with meter.stage("traverse"):
                try:
                    t = await traverse_core.traverse(
                        pool, run_space.space_id, run_space.principal, hit["node"]["id"],
                        "both", max_depth=self.max_depth, detail="summary",
                    )
                except Exception:  # noqa: BLE001 -- an unwalkable seed is not fatal
                    continue
            for node in t.get("nodes", []):
                if node["id"] not in seen:
                    seen.add(node["id"])
                    walked.append(node)
        env = dict(seed)
        # Kept as its own key rather than merged into `results`: a reader (and an
        # auditor) should be able to see which memories came from the graph
        # rather than from the search ranking.
        env["traversed"] = walked
        return Retrieval(envelope=env, strategy=self.name, meter=meter)


class BriefingThenSearch:
    """Briefing first (the relevance floor and capped sections), then search to
    fill the remaining budget. The token-efficiency story."""

    name = "briefing_then_search"

    def __init__(self, limit: int = 5) -> None:
        self.limit = limit

    async def retrieve(self, pool, run_space, question: Question, meter: Meter) -> Retrieval:
        scope = run_space.scope_for(question.haystack_id)
        cfg = await _briefing_config(pool, run_space)
        with meter.stage("briefing"):
            brief = await briefing_core.briefing(
                pool, run_space.space_id, run_space.principal, scope, question.text,
                SOURCE_CLIENT, cfg,
            )
        with meter.stage("search"):
            found = await search_core.search(
                pool, run_space.space_id, run_space.principal, scope, question.text,
                SOURCE_CLIENT, limit=self.limit, detail="full",
            )
        return Retrieval(
            envelope={"briefing": brief, "search": found}, strategy=self.name, meter=meter
        )


async def _briefing_config(pool, run_space) -> dict:
    """The pack's own briefing fragment, read from `config` -- the same source
    the server's dispatcher uses, so the sections are the ones a deployment
    running this pack would get.

    `db.transaction` yields a *connection*, not a cursor (matching
    `engraphy.core.briefing`, which does `cur = conn.cursor()`). The first draft
    called `fetchone()` on the connection and would have crashed every
    `BriefingThenSearch` retrieval -- it was never caught because the strategy
    had never once been run. Fixed and pinned by `test_retrieve` +
    fixture validation.
    """
    async with db.transaction(pool, run_space.space_id, run_space.principal) as conn:
        cur = conn.cursor()
        await cur.execute(
            "SELECT value FROM config WHERE space_id = %s AND key = 'pack.briefing'",
            (run_space.space_id,),
        )
        row = await cur.fetchone()
    return row[0] if row and row[0] else {}


STRATEGIES = {s.name: s for s in (SearchOnly, SearchThenTraverse, BriefingThenSearch)}


async def probe_search(pool, run_space, scope_id: str, query: str, limit: int = 5) -> dict:
    """Scope-addressed search for the dupstream probes.

    dupstream has no `Question` objects -- its probes address a scope directly,
    so this bypasses the strategy protocol rather than inventing a fake question
    to satisfy it.
    """
    return await search_core.search(
        pool, run_space.space_id, run_space.principal, scope_id, query,
        SOURCE_CLIENT, limit=limit, detail="full",
    )
