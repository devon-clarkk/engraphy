"""Extraction: conversational turns -> typed node drafts (design/09 §Ingest).

This module is the confound, made visible. Public benchmarks assume a memory
system ingests a transcript; Engraphy ingests typed, attr-validated nodes. Whoever
bridges that gap is doing part of the work being measured, and every vendor's
harness bridges it silently. Ours ships two bridges and reports both:

* `VerbatimExtractor` -- one turn, one `note`, no LLM, no judgment. The **floor**:
  what the store does with zero adapter intelligence. It emits no edges, so
  `traverse` has nothing to walk and multi-hop accuracy under it should sit at
  the search-only baseline. That is the demonstration, not a defect -- graph
  structure has to be written before it can be traversed.
* `LLMExtractor` -- typed against the pack registry, emits edges and supersede
  intents. The **headline**, and like-for-like with how Mem0/Zep/Letta ingest.

The gap between the two rows is the adapter's contribution, stated numerically
instead of argued about.

`NodeDraft.local_id` exists because a draft's edges refer to other drafts that
may not be written yet (or at all, if they merge into an existing node). Ingest
resolves local ids to real node ids after each write; see `bench/core/ingest.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from bench.core.corpus import Session, Turn

__all__ = [
    "BODY_MAX",
    "TITLE_MAX",
    "EdgeDraft",
    "ExtractResult",
    "ExtractWindow",
    "ExtractionError",
    "Extractor",
    "LLMExtractor",
    "NodeDraft",
    "VerbatimExtractor",
    "build_extraction_schema",
    "validate_against_pack",
    "window_sessions",
]

# Mirrors the DDL's CHECK constraints (design/01: title 3-200, body 1-8000). A
# draft that violates them is rejected by Postgres at write time with a
# constraint error that says nothing about which turn produced it, so drafts are
# clamped here where the provenance is still in hand.
TITLE_MAX = 200
TITLE_MIN = 3
BODY_MAX = 8000

# design/09 §single-hop finding (2026-07-22). Typed extraction paraphrases, so a
# node loses the speaker's exact words and an exact-wording lookup stops matching
# it -- which is why the verbatim floor beat the LLM extractor on single-hop. A
# node's search vector and embedding are built from title+body, so the fix is to
# retain the verbatim source turn(s) IN THE BODY: the node stays typed (its type,
# title and attrs are unchanged, its edges intact) and additionally becomes
# word-searchable. This is a general recall property, not a single-hop trick --
# any exact-phrasing query benefits, and a real deployment that wants both
# structure and verbatim recall would store both. It is recorded in the manifest
# and the source text is deterministically the turn(s) the model cites, never
# re-summarised, so nothing is invented.
LLM_RETAIN_SOURCE_TEXT = True
_SOURCE_MARKER = "\n\nSource (verbatim):\n"


class ExtractionError(ValueError):
    """A draft cannot be made writable without inventing content."""


@dataclass(frozen=True, slots=True)
class NodeDraft:
    """One prospective node. `local_id` is extractor-local and is resolved to a
    real node id after the write; `provenance` carries the turn ids the draft
    came from, so a surprising result can be traced back to the transcript."""

    local_id: str
    node_type: str
    title: str
    body: str
    attrs: dict = field(default_factory=dict)
    supersedes_local_id: str | None = None
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.local_id:
            raise ExtractionError("NodeDraft.local_id is empty")
        if not self.node_type:
            raise ExtractionError(f"NodeDraft {self.local_id!r} has no node_type")
        if not (TITLE_MIN <= len(self.title) <= TITLE_MAX):
            raise ExtractionError(
                f"NodeDraft {self.local_id!r} title length {len(self.title)} outside "
                f"{TITLE_MIN}..{TITLE_MAX}"
            )
        if not (1 <= len(self.body) <= BODY_MAX):
            raise ExtractionError(
                f"NodeDraft {self.local_id!r} body length {len(self.body)} outside 1..{BODY_MAX}"
            )


@dataclass(frozen=True, slots=True)
class EdgeDraft:
    """An edge between two drafts. Either endpoint may instead name an already
    written node id -- `ingest` treats an id it cannot find in the local map as
    a real node id."""

    src_local_id: str
    dst_local_id: str
    edge_type: str


@dataclass(frozen=True, slots=True)
class ExtractWindow:
    """What an extractor is allowed to see.

    Deliberately narrow. `prior_titles` carries the titles of nodes written from
    earlier sessions -- titles only, never bodies and never the earlier
    transcript. That is enough to detect that a fact is being updated (the
    supersede signal) without letting the extractor re-read and re-summarize
    history it has already processed, which would make ingest cost quadratic and
    let late sessions silently rewrite early ones.

    An extractor never sees a question. Any extraction prompt that mentions a
    benchmark, a category, or a question format is a neutrality breach
    (design/09 §Neutrality).
    """

    haystack_id: str
    session: Session
    turns: tuple[Turn, ...]
    window_index: int
    prior_titles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractResult:
    nodes: tuple[NodeDraft, ...] = ()
    edges: tuple[EdgeDraft, ...] = ()
    # Populated by LLM-backed extractors; folded into the run's ingest-token
    # accounting, which is reported separately from retrieval tokens and never
    # netted against them (design/09 §Token accounting).
    input_tokens: int = 0
    output_tokens: int = 0


@runtime_checkable
class Extractor(Protocol):
    name: str
    emits_edges: bool

    def extract(self, window: ExtractWindow) -> ExtractResult: ...


class VerbatimExtractor:
    """One turn -> one `note`. The floor.

    Zero judgment by construction: no LLM, no type selection, no attrs, no
    edges, no supersede detection. Everything that reaches the store is text the
    speaker actually said, and the only decision made is where to cut a title.

    Why it is worth running at all: it isolates the engine. Any accuracy the
    floor achieves is the retrieval stack alone, with no adapter intelligence
    upstream of it, which makes the LLM extractor's margin measurable rather
    than assumed.
    """

    name = "verbatim"
    emits_edges = False

    def __init__(self, node_type: str = "note") -> None:
        self.node_type = node_type

    def extract(self, window: ExtractWindow) -> ExtractResult:
        drafts: list[NodeDraft] = []
        for i, turn in enumerate(window.turns):
            body = f"{turn.speaker}: {turn.text}".strip()
            if len(body) > BODY_MAX:
                body = body[:BODY_MAX]
            drafts.append(
                NodeDraft(
                    local_id=f"{window.haystack_id}:{window.session.session_id}:{window.window_index}:{i}",
                    node_type=self.node_type,
                    title=_title_from(turn),
                    body=body,
                    attrs={},
                    provenance=(turn.turn_id,) if turn.turn_id else (),
                )
            )
        return ExtractResult(nodes=tuple(drafts))


class LLMExtractor:
    """Typed extraction against the pack registry. The headline configuration.

    Like-for-like with how Mem0, Zep and Letta ingest -- all of them put an LLM
    between the transcript and the store -- which is why this, not the verbatim
    floor, produces the number compared against theirs.

    The output schema is **built from the live pack**, not hand-written: node
    types, their attributes, and which attributes are required all come from the
    registry the write path will validate against. A draft that would be
    rejected by the attr-spec trigger is therefore mostly unrepresentable rather
    than merely unlikely, and the remaining gap is caught by
    `validate_against_pack` before any write is attempted.

    The prompt is suite-agnostic and never sees a question (design/09
    §Neutrality). It is a committed file, hashed into the run manifest.
    """

    name = "llm"
    emits_edges = True

    def __init__(
        self,
        client,
        pack: dict,
        *,
        prompt_name: str = "extract.md",
        effort: str = "high",
        max_tokens: int = 16000,
        retain_source_text: bool = LLM_RETAIN_SOURCE_TEXT,
    ) -> None:
        from bench.core.llm import load_prompt

        self.client = client
        self.pack = pack
        self.prompt_name = prompt_name
        self.system = load_prompt(prompt_name)
        self.effort = effort
        self.max_tokens = max_tokens
        self.retain_source_text = retain_source_text
        self.schema = build_extraction_schema(pack)
        self._node_types = dict(pack.get("node_types") or {})
        self._edge_types = tuple(sorted((pack.get("edge_types") or {}).keys()))

    def extract(self, window: ExtractWindow) -> ExtractResult:
        from bench.core.llm import LLMError

        user = _render_window(window, self._node_types, self._edge_types,
                              show_turn_ids=self.retain_source_text)
        try:
            resp = self.client.complete(
                self.system,
                user,
                schema=self.schema,
                max_tokens=self.max_tokens,
                effort=self.effort,
            )
        except LLMError:
            # One failed window must not abort a 500-haystack run. The caller
            # records the gap; an empty result is honest -- it says this window
            # contributed nothing, which is exactly what happened.
            return ExtractResult()

        payload = resp.data if resp.data is not None else {}
        nodes, edges = _drafts_from_payload(
            payload, window, self._node_types, retain_source_text=self.retain_source_text)
        return ExtractResult(
            nodes=nodes,
            edges=edges,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
        )


def build_extraction_schema(pack: dict) -> dict:
    """A JSON schema mirroring the pack's own registry.

    Structured output is enforced server-side, so encoding the ontology here is
    what keeps the extractor inside the type system rather than merely
    encouraged toward it. `additionalProperties: false` is required throughout
    for strict schema support.
    """
    node_types = sorted((pack.get("node_types") or {}).keys())
    edge_types = sorted((pack.get("edge_types") or {}).keys())
    if not node_types:
        raise ExtractionError("pack declares no node_types; cannot build an extraction schema")

    attr_props: dict[str, dict] = {}
    for spec in (pack.get("node_types") or {}).values():
        attrs = (spec or {}).get("attrs") or {}
        for group in ("required", "optional"):
            for key, kind in (attrs.get(group) or {}).items():
                attr_props.setdefault(key, _attr_schema(kind))

    return {
        "type": "object",
        "properties": {
            "memories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "local_id": {"type": "string"},
                        "node_type": {"type": "string", "enum": node_types},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "attrs": {
                            "type": "object",
                            "properties": attr_props,
                            "additionalProperties": False,
                        },
                        "supersedes_title": {"type": "string"},
                        # The turn ids (dia_ids) this memory is drawn from. Used
                        # to retain the verbatim source text in the body so a
                        # typed node stays word-searchable (design/09 single-hop
                        # finding). Resolved deterministically against the window
                        # -- the model names the turns, the harness copies their
                        # exact text, so nothing is re-summarised.
                        "source_turn_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["local_id", "node_type", "title", "body"],
                    "additionalProperties": False,
                },
            },
            "edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "src_local_id": {"type": "string"},
                        "dst_local_id": {"type": "string"},
                        "edge_type": {"type": "string", "enum": edge_types},
                    },
                    "required": ["src_local_id", "dst_local_id", "edge_type"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["memories", "edges"],
        "additionalProperties": False,
    }


def _attr_schema(kind: dict) -> dict:
    """Map one attr-spec entry to JSON schema.

    Deliberately lossy in the safe direction: `date` becomes a plain string
    rather than a format-constrained one, because the engine's own validator is
    the authority on date shape and duplicating the rule here would create two
    places for it to drift.
    """
    kind = kind or {}
    if "enum" in kind:
        return {"type": "string", "enum": list(kind["enum"])}
    t = kind.get("type")
    if t == "int":
        return {"type": "integer"}
    if t == "number":
        return {"type": "number"}
    if t == "bool":
        return {"type": "boolean"}
    return {"type": "string"}


def validate_against_pack(draft: NodeDraft, pack: dict) -> list[str]:
    """Re-check a draft with the engine's own attr-spec interpreter.

    The schema constrains which attribute *keys* may appear, but not which are
    required for the chosen type, nor that a closed spec rejects a key borrowed
    from a different type. `engraphy.core.attr_spec` is the same code the database
    trigger mirrors, so a draft that passes here passes at write time.
    """
    from engraphy.core.attr_spec import validate_attrs

    spec = (pack.get("node_types") or {}).get(draft.node_type)
    if spec is None:
        return [f"unknown node_type {draft.node_type!r}"]
    return validate_attrs(spec, draft.attrs or {})


def _render_window(window: ExtractWindow, node_types: dict, edge_types: tuple,
                   *, show_turn_ids: bool = False) -> str:
    """The user turn: the ontology, the prior titles, and the transcript.

    The ontology is rendered per-call rather than baked into the prompt file so
    that changing the pack cannot silently desynchronize from the instructions
    the extractor is given.

    When `show_turn_ids` is set, each turn is prefixed with its dia_id so the
    model can cite which turns a memory came from (`source_turn_ids`), which the
    harness then resolves to verbatim source text. The ids are provenance, not
    content, and never reveal a question -- design/09 §Neutrality is unaffected.
    """
    lines: list[str] = ["# Types", ""]
    for name, spec in sorted(node_types.items()):
        attrs = (spec or {}).get("attrs") or {}
        req = ", ".join(sorted((attrs.get("required") or {}).keys())) or "none"
        opt = ", ".join(sorted((attrs.get("optional") or {}).keys())) or "none"
        desc = (spec or {}).get("description", "")
        lines.append(f"- **{name}** — {desc}")
        lines.append(f"  required attrs: {req}; optional attrs: {opt}")
    lines += ["", "# Edge types", "", ", ".join(edge_types) or "none", ""]

    if window.prior_titles:
        lines += ["# Memories already stored from earlier sessions", ""]
        lines += [f"- {t}" for t in window.prior_titles]
        lines.append("")
    else:
        lines += ["# Memories already stored from earlier sessions", "", "(none)", ""]

    lines += [f"# Conversation window (session {window.session.session_id})", ""]
    if window.session.timestamp:
        lines.append(f"Session timestamp: {window.session.timestamp}")
        lines.append("")
    for turn in window.turns:
        prefix = f"[{turn.turn_id}] " if (show_turn_ids and turn.turn_id) else ""
        lines.append(f"{prefix}{turn.speaker}: {turn.text}")
    return "\n".join(lines)


def _drafts_from_payload(
    payload: dict, window: ExtractWindow, node_types: dict, *, retain_source_text: bool = False
) -> tuple[tuple[NodeDraft, ...], tuple[EdgeDraft, ...]]:
    """Turn the model's JSON into drafts, dropping anything unusable.

    Dropping rather than raising: a single malformed memory in a window of ten
    should cost that one memory, not the other nine. Every drop is silent here
    but visible downstream -- the drafts simply never reach the write path, and
    the ingest counters show fewer drafts than the transcript would suggest.
    """
    provenance = tuple(t.turn_id for t in window.turns if t.turn_id)
    prefix = f"{window.haystack_id}:{window.session.session_id}:{window.window_index}"
    # id -> verbatim turn text, for source-text retention. The map is the
    # window's own turns, so a cited id that is not in the window resolves to
    # nothing (the model cannot smuggle in text that was not shown).
    turn_text = {t.turn_id: f"{t.speaker}: {t.text}" for t in window.turns if t.turn_id}

    drafts: list[NodeDraft] = []
    local_ids: set[str] = set()
    for i, raw in enumerate(payload.get("memories") or []):
        if not isinstance(raw, dict):
            continue
        node_type = str(raw.get("node_type") or "")
        if node_type not in node_types:
            continue
        title = _clamp_title(str(raw.get("title") or ""))
        body = str(raw.get("body") or "").strip()
        if not title or not body:
            continue
        if retain_source_text:
            body = _append_source_text(body, raw.get("source_turn_ids"), turn_text)
        local_id = f"{prefix}:{raw.get('local_id') or i}"
        supersedes = raw.get("supersedes_title")
        try:
            drafts.append(
                NodeDraft(
                    local_id=local_id,
                    node_type=node_type,
                    title=title,
                    body=body[:BODY_MAX],
                    attrs={k: v for k, v in (raw.get("attrs") or {}).items() if v is not None},
                    # Resolved from title to a real node id by the ingest loop,
                    # which owns the title -> id map across sessions.
                    supersedes_local_id=str(supersedes) if supersedes else None,
                    provenance=provenance,
                )
            )
            local_ids.add(str(raw.get("local_id") or i))
        except ExtractionError:
            continue

    edges: list[EdgeDraft] = []
    for raw in payload.get("edges") or []:
        if not isinstance(raw, dict):
            continue
        src, dst = str(raw.get("src_local_id") or ""), str(raw.get("dst_local_id") or "")
        etype = str(raw.get("edge_type") or "")
        # Both endpoints must be memories this window actually produced --
        # an edge to a dropped or hallucinated local_id has nothing to attach to.
        if src in local_ids and dst in local_ids and src != dst and etype:
            edges.append(
                EdgeDraft(
                    src_local_id=f"{prefix}:{src}",
                    dst_local_id=f"{prefix}:{dst}",
                    edge_type=etype,
                )
            )

    return tuple(drafts), tuple(edges)


def _append_source_text(body: str, source_ids, turn_text: dict) -> str:
    """Append the verbatim text of the cited turns to a typed body.

    The node keeps its typed identity (type, title, attrs, edges) and gains the
    speaker's exact words, so an exact-phrasing search matches it. Three guards
    keep this honest rather than inflationary:

    * **Only turns actually shown** are appended -- a cited id absent from this
      window resolves to nothing, so the model cannot introduce unseen text.
    * **No double-counting** -- a quote already substring-present in the body
      (the model often quotes anyway) is skipped, so the source block adds only
      words the typed body dropped.
    * **Bounded** -- the whole body is clamped to BODY_MAX by the caller, and a
      draft whose source would blow the limit simply keeps less of it.

    A memory that cites no turns is returned unchanged: retention never
    fabricates a source.
    """
    if not isinstance(source_ids, list):
        return body
    seen: set[str] = set()
    quotes: list[str] = []
    for sid in source_ids:
        text = turn_text.get(str(sid))
        if not text or text in seen:
            continue
        if text.strip() and text not in body:  # skip what the body already says
            quotes.append(text)
            seen.add(text)
    if not quotes:
        return body
    block = _SOURCE_MARKER + "\n".join(quotes)
    # Leave room for the marker+quotes within BODY_MAX; if the typed body alone
    # already fills it, retention yields to the typed content rather than
    # truncating mid-fact.
    room = BODY_MAX - len(body)
    if room <= len(_SOURCE_MARKER) + 8:
        return body
    return body + block[:room]


def _clamp_title(title: str) -> str:
    title = " ".join(title.split())
    if len(title) > TITLE_MAX:
        title = title[: TITLE_MAX - 1].rstrip() + "…"
    return title if len(title) >= TITLE_MIN else ""


def _title_from(turn: Turn) -> str:
    """A title that is a prefix of what was said, prefixed by the speaker.

    Not a summary: summarizing would be judgment, and the floor's whole job is
    to have none. The speaker prefix stays because attribution is what several
    single-hop questions are actually about.
    """
    text = " ".join(turn.text.split())
    prefix = f"{turn.speaker}: "
    budget = TITLE_MAX - len(prefix)
    if budget < TITLE_MIN:
        # A pathological speaker label; fall back to the text alone rather than
        # producing a title that is all label and no content.
        prefix, budget = "", TITLE_MAX
    if len(text) > budget:
        cut = text[: budget - 1].rstrip()
        # Prefer a word boundary, but never produce a stub.
        space = cut.rfind(" ")
        if space >= TITLE_MIN:
            cut = cut[:space]
        text = cut + "…"
    title = f"{prefix}{text}".strip()
    if len(title) < TITLE_MIN:
        title = (title + " .")[:TITLE_MAX].ljust(TITLE_MIN, ".")
    return title[:TITLE_MAX]


def window_sessions(
    haystack_id: str,
    session: Session,
    *,
    max_turns: int = 40,
    overlap: int = 4,
    prior_titles: tuple[str, ...] = (),
) -> list[ExtractWindow]:
    """Split a session into extraction windows.

    Sessions are processed whole where they fit, because a fact and the turn
    that qualifies it are usually adjacent and splitting between them loses the
    qualification. The overlap exists for the same reason at the seam.
    """
    if max_turns <= 0:
        raise ExtractionError("max_turns must be positive")
    if overlap >= max_turns:
        raise ExtractionError("overlap must be smaller than max_turns")

    turns = session.turns
    if len(turns) <= max_turns:
        return [
            ExtractWindow(
                haystack_id=haystack_id,
                session=session,
                turns=turns,
                window_index=0,
                prior_titles=prior_titles,
            )
        ]

    windows: list[ExtractWindow] = []
    step = max_turns - overlap
    for wi, start in enumerate(range(0, len(turns), step)):
        chunk = turns[start : start + max_turns]
        if not chunk:
            break
        windows.append(
            ExtractWindow(
                haystack_id=haystack_id,
                session=session,
                turns=chunk,
                window_index=wi,
                prior_titles=prior_titles,
            )
        )
        if start + max_turns >= len(turns):
            break
    return windows
