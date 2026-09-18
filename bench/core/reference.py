"""The reference harness's LoCoMo conventions, for a figure comparable to published ones.

Engraphy's LoCoMo figure is measured under its own strict conventions: the reader
may decline when memory lacks the answer, and the judge requires every item of a
multi-item gold answer. Published LoCoMo figures for other memory systems come
from a harness with different conventions. This module reproduces that harness's
reader and judge conventions from its own source, so a second figure can be
reported beside the strict one, labelled as measured under the reference harness
conventions. It never replaces the strict figure.

Source, pinned: `mem0ai/memory-benchmarks`, `benchmarks/locomo/prompts.py` and
`benchmarks/locomo/run.py` at commit `4b61c5d31b9c668a12b4f5e78064248a02c82d2b`,
Apache-2.0. The answer prompt, the judge prompt and the judge system prompt are
vendored byte for byte under `bench/prompts/reference/`, written out by executing
the upstream module rather than retyped, and their hashes are recorded.

Reproduced from the reference source:

- the answer prompt, which tells the reader to commit to an answer and never to
  decline, with its final answer taken from after the last `ANSWER:`;
- at most 200 memories, presented oldest first, with no ranks or scores;
- the reference date: the date string of the conversation's latest session;
- the judge prompt in its default form, without evidence: at least one gold item
  suffices, dates within 14 days and durations within 50% count, extra detail is
  never penalised, and the verdict is a `CORRECT` or `WRONG` label;
- open-domain gold answers cut at the first semicolon before judging;
- categories 1 to 4 scored, adversarial excluded;
- one judge pass per answer.

What cannot be reproduced, recorded in every manifest as a difference:

- the memories are Engraphy's, retrieved by Engraphy's search at the run's width;
- the reference ingests each session with its timestamp, so each memory line it
  shows carries a session date. An Engraphy memory records its dates in its own
  text and attributes, and its `created_at` is the ingest time, so each line uses
  the reference's own form for a memory without a date;
- the reader and judge are the run's models, not the reference defaults.
"""

from __future__ import annotations

import datetime
import hashlib
import pathlib
import time
from dataclasses import dataclass

from bench.core.corpus import Haystack, Question
from bench.core.judge import Verdict
from bench.core.llm import LLMError
from bench.core.providers import QuotaExhausted

__all__ = [
    "ANSWERER_MEMORY_LIMIT",
    "OPEN_DOMAIN",
    "REFERENCE_SOURCE",
    "ReferenceAnswer",
    "ReferenceJudge",
    "ReferenceReader",
    "answer_prompt",
    "conventions_manifest",
    "extract_answer",
    "gold_for_judge",
    "reference_date",
    "render_memories",
]

REFERENCE_SOURCE = {
    "repository": "https://github.com/mem0ai/memory-benchmarks",
    "commit": "4b61c5d31b9c668a12b4f5e78064248a02c82d2b",
    "files": ["benchmarks/locomo/prompts.py", "benchmarks/locomo/run.py"],
    "license": "Apache-2.0",
}

# prompts.py ANSWERER_MEMORY_LIMIT.
ANSWERER_MEMORY_LIMIT = 200
# The loader's name for LoCoMo category 3, whose gold the reference cuts at ";".
OPEN_DOMAIN = "open-domain-knowledge"

PROMPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "prompts" / "reference"


def _load(name: str) -> str:
    # The working tree may carry CRLF (core.autocrlf); the upstream strings are LF.
    return (PROMPT_DIR / name).read_bytes().decode("utf-8").replace("\r\n", "\n")


ANSWER_PROMPT = _load("answer.md")
JUDGE_PROMPT = _load("judge.md")
JUDGE_SYSTEM_PROMPT = _load("judge_system.md")


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# The reference parses the judge's reply as a JSON object and reads `label`.
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"reasoning": {"type": "string"}, "label": {"type": "string"}},
    "required": ["reasoning", "label"],
}


def _parse_locomo_date(value: str | None) -> datetime.datetime | None:
    """run.py parse_locomo_date: '1:56 pm on 8 May, 2023'."""
    for fmt in ("%I:%M %p on %d %B, %Y", "%I:%M %p on %d %b, %Y"):
        try:
            return datetime.datetime.strptime(value or "", fmt).replace(tzinfo=datetime.UTC)
        except (ValueError, TypeError):
            continue
    return None


def reference_date(haystack: Haystack) -> str | None:
    """run.py: the date string of the last session after get_sorted_sessions.

    Sessions sort by parsed date; one whose date does not parse sorts after every
    dated one, by session number, exactly as the reference orders them.
    """
    def key(item: tuple[int, str | None]) -> tuple:
        number, stamp = item
        parsed = _parse_locomo_date(stamp)
        if parsed:
            return (0, parsed)
        return (1, datetime.datetime(2000, 1, number, tzinfo=datetime.UTC))

    numbered = []
    for i, session in enumerate(haystack.sessions, start=1):
        digits = "".join(ch for ch in str(session.session_id) if ch.isdigit())
        numbered.append((int(digits) if digits else i, session.timestamp))
    if not numbered:
        return None
    # sorted(...)[-1] keeps the later of two equal keys; the index reproduces that.
    return max(enumerate(numbered), key=lambda p: (key(p[1]), p[0]))[1][1]


def _memory_text(node: dict) -> str:
    """One Engraphy memory as one reference memory line: title, body and attributes.

    The reference shows one memory per line, so internal line breaks become spaces.
    Nothing is dropped: attributes can carry the date a fact occurred on.
    """
    title = " ".join(str(node.get("title") or "").split())
    body = " ".join(str(node.get("body") or "").split())
    text = title if not body or body == title else f"{title}: {body}" if title else body
    attrs = node.get("attrs") or {}
    pairs = [f"{k}: {v}" for k, v in sorted(attrs.items()) if v not in (None, "", [], {})]
    if pairs:
        text = f"{text} ({'; '.join(pairs)})"
    return text


def _nodes(envelope: dict) -> list[dict]:
    items = list(envelope.get("results") or []) + list(envelope.get("traversed") or [])
    return [item.get("node", item) if isinstance(item, dict) else item for item in items]


def render_memories(envelope: dict) -> str:
    """prompts.get_answer_generation_prompt's memory block, over Engraphy memories.

    Top ANSWERER_MEMORY_LIMIT, sorted by `created_at` oldest first, no rank numbers
    or scores. An Engraphy `created_at` is when the memory was written, not when the
    conversation happened, so every line takes the reference's no-date form.
    """
    nodes = _nodes(envelope)[:ANSWERER_MEMORY_LIMIT]
    if not nodes:
        return "(No relevant memories found)"
    ordered = sorted(nodes, key=lambda n: str(n.get("created_at") or ""))
    lines = ["The following memories are presented in chronological order (oldest to newest).",
             ""]
    lines += [f"(unknown date) {_memory_text(n)}" for n in ordered]
    return "\n".join(lines)


def answer_prompt(question: Question, envelope: dict, ref_date: str | None) -> str:
    return ANSWER_PROMPT.format(
        memories=render_memories(envelope),
        question=question.text,
        # prompts.py: "Defaults to '2023' if not provided."
        reference_date=ref_date if ref_date is not None else "2023",
    )


def extract_answer(text: str) -> str:
    """run.py: the text after the last `ANSWER:`, else the whole reply."""
    if "ANSWER:" in text:
        return text.rpartition("ANSWER:")[2].strip()
    return text


def gold_for_judge(question: Question) -> str:
    """prompts.preprocess_answer: category 3 gold is cut at the first semicolon."""
    gold = str(question.gold_answer)
    if question.category == OPEN_DOMAIN and ";" in gold:
        return gold.partition(";")[0].strip()
    return gold


@dataclass(frozen=True, slots=True)
class ReferenceAnswer:
    question_id: str
    text: str
    raw: str
    seconds: float
    model: str = ""
    error: str = ""


class ReferenceReader:
    """The reference answerer: its prompt as the user turn, an empty system prompt."""

    def __init__(self, client) -> None:
        self.client = client

    def read(self, question: Question, envelope: dict, ref_date: str | None) -> ReferenceAnswer:
        started = time.perf_counter()
        try:
            resp = self.client.complete("", answer_prompt(question, envelope, ref_date))
        except QuotaExhausted:
            raise
        except LLMError as exc:
            return ReferenceAnswer(question.question_id, "", "", 0.0,
                                   error=f"{type(exc).__name__}: {exc}"[:400])
        raw = (resp.text or "").strip()
        return ReferenceAnswer(question.question_id, extract_answer(raw), raw,
                               resp.seconds or (time.perf_counter() - started), resp.model)


class ReferenceJudge:
    """The reference judge: one structured call, correct iff the label is CORRECT."""

    def __init__(self, client) -> None:
        self.client = client

    def grade(self, question: Question, answer: str) -> Verdict:
        user = JUDGE_PROMPT.format(question=question.text, answer=gold_for_judge(question),
                                   response=answer)
        try:
            resp = self.client.complete(JUDGE_SYSTEM_PROMPT, user, schema=JUDGE_SCHEMA)
        except QuotaExhausted:
            raise
        except LLMError as exc:
            return Verdict(correct=False, graded_by="judge_error",
                           error=f"{type(exc).__name__}: {exc}"[:300])
        data = resp.data if isinstance(resp.data, dict) else {}
        label = str(data.get("label", "")).upper()
        return Verdict(correct=label == "CORRECT", reason=str(data.get("reasoning") or ""),
                       graded_by="reference_judge", model=resp.model, seconds=resp.seconds)

    def grade_majority(self, question: Question, answer: str, passes: int = 1) -> Verdict:
        if passes <= 1:
            return self.grade(question, answer)
        votes = []
        for _ in range(passes):
            v = self.grade(question, answer)
            if v.graded_by == "judge_error":
                return v
            votes.append(v)
        yes = sum(v.correct for v in votes)
        correct = yes > len(votes) / 2
        winner = next(v for v in votes if v.correct == correct)
        return Verdict(correct=correct,
                       reason=f"[best-of-{len(votes)}: {yes} correct / {len(votes) - yes} wrong] "
                              f"{winner.reason}",
                       graded_by="reference_judge", model=winner.model,
                       seconds=sum(v.seconds for v in votes))


def conventions_manifest(*, judge_passes: int) -> dict:
    """Everything a reader of the matched-convention figure needs to check it."""
    return {
        "label": "measured under the reference harness conventions, for comparability",
        "source": REFERENCE_SOURCE,
        "prompts": {
            "answer": {"path": "bench/prompts/reference/answer.md", "sha256": _sha(ANSWER_PROMPT)},
            "judge": {"path": "bench/prompts/reference/judge.md", "sha256": _sha(JUDGE_PROMPT)},
            "judge_system": {"path": "bench/prompts/reference/judge_system.md",
                             "sha256": _sha(JUDGE_SYSTEM_PROMPT)},
        },
        "reproduced": [
            ("answer prompt verbatim: commit to an answer, never decline; answer taken "
             "after the last 'ANSWER:'"),
            f"at most {ANSWERER_MEMORY_LIMIT} memories, oldest first, no ranks or scores",
            "reference date: the latest session's date string",
            ("judge prompt verbatim, without evidence (the reference default): at least one "
             "gold item suffices, dates within 14 days, durations within 50%"),
            "open-domain gold cut at the first semicolon",
            "categories 1-4 scored; adversarial excluded",
            f"{judge_passes} judge pass(es) per answer (the reference uses 1)",
        ],
        "differences": [
            ("memories are Engraphy's, from Engraphy's search at the run's retrieval width; "
             "the reference retrieves up to 200 from its own store"),
            ("each reference memory line carries its session date; Engraphy memories keep "
             "dates in their text and attributes and created_at is ingest time, so each line "
             "uses the reference's no-date form '(unknown date)'"),
            "reader and judge models are the run's, not the reference defaults",
            "an Engraphy memory is rendered as one line: title, body and attributes",
        ],
    }
