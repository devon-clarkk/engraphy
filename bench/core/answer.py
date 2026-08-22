"""The reader: a retrieval envelope -> an answer (design/09 §Answer extraction).

One LLM call, and its entire context is three things: the committed system
prompt `bench/prompts/read.md`, the question, and a **rendered presentation of
the retrieval envelope** (`render_envelope`, below). It sees no corpus, no gold
answer, no category, no evidence pointer, and no other question. That narrowness
is the point -- it is what makes the accuracy figure a statement about what
retrieval put in front of an agent, rather than about what the harness happened
to know.

**Presentation vs measurement -- deliberately separated.** Until July 2026 the
reader was handed `json.dumps(envelope)` raw: every node prefixed by a UUID,
type, and scope, its title/body buried, ten times over, source turns dumped
unlabelled. That is a serialization format, not something written to be read, and
a forensic pass over reader-misses found the reader hedging on facts that were
present but hard to see in the blob. `render_envelope` now turns the envelope
into clean, rank-ordered prose: each result numbered by relevance, title and body
as text, one compact provenance line per fact (recorded-date · author · scope),
the answer-irrelevant scaffolding (ids, status, raw relevance scores) omitted from
what the reader sees, verbatim source text left in place under its own marker. It
is **faithful** -- no node, title, body, or attribute is dropped or altered, and
the provenance line renders only fields the node actually carries (nothing is
invented) -- and **general**: it is shaped by retrieval structure (results,
briefing sections, graph neighbours), never by any question or the answer key.

**The payload metric still measures the canonical envelope, on purpose.**
`envelope_bytes` / `envelope_sha256` are computed on `json.dumps(envelope)` -- the
retrieval *output*, not the rendered reader input. This keeps the "what this
memory costs to have in context" number comparable across strategies and across
runs (design/09 §Token accounting pins it to the serialized envelope, and
`meter.measure_payload` measures the same thing), and keeps the hash a stable
provenance tie from an answer back to the exact retrieval that produced it
(design/09 §Neutrality item 8). The rendered string the reader actually receives
is a deterministic, committed function of that hashed envelope, so "what was sent"
is fully reproducible from the recorded hash plus this module's
`RENDER_FORMAT_VERSION`. The reader-input shape is recorded in the manifest.

**Reader token counts are deliberately absent, not estimated.** The Claude CLI
route reports usage for its own injected system prompt (~9-15k cache-creation
tokens per call), so its `input_tokens` measures Claude Code rather than the
memory payload; `count_tokens` on that client raises rather than returning a
number that would look like a measurement. Bytes are reported instead and
labelled as such. A byte comparison between two strategies is
tokenizer-independent, so the questions the token metric exists to answer --
does briefing cost less context than search -- survive the substitution; an
absolute "N tokens per answer" claim does not, and is not made.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass

from bench.core.corpus import Question
from bench.core.llm import LLMError
from bench.core.providers import QuotaExhausted
from bench.core.retrieve import Retrieval

__all__ = [
    "Answer", "Reader", "READ_PROMPT", "INSUFFICIENT", "is_abstention",
    "READER_SKILL", "READER_OUTPUT_CONTRACT", "build_reader_system",
    "READER_STANCES", "READER_DEFAULT_STANCE",
    "render_envelope", "RENDER_FORMAT_VERSION",
]

READ_PROMPT = "read.md"

# Bumped whenever the reader-input rendering changes shape, so a run's manifest
# records exactly how the envelope was presented and two runs with different
# presentations are never silently compared. v2 adds a per-fact provenance line
# (recorded-date / author / scope), previously stripped as scaffolding.
RENDER_FORMAT_VERSION = "rendered_prose_v2"

# Node fields that are retrieval bookkeeping or relevance internals, never
# answer-bearing, and so are omitted from what the reader sees. `id` is plumbing;
# `status` is always 'active' on a returned node; `score`/`similarity`/
# `edge_count` are relevance internals the rank ORDER already conveys, and
# exposing them invites the score-thresholding the reader skill explicitly warns
# against; `resolved_from` is a merge-chain pointer. Nothing here carries content.
# NOTE: `created_at`, `author`, and `scope` are NOT stripped -- they feed the one
# compact provenance line per fact (`_provenance_line`), because when/who/where a
# memory was recorded is legitimate context for reasoning about recency and
# source, not noise. They are rendered as a single labelled line, never as raw
# key/value dumps.
_NODE_SCAFFOLD = frozenset(
    {"id", "status", "score", "similarity", "edge_count", "resolved_from"}
)


def _readable_date(created_at) -> str:
    """The recorded-date as a short human date. Faithful: derived only from the
    node's own `created_at`; if it is not a parseable ISO datetime the raw value
    is returned unchanged rather than guessed at. This is the INGEST time (when
    the memory was written), deliberately labelled `recorded` by the caller so it
    is never mistaken for when the fact itself occurred -- that date, when known,
    lives in the body or in `attrs` and is rendered there."""
    s = str(created_at).strip()
    if not s:
        return ""
    try:
        import datetime
        return datetime.date.fromisoformat(s[:10]).strftime("%d %b %Y")
    except (ValueError, TypeError):
        return s


def _provenance_line(node: dict) -> str:
    """One compact provenance line: `recorded <date> · by <author> · in <scope>`.
    Only the segments whose fields are present on the node are emitted (nothing is
    invented); an all-absent node yields no line at all."""
    bits = []
    ca = node.get("created_at")
    if ca:
        d = _readable_date(ca)
        if d:
            bits.append(f"recorded {d}")
    who = node.get("author")
    if who:
        bits.append(f"by {who}")
    scope = node.get("scope")
    if scope:
        bits.append(f"in {scope}")
    return " · ".join(bits)


def _attrs_str(attrs: dict) -> str:
    """Non-empty attrs as `key: value` pairs. attrs can carry answer-bearing
    structured data (e.g. an `occurred` date on a temporal fact), so they are
    surfaced, not stripped -- deterministically ordered for reproducibility."""
    if not isinstance(attrs, dict) or not attrs:
        return ""
    pairs = [f"{k}: {v}" for k, v in sorted(attrs.items())
             if v not in (None, "", [], {})]
    return "; ".join(pairs)


def _node_block(node: dict) -> str:
    """One node rendered as prose: a light type/attrs/graph-depth tag, then the
    title, then the body when it adds anything over the title. Faithful -- title,
    body, non-empty attrs and any linked nodes are all kept; only `_NODE_SCAFFOLD`
    is omitted. The body is emitted verbatim, so a `Source (verbatim):` marker
    already inside it stays exactly where retrieval put it."""
    if not isinstance(node, dict):
        return str(node)
    title = (node.get("title") or "").strip()
    body = (node.get("body") or "").strip()

    tag_bits = []
    ntype = node.get("type")
    if ntype:
        tag_bits.append(f"[{ntype}]")
    astr = _attrs_str(node.get("attrs") or {})
    if astr:
        tag_bits.append(f"({astr})")
    depth = node.get("depth")
    if depth:
        tag_bits.append(f"(reached via {depth} link{'s' if depth != 1 else ''})")
    prefix = " ".join(tag_bits)

    head = f"{prefix} {title}".strip() if prefix else title
    out = [head] if head else []
    if body and body != title:
        out.append(body)
    for ln in (node.get("linked") or []):
        lt = (ln.get("title") or "").strip()
        lb = (ln.get("body") or "").strip()
        out.append(f"linked → {lt}" if lt else "linked →")
        if lb and lb != lt:
            out.append(lb)
    prov = _provenance_line(node)
    if prov:
        out.append(f"({prov})")
    return "\n".join(out).strip()


def _render_items(items: list) -> str:
    """A ranked list of results/nodes, numbered by position. Accepts either
    search-result dicts (`{node, score, ...}`) or bare node dicts (briefing
    sections, traversed neighbours) -- the `.get("node", item)` handles both."""
    if not items:
        return "(no memories retrieved)"
    blocks = []
    for i, item in enumerate(items, start=1):
        node = item.get("node", item) if isinstance(item, dict) else item
        blocks.append(f"[{i}] {_node_block(node)}")
    return "\n\n".join(blocks)


def render_envelope(envelope: dict) -> str:
    """Turn a retrieval envelope into clean, rank-ordered, readable prose.

    General over the three shipped strategy shapes -- `search` / `search+traverse`
    (`results` [+ `traversed`]), `briefing+search` (`briefing` + `search`) -- plus
    a bare-`nodes` fallback and, for any unrecognised shape, a legible JSON dump
    so content is never silently lost. Shaped by retrieval STRUCTURE only; it
    names no question and encodes nothing about the answer key.
    """
    if not isinstance(envelope, dict):
        return str(envelope)

    # briefing_then_search: pushed sections first, then the ranked search results.
    if "briefing" in envelope and "search" in envelope:
        parts = ["Session briefing (context pushed for this question):"]
        for section in (envelope["briefing"].get("sections") or []):
            name = section.get("name", "section")
            parts.append(f"-- {name} --\n{_render_items(section.get('nodes') or [])}")
        search = envelope.get("search") or {}
        parts.append("Search results (most relevant first):\n"
                     + _render_items(search.get("results") or []))
        return "\n\n".join(parts).strip()

    # search / search_then_traverse
    if "results" in envelope:
        parts = ["The following memories were retrieved, most relevant first:",
                 _render_items(envelope.get("results") or [])]
        traversed = envelope.get("traversed")
        if traversed:
            parts.append("Related memories reached by following graph links from "
                         "the results above (not relevance-ranked):")
            parts.append(_render_items(traversed))
        return "\n\n".join(parts).strip()

    # bare traverse-style envelope
    if "nodes" in envelope:
        return ("The following memories were retrieved:\n\n"
                + _render_items(envelope.get("nodes") or []))

    # unknown shape: never drop content -- fall back to a readable dump.
    return json.dumps(envelope, indent=1, default=str)

# The reader's governing instruction is the SHIPPED skill, not a prompt authored
# for this harness -- this is the honest deployment config, and the file's hash
# goes in the manifest so "we used the shipped skill" is verifiable against the
# committed file. skills/retrieval.md is deliberately NOT loaded: it tells an
# agent how to issue search/traverse/briefing calls, and the reader issues none
# -- retrieval is the strategy's job and the reader receives a fixed envelope --
# so that guidance is inapplicable to the reader's path.
SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
READER_SKILL = "answer-discipline.md"

# The one thing the skill does not carry: the harness's answer FORMAT. The skill
# says "when memory lacks the answer, say so plainly"; the harness needs that in
# a machine-scorable form (the exact INSUFFICIENT token) plus conciseness for the
# judge. This contract is general -- it is the scoring format for ANY abstention
# benchmark, names no LoCoMo category or question shape -- and it is recorded
# verbatim in the manifest so the full reader instruction is disclosed, not just
# the skill half.
# The reader's inference stance (skills/answer-discipline.md §"The inference
# stance"). The skill defines both `strict` (decline when memory lacks the
# answer) and `grounded` (declared, sourced inference allowed); the DEPLOYMENT
# selects which. `grounded` is the default here because the personal-memory pack
# recommends it (the skill's own default for this domain) -- and it is a run-level
# knob, recorded in the manifest, not a prompt authored for the benchmark.
READER_STANCES = ("strict", "grounded")
READER_DEFAULT_STANCE = "grounded"

_OUTPUT_BASE = (
    "## Output format for this evaluation\n\n"
    "You are being evaluated, so you cannot ask the user or issue further tool "
    "calls -- answer only from the memory already provided in this message. "
    "Answer in one concise sentence, or a bare phrase where the question asks for "
    "a name, a date, a number, or a place. No preamble, no restating the question."
)


def _stance_directive(stance: str) -> str:
    if stance == "grounded":
        return (
            "## Active inference stance for this evaluation: `grounded`\n\n"
            "Operate under the grounded stance defined above. When the memory does "
            "not state the answer outright but supports a reasonable, sourced "
            "inference, give that inference concisely as your answer (you need not "
            "spell out the citation for this evaluation). The hard invariant still "
            "holds: never assert as fact what memory does not support. Decline only "
            "when the memory gives no basis to answer OR to infer."
        )
    return (
        "## Active inference stance for this evaluation: `strict`\n\n"
        "Operate under the strict stance defined above: do not infer. If the memory "
        "does not state the answer, decline."
    )


def _output_contract(stance: str) -> str:
    if stance == "grounded":
        return _OUTPUT_BASE + (
            " If the memory neither states the answer nor supports a grounded "
            "inference, reply with exactly the single word INSUFFICIENT."
        )
    return _OUTPUT_BASE + (
        " Following the discipline above: if the provided memory does not contain "
        "the answer, reply with exactly the single word INSUFFICIENT."
    )


# Kept for back-compat / external reference: the default (grounded) contract.
READER_OUTPUT_CONTRACT = _output_contract(READER_DEFAULT_STANCE)


def build_reader_system(stance: str = READER_DEFAULT_STANCE) -> tuple[str, dict]:
    """The reader's full system instruction, and what the manifest records about it.

    The system is the shipped skill, then the active inference stance directive,
    then the output-format contract -- the stance and contract are selected by the
    `stance` knob (`strict` | `grounded`). The manifest carries the skill path and
    sha256 (verifiable against the committed file), the active stance, and the
    contract verbatim.
    """
    if stance not in READER_STANCES:
        raise ValueError(f"unknown reader stance {stance!r}; choose {READER_STANCES}")
    skill_path = SKILLS_DIR / READER_SKILL
    skill_text = skill_path.read_text(encoding="utf-8")
    skill_sha = hashlib.sha256(skill_text.encode("utf-8")).hexdigest()
    contract = _output_contract(stance)
    system = (f"{skill_text}\n\n---\n\n{_stance_directive(stance)}"
              f"\n\n---\n\n{contract}")
    manifest = {
        "governing_skill": f"skills/{READER_SKILL}",
        "governing_skill_sha256": f"sha256:{skill_sha}",
        "inference_stance": stance,
        "output_contract": contract,
        "reader_input_shape": RENDER_FORMAT_VERSION,
        "note": (
            "The reader's governing instruction is the shipped skill file, loaded "
            f"by path (sha256 verifiable against the committed file), applied under "
            f"the '{stance}' inference stance the skill defines. A general "
            "output-format contract (INSUFFICIENT token + conciseness) is appended "
            "so answers are machine-scorable. skills/retrieval.md is not loaded: "
            "the reader issues no retrieval calls, so that guidance does not apply. "
            f"The retrieval envelope is presented to the reader via render_envelope "
            f"('{RENDER_FORMAT_VERSION}': clean rank-ordered prose, scaffolding "
            "omitted, content faithful); envelope_bytes/sha256 still measure the "
            "canonical json envelope (the retrieval payload), of which the rendered "
            "input is a deterministic function."
        ),
    }
    return system, manifest

# The exact token the prompt instructs the reader to emit when the memory does
# not contain the answer. LoCoMo's adversarial category and LongMemEval's
# abstention set are both graded on this, so it is a constant rather than a
# string literal repeated across modules.
INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class Answer:
    """One reader call, and everything the result row needs from it."""

    question_id: str
    text: str
    envelope_bytes: int
    envelope_sha256: str
    seconds: float
    model: str = ""
    # Reported by the provider, kept as a diagnostic and NEVER promoted to the
    # headline token figure -- on the CLI route these numbers include the CLI's
    # own overhead. See the module docstring.
    provider_input_tokens: int = 0
    provider_output_tokens: int = 0
    error: str = ""

    @property
    def abstained(self) -> bool:
        return is_abstention(self.text)

    def as_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "answer": self.text,
            "envelope_bytes": self.envelope_bytes,
            "envelope_sha256": self.envelope_sha256,
            "reader_seconds": round(self.seconds, 4),
            "reader_model": self.model,
            "provider_input_tokens": self.provider_input_tokens,
            "provider_output_tokens": self.provider_output_tokens,
            "abstained": self.abstained,
            "error": self.error,
        }


def is_abstention(text: str) -> bool:
    """Did the reader decline?

    Matched on the instructed token appearing in a short reply rather than on
    exact equality: a model that obeys the instruction but adds a full stop has
    still declined, and scoring that as a wrong answer would penalise formatting
    rather than memory. The length bound stops a long answer that merely
    *mentions* insufficiency from being read as a refusal.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    return INSUFFICIENT in stripped.upper() and len(stripped) <= len(INSUFFICIENT) + 40


class Reader:
    """One fixed instruction, one call per question. Never branches on anything.

    The system instruction is the shipped `answer-discipline.md` skill plus a
    general output-format contract (see `build_reader_system`). `effort` is a
    fixed, disclosed ship default of `medium` -- raised from the earlier `low`
    because reading a retrieval envelope and deciding whether it answers the
    question is the reasoning step the whole pipeline rests on. It is a uniform
    setting, not tuned per result, and it is recorded in the manifest.
    """

    def __init__(self, client, *, effort: str = "medium", max_tokens: int = 1000,
                 stance: str = READER_DEFAULT_STANCE) -> None:
        self.client = client
        self.stance = stance
        self.system, self.system_manifest = build_reader_system(stance)
        self.effort = effort
        self.max_tokens = max_tokens

    @property
    def prompt_hash(self) -> str:
        """SHA-256 of the assembled system instruction (skill + contract)."""
        return f"sha256:{hashlib.sha256(self.system.encode('utf-8')).hexdigest()[:16]}"

    def read(self, question: Question, retrieval: Retrieval) -> Answer:
        # The reader receives a rendered presentation of the envelope; the payload
        # bytes/hash stay on the canonical json envelope (retrieval output) so the
        # cost metric is comparable across strategies/runs and the hash remains a
        # stable provenance tie. The rendered string is a deterministic function
        # of the hashed envelope -- see the module docstring.
        blob = json.dumps(retrieval.envelope)
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        memory = render_envelope(retrieval.envelope)
        user = f"Memory:\n{memory}\n\nQuestion: {question.text}"

        try:
            resp = self.client.complete(
                self.system, user, max_tokens=self.max_tokens, effort=self.effort
            )
        except QuotaExhausted:
            # A provider usage cap is NOT "the system produced no answer" -- it is
            # the harness's provider being unavailable, and grading it as a wrong
            # answer would blame Engraphy for a rate limit. Re-raised so the answer
            # phase stops cleanly and a resume RE-ANSWERS the question, exactly as
            # the judge already re-grades. (Learned the hard way: a mid-answer
            # usage limit once checkpointed 1,189 empty answers as permanent
            # wrong answers, producing two bogus 0% arms.)
            raise
        except LLMError as exc:
            # A non-quota reader failure is recorded as an errored answer. The
            # caller does NOT checkpoint it (see run.phase_answer), so a resume
            # re-answers it rather than banking an empty answer as a wrong one.
            return Answer(
                question_id=question.question_id,
                text="",
                envelope_bytes=len(blob.encode("utf-8")),
                envelope_sha256=digest,
                seconds=0.0,
                error=f"{type(exc).__name__}: {exc}"[:400],
            )

        return Answer(
            question_id=question.question_id,
            text=(resp.text or "").strip(),
            envelope_bytes=len(blob.encode("utf-8")),
            envelope_sha256=digest,
            seconds=resp.seconds,
            model=resp.model,
            provider_input_tokens=resp.input_tokens,
            provider_output_tokens=resp.output_tokens,
        )
