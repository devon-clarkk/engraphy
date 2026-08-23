"""Ingest: node drafts -> the real Engraphy write path (design/09 §Ingest).

Everything here goes through `engraphy.core.dedup.write` with the shipped bands,
the shipped advisory lock, the real attr-spec trigger and real RLS. There is no
bypass, and three temptations are refused explicitly:

* **No `import_mode=True`.** It is the obvious-looking bulk flag and it is wrong
  for a benchmark: in import mode a confirm-band write goes to a review-queue
  CSV instead of being parked, so the fact never lands in the store at all. That
  is a systematic recall loss concentrated on exactly the paraphrase-heavy and
  update-heavy questions the suites score, and it would look like weak retrieval
  rather than like absent data.
* **No `thresholds` / `resonance_floor` override.** Both are reachable from here
  (they are test surface, not wire surface) and neither may be used: a run that
  moves the bands is not measuring the shipped product.
* **Embeddings are computed outside the transaction**, matching
  `engraphy/server/tools/write.py` and the AST guard in CI.

The confirm band is the crux. Similarity in [0.80, 0.95) parks the payload and
stores *nothing* until an agent resolves it, and a conversational corpus hits
that band constantly. `ConfirmPolicy` is where that decision lives; it is named
in the run manifest, and the confirm-band hit rate is a first-class reported
number under every policy.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from engraphy.core import dedup, embedding

from bench.core.corpus import Haystack
from bench.core.extract import EdgeDraft, ExtractResult, Extractor, NodeDraft, window_sessions
from bench.core.space import RunSpace

__all__ = [
    "SOURCE_CLIENT",
    "AlwaysDistinct",
    "ConfirmDecision",
    "ConfirmPolicy",
    "IngestStats",
    "LLMAdjudicate",
    "ingest_haystack",
]

SOURCE_CLIENT = "engraphy-bench"

Resolution = Literal["distinct", "merge"]


@dataclass(frozen=True, slots=True)
class ConfirmDecision:
    resolution: Resolution
    merge_into: str | None = None
    # Free-text rationale from an adjudicating policy, recorded per decision so
    # a surprising dedup result can be audited rather than guessed at.
    reason: str = ""

    def __post_init__(self) -> None:
        if self.resolution == "merge" and not self.merge_into:
            raise ValueError("a 'merge' decision must name merge_into")


@runtime_checkable
class ConfirmPolicy(Protocol):
    """How the harness answers a `needs_confirmation` envelope.

    Two implementations ship (ruled 2026-07-21, Devon -- QUESTIONS.md
    "bench-ingest"): `AlwaysDistinct` here, and `LLMAdjudicate` alongside the
    other LLM roles.

    **`AlwaysMerge` is deliberately absent and must not be added** -- not as a
    primary and not as a comparison arm. It is the one policy that can destroy
    facts (every paraphrase-band pair collapses, including pairs that are merely
    similar), and it would manufacture a dedup number by deleting the evidence
    that contradicts it. Implementing it at all would leave it one config flag
    away from a published number.
    """

    name: str

    def decide(self, envelope: dict, draft: NodeDraft) -> ConfirmDecision: ...


class AlwaysDistinct:
    """Every pending resolves `distinct`. The reproducible primary.

    Deterministic, no LLM, no cost, and it never destroys a fact: the store
    grows instead. In-band dedup never fires under this policy, which is the
    point -- paired against `LLMAdjudicate` it answers whether adjudicating the
    uncertain zone actually beats simply storing everything separately.

    Note this is not a no-op inside the engine: `resolve_duplicate("distinct")`
    re-enters the banding core, so a pending that has since acquired a >= t_high
    twin still merges, and the engine auto-attaches a `relates_to` edge to the
    nearest still-active candidate.
    """

    name = "always_distinct"

    def decide(self, envelope: dict, draft: NodeDraft) -> ConfirmDecision:
        return ConfirmDecision(resolution="distinct", reason="policy: always distinct")


_ADJUDICATE_SCHEMA = {
    "type": "object",
    "properties": {
        "resolution": {"type": "string", "enum": ["distinct", "merge"]},
        "merge_into": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["resolution", "reason"],
    "additionalProperties": False,
}


class LLMAdjudicate:
    """One Claude call per pending. The realistic arm.

    This is what a well-behaved agent actually does with a `needs_confirmation`
    envelope: read the candidates, decide, call `resolve_duplicate`. Paired
    against `AlwaysDistinct` it answers the question design/09 poses — does
    adjudicating the uncertain zone beat simply storing everything separately?

    Fails **closed to `distinct`**. An adjudication that errors, returns an
    unusable answer, or names a candidate that was not offered resolves distinct,
    because the two failure directions are not symmetric: a spurious distinct
    costs a duplicate row, a spurious merge destroys a fact. The fallback is
    counted so the reported dedup numbers can be read against how often the
    policy actually ran.
    """

    name = "llm_adjudicate"

    def __init__(self, client, *, prompt_name: str = "adjudicate.md", effort: str = "high") -> None:
        from bench.core.llm import load_prompt

        self.client = client
        self.prompt_name = prompt_name
        self.system = load_prompt(prompt_name)
        self.effort = effort
        self.fallbacks = 0
        self.decisions: list[dict] = []

    def decide(self, envelope: dict, draft: NodeDraft) -> ConfirmDecision:
        from bench.core.llm import LLMError

        candidates = envelope.get("candidates") or []
        offered = {str(c.get("id")) for c in candidates if c.get("id")}
        try:
            resp = self.client.complete(
                self.system,
                _render_adjudication(draft, candidates),
                schema=_ADJUDICATE_SCHEMA,
                max_tokens=2000,
                effort=self.effort,
            )
            data = resp.data or {}
        except LLMError as exc:
            self.fallbacks += 1
            return ConfirmDecision("distinct", reason=f"adjudication failed ({exc}); fell back")

        resolution = data.get("resolution")
        reason = str(data.get("reason") or "")
        if resolution == "merge":
            target = str(data.get("merge_into") or "")
            if target not in offered:
                # A merge into something that was never a candidate is not a
                # decision we can act on -- and guessing the nearest candidate
                # would be inventing a fact-destroying merge on the model's
                # behalf.
                self.fallbacks += 1
                return ConfirmDecision(
                    "distinct",
                    reason=f"merge target {target!r} was not among the candidates; fell back",
                )
            self.decisions.append({"resolution": "merge", "into": target, "reason": reason})
            return ConfirmDecision("merge", merge_into=target, reason=reason)

        self.decisions.append({"resolution": "distinct", "reason": reason})
        return ConfirmDecision("distinct", reason=reason)


def _render_adjudication(draft: NodeDraft, candidates: list) -> str:
    lines = [
        "# Incoming memory",
        "",
        f"Type: {draft.node_type}",
        f"Title: {draft.title}",
        f"Body: {draft.body}",
        "",
        "# Existing candidates",
        "",
    ]
    if not candidates:
        lines.append("(none returned)")
    for c in candidates:
        lines += [
            f"- id: {c.get('id')}",
            f"  similarity: {c.get('similarity')}",
            f"  title: {c.get('title')}",
            f"  body: {c.get('body', '')}",
        ]
    return "\n".join(lines)


@dataclass
class IngestStats:
    """What ingest reports upward. Every field here appears in the run report;
    none of it is diagnostic-only."""

    haystack_id: str = ""
    drafts: int = 0
    inserted: int = 0
    merged: int = 0
    # Phase B: a >= t_high write whose content was novel was kept as its own
    # searchable member row + same_topic edge (`merged_linked`) rather than
    # absorbed into a get-only addendum. A distinct fact preserved, NOT a loss --
    # this is the metric that replaces Phase A's "facts absorbed as addenda".
    merged_linked: int = 0
    # The confirm-band hit rate's numerator -- first-class, not diagnostic.
    needs_confirmation: int = 0
    resolved_distinct: int = 0
    resolved_merge: int = 0
    resolved_to_insert: int = 0
    resolved_to_merge: int = 0
    resolved_to_merge_linked: int = 0
    edges_requested: int = 0
    edges_attached: int = 0
    supersede_attempted: int = 0
    # The honest measure of how much of the knowledge-update story is extraction
    # rather than engine: a supersede whose target could not be resolved is
    # downgraded to a plain write and counted here.
    supersede_target_unresolved: int = 0
    write_errors: Counter = field(default_factory=Counter)
    # A handful of worked examples behind the counters. Bounded to 5 so a
    # systematically failing extractor cannot fill the manifest, but present at
    # all so a nonzero error tally can be explained rather than merely reported.
    write_samples: list = field(default_factory=list)
    ingest_input_tokens: int = 0
    ingest_output_tokens: int = 0
    embed_seconds: float = 0.0
    write_seconds: float = 0.0
    total_seconds: float = 0.0

    @property
    def confirm_band_rate(self) -> float:
        return (self.needs_confirmation / self.drafts) if self.drafts else 0.0

    @property
    def nodes_written(self) -> int:
        """Active nodes this ingest is responsible for creating. Merge-link
        members (direct and via a pending resolution) are real embedded rows, so
        they count here exactly as inserts do."""
        return (self.inserted + self.resolved_to_insert
                + self.merged_linked + self.resolved_to_merge_linked)

    def as_dict(self) -> dict:
        return {
            "haystack_id": self.haystack_id,
            "drafts": self.drafts,
            "inserted": self.inserted,
            "merged": self.merged,
            "merged_linked": self.merged_linked,
            "needs_confirmation": self.needs_confirmation,
            "confirm_band_rate": round(self.confirm_band_rate, 4),
            "resolved_distinct": self.resolved_distinct,
            "resolved_merge": self.resolved_merge,
            "resolved_to_insert": self.resolved_to_insert,
            "resolved_to_merge": self.resolved_to_merge,
            "resolved_to_merge_linked": self.resolved_to_merge_linked,
            "nodes_written": self.nodes_written,
            "edges_requested": self.edges_requested,
            "edges_attached": self.edges_attached,
            "supersede_attempted": self.supersede_attempted,
            "supersede_target_unresolved": self.supersede_target_unresolved,
            "write_errors": dict(self.write_errors),
            "write_error_samples": self.write_samples,
            "ingest_input_tokens": self.ingest_input_tokens,
            "ingest_output_tokens": self.ingest_output_tokens,
            "embed_seconds": round(self.embed_seconds, 4),
            "write_seconds": round(self.write_seconds, 4),
            "total_seconds": round(self.total_seconds, 4),
        }


async def ingest_haystack(
    pool,
    run_space: RunSpace,
    haystack: Haystack,
    extractor: Extractor,
    *,
    confirm_policy: ConfirmPolicy | None = None,
    max_turns_per_window: int = 40,
    window_overlap: int = 4,
    embed_document=embedding.embed_document,
) -> IngestStats:
    """Ingest one haystack into its own scope, in chronological order.

    Order is load-bearing and is not an optimization detail: a knowledge-update
    question is only answerable if the store saw the superseded fact before the
    fact that replaces it, so sessions are processed in sequence and never
    concurrently.
    """
    policy = confirm_policy or AlwaysDistinct()
    scope_id = run_space.scope_for(haystack.haystack_id)
    stats = IngestStats(haystack_id=haystack.haystack_id)
    started = time.perf_counter()

    # local_id -> real node id, for resolving edge and supersede endpoints.
    resolved: dict[str, str] = {}
    # Title -> node id, across the whole haystack. The LLM extractor names a
    # supersede target by its *title* (the only handle it has -- it is shown
    # prior titles, never ids), so resolving an update needs this second map.
    # Kept separate from `resolved` rather than merged into it: local ids and
    # titles are different namespaces, and collapsing them would let a title
    # that happens to look like a local id silently address the wrong node.
    by_title: dict[str, str] = {}
    prior_titles: list[str] = []
    # Phase C: the searchable attr surface (keys per node type + the space's
    # write.attr_surface flag), resolved once per type and reused across drafts.
    # This is what puts stranded-attr content into the embedded document + tsvector
    # for the treatment arm (and, when the flag is off in the control arm, yields
    # extra_search='' == Phase B behavior).
    surface_cache: dict[str, tuple[set, bool]] = {}
    # Edges are accumulated across the whole haystack and attached at the end,
    # not per window. An extractor legitimately emits an edge whose destination
    # is written in a later session ("this relates to the job she mentioned
    # earlier" runs the other way too), and attaching per window would silently
    # drop every forward reference -- leaving traverse with a sparser graph than
    # the extractor actually produced and understating multi-hop.
    pending_edges: list[EdgeDraft] = []

    for session in haystack.sessions:
        windows = window_sessions(
            haystack.haystack_id,
            session,
            max_turns=max_turns_per_window,
            overlap=window_overlap,
            # Titles only -- never bodies, never the earlier transcript. Enough
            # to spot that a fact is being updated without letting ingest cost
            # grow quadratically in session count.
            prior_titles=tuple(prior_titles[-200:]),
        )
        for window in windows:
            result: ExtractResult = extractor.extract(window)
            stats.ingest_input_tokens += result.input_tokens
            stats.ingest_output_tokens += result.output_tokens

            for draft in result.nodes:
                node_id = await _write_draft(
                    pool, run_space, scope_id, draft, resolved, by_title, policy, stats,
                    embed_document=embed_document, session_id=session.session_id,
                    surface_cache=surface_cache,
                )
                if node_id is not None:
                    resolved[draft.local_id] = node_id
                    by_title[draft.title] = node_id
                    prior_titles.append(draft.title)

            pending_edges.extend(result.edges)

    if pending_edges:
        await _attach_edges(pool, run_space, tuple(pending_edges), resolved, stats)

    stats.total_seconds = time.perf_counter() - started
    return stats


async def _write_draft(
    pool,
    run_space: RunSpace,
    scope_id: str,
    draft: NodeDraft,
    resolved: dict[str, str],
    by_title: dict[str, str],
    policy: ConfirmPolicy,
    stats: IngestStats,
    *,
    embed_document,
    session_id: str,
    surface_cache: dict[str, tuple[set, bool]] | None = None,
) -> str | None:
    stats.drafts += 1
    if surface_cache is None:
        surface_cache = {}

    # Phase C: resolve the type's searchable attr surface (cached per type) and
    # render the draft's attrs into the extra searchable text, so the embedded
    # document + tsvector carry attr content that the body may not restate. The
    # injected embed_document embeds searchable_text(title, body, extra), matching
    # the engine's own tool-layer path.
    if draft.node_type not in surface_cache:
        surface_cache[draft.node_type] = await dedup.resolve_attr_surface(
            pool, run_space.space_id, draft.node_type)
    keys, surface_on = surface_cache[draft.node_type]
    extra = embedding.render_attr_surface(draft.attrs or {}, keys) if surface_on else ""

    # Outside any transaction -- design/02 trap 3, enforced by
    # scripts/check_no_embed_in_transaction.py for the engine and matched here.
    t0 = time.perf_counter()
    vector = embed_document(embedding.searchable_text(draft.title, draft.body, extra))
    stats.embed_seconds += time.perf_counter() - t0

    # Every edge goes through the end-of-haystack `link()` pass, so no draft
    # carries inline links. Keeping one attachment path means edge rules are
    # exercised identically for every edge rather than depending on whether the
    # destination happened to be written first.
    links: list[dict] = []

    old_id = _supersede_target(draft, resolved, by_title)
    t0 = time.perf_counter()
    try:
        if draft.supersedes_local_id is not None:
            stats.supersede_attempted += 1
            if old_id is None:
                # Downgraded to a plain write and counted: the extractor
                # believed this updated something it could not point at, and
                # pretending otherwise would credit the engine for a supersede
                # chain that was never created.
                stats.supersede_target_unresolved += 1
                envelope = await _plain_write(
                    pool, run_space, scope_id, draft, vector, links, session_id, extra
                )
            else:
                envelope = await dedup.supersede(
                    pool,
                    run_space.space_id,
                    run_space.principal,
                    old_id,
                    draft.node_type,
                    scope_id,
                    draft.title,
                    draft.body,
                    draft.attrs,
                    vector,
                    SOURCE_CLIENT,
                    source_session=session_id,
                    links=links or None,
                    extra_search=extra,
                )
        else:
            envelope = await _plain_write(
                pool, run_space, scope_id, draft, vector, links, session_id, extra
            )
    except Exception as exc:  # noqa: BLE001 -- classified, counted, never fatal
        # One bad draft must not abort a 500-haystack run, but it must be
        # visible: silently dropped drafts are missing memories that look like
        # retrieval failures downstream.
        #
        # Counted by exception *class* alone this was useless in practice: a real
        # run reported six `CheckViolation`s and the tally could not say which
        # constraint, on which draft. A dropped draft is a missing memory, and a
        # missing memory that cannot be explained is indistinguishable from weak
        # retrieval -- so the classifier records the constraint name and the
        # first offending draft's shape, which is what makes the loss diagnosable
        # instead of merely countable.
        stats.write_errors[_classify(exc)] += 1
        if len(stats.write_samples) < 5:
            stats.write_samples.append({
                "error": _classify(exc),
                "detail": str(exc)[:300],
                "node_type": draft.node_type,
                "title_len": len(draft.title),
                "body_len": len(draft.body),
                "attr_keys": sorted(draft.attrs or {}),
                "title": draft.title[:120],
            })
        return None
    finally:
        stats.write_seconds += time.perf_counter() - t0

    return await _record_outcome(pool, run_space, envelope, draft, policy, stats)


async def _plain_write(pool, run_space: RunSpace, scope_id, draft, vector, links, session_id, extra=""):
    return await dedup.write(
        pool,
        run_space.space_id,
        run_space.principal,
        draft.node_type,
        scope_id,
        draft.title,
        draft.body,
        draft.attrs,
        vector,
        SOURCE_CLIENT,
        # No thresholds, no resonance_floor, no import_mode. See module docstring.
        links=links or None,
        source_session=session_id,
        extra_search=extra,
    )


async def _record_outcome(
    pool, run_space: RunSpace, envelope: dict, draft: NodeDraft, policy: ConfirmPolicy,
    stats: IngestStats,
) -> str | None:
    outcome = envelope.get("outcome")

    if outcome == "inserted":
        stats.inserted += 1
        stats.edges_attached += int(envelope.get("links_attached") or 0)
        return envelope["node"]["id"]

    if outcome == "merged":
        stats.merged += 1
        stats.edges_attached += int(envelope.get("links_attached") or 0)
        return envelope["canonical"]["id"]

    if outcome == "merged_linked":
        # Phase B: the incoming was banded >= t_high but judged distinct, so it
        # inserted as its own member row (envelope["node"]) linked to the
        # canonical. Return the MEMBER id so the extractor's edges attach to the
        # fact that was actually written, and so the draft's local id maps to a
        # real, searchable node.
        stats.merged_linked += 1
        return envelope["node"]["id"]

    if outcome == "needs_confirmation":
        stats.needs_confirmation += 1
        decision = policy.decide(envelope, draft)
        try:
            resolved_env = await dedup.resolve_duplicate(
                pool,
                run_space.space_id,
                run_space.principal,
                envelope["pending_id"],
                decision.resolution,
                merge_into=decision.merge_into,
            )
        except Exception as exc:  # noqa: BLE001
            stats.write_errors[f"resolve:{_classify(exc)}"] += 1
            if len(stats.write_samples) < 5:
                stats.write_samples.append({
                    "error": f"resolve:{_classify(exc)}",
                    "detail": str(exc)[:300],
                    "title": draft.title[:120],
                })
            return None

        if decision.resolution == "distinct":
            stats.resolved_distinct += 1
        else:
            stats.resolved_merge += 1

        # A "distinct" resolution can still land as a merge: resolve_duplicate
        # re-enters the banding core, so a twin that arrived while the write sat
        # parked is still caught. Record what actually happened, not what was
        # asked for.
        inner = resolved_env.get("outcome")
        if inner == "inserted":
            stats.resolved_to_insert += 1
            return resolved_env["node"]["id"]
        if inner == "merged":
            stats.resolved_to_merge += 1
            return resolved_env["canonical"]["id"]
        if inner == "merged_linked":
            # Phase B: a distinct resolution whose payload banded >= t_high novel
            # against a twin that landed while it sat parked merge-links instead of
            # inserting. The member is a real row; return it.
            stats.resolved_to_merge_linked += 1
            return resolved_env["node"]["id"]
        stats.write_errors[f"resolve_outcome:{inner}"] += 1
        return None

    # review_queued should be unreachable: it is the import_mode surface and
    # import_mode is never set. If it appears, someone reintroduced the bypass.
    stats.write_errors[f"outcome:{outcome}"] += 1
    return None


def _classify(exc: Exception) -> str:
    """Exception class, plus the database constraint when there is one.

    psycopg carries the violated constraint in `diag.constraint_name`. Without
    it a `CheckViolation` tally says only "a row was rejected"; with it the tally
    names which rule rejected it, which is the difference between a number to
    report and a fault to fix.
    """
    name = type(exc).__name__
    diag = getattr(exc, "diag", None)
    constraint = getattr(diag, "constraint_name", None) if diag is not None else None
    return f"{name}:{constraint}" if constraint else name


def _supersede_target(
    draft: NodeDraft, resolved: dict[str, str], by_title: dict[str, str]
) -> str | None:
    """Resolve a supersede reference to a real node id, or None.

    Three handles are accepted, because different extractors have different ones
    available: a local draft id, a prior memory's exact title (what the LLM
    extractor is shown), or an already-real node id.

    A reference that is none of them -- a hallucinated title, or a draft that
    merged and so never became a node -- must return None so the caller counts
    it as `supersede_target_unresolved`. Passing the raw string through to
    `dedup.supersede` instead would surface it as a generic write error, which
    is the wrong story: it is an *extraction* failure, and that counter is the
    honest measure of how much of the knowledge-update result is extraction
    rather than engine.
    """
    ref = draft.supersedes_local_id
    if ref is None:
        return None
    hit = resolved.get(ref) or by_title.get(ref)
    if hit:
        return hit
    return ref if _looks_like_node_id(ref) else None


def _looks_like_node_id(value: str) -> bool:
    """Node ids are UUIDs. Anything else is an extractor-local name that we
    failed to resolve, not an address."""
    import uuid

    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


async def _attach_edges(
    pool, run_space: RunSpace, edges: tuple[EdgeDraft, ...], resolved: dict[str, str],
    stats: IngestStats,
) -> None:
    """Second pass: edges whose endpoints are now both real nodes.

    Uses the same `link()` core the MCP tool calls, so edge rules and the
    both-endpoints-readable requirement apply exactly as they would in
    production.
    """
    payload = []
    for e in edges:
        src = resolved.get(e.src_local_id)
        dst = resolved.get(e.dst_local_id)
        if src and dst and src != dst:
            payload.append({"type": e.edge_type, "src_id": src, "dst_id": dst})

    stats.edges_requested += len(edges)
    if not payload:
        return

    from engraphy.core import link as link_core

    try:
        result = await link_core.link(pool, run_space.space_id, run_space.principal, payload)
        stats.edges_attached += int(result.get("attached") or 0)
    except Exception as exc:  # noqa: BLE001
        stats.write_errors[f"link:{type(exc).__name__}"] += 1
