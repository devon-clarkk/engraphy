"""The corpus intermediate representation -- shim boundary 1 (design/09
§Interface 1).

Every benchmark lowers to these types and the shared core sees nothing else.
The point is not convenience, it is auditability: if the ingest path cannot tell
LoCoMo from LongMemEval from a synthetic duplicate stream, then no benchmark can
receive special treatment, and "we tuned it for the suite that flatters us" stops
being expressible in code.

Two invariants the rest of the harness relies on:

1. **Category strings pass through verbatim.** A suite's own category name is
   carried unchanged into the report. The harness never renames a category into
   a friendlier bucket, never merges two categories, and never invents one. A
   loader that maps an integer code to a name (LoCoMo does) must do it with an
   explicit, commented table and fail loudly on an unknown code.
2. **A haystack is the isolation unit.** One haystack becomes one scope with its
   own dedup candidate set (design/09 §Isolation). Questions name their haystack
   and nothing else; there is no cross-haystack question, and `validate()`
   refuses a corpus that implies one.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "BenchmarkLoader",
    "Corpus",
    "CorpusError",
    "Haystack",
    "Question",
    "Session",
    "Turn",
]


class CorpusError(ValueError):
    """A corpus (or the raw file behind it) is malformed.

    Loaders raise this rather than returning a partial corpus. A benchmark that
    silently drops 3% of its questions produces a number that cannot be compared
    to a published one, and the drop is invisible in the result -- so every
    parse failure is loud, names the offending record, and stops the run.
    """


# Speaker labels are normalized to these two where a suite distinguishes the
# roles (LongMemEval does). Suites whose dialogues are between two named humans
# (LoCoMo) keep the names verbatim -- flattening "Caroline"/"Melanie" to
# user/assistant would destroy exactly the attribution that its single-hop
# questions ask about ("what did Melanie say about...").
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Turn:
    """One utterance. `turn_id` is the suite's own identifier where it has one
    (LoCoMo's `dia_id`), because question evidence is expressed in those ids and
    we want provenance to survive into the store."""

    speaker: str
    text: str
    turn_id: str | None = None
    timestamp: str | None = None

    def __post_init__(self) -> None:
        if not self.speaker:
            raise CorpusError("Turn.speaker is empty")
        if not self.text or not self.text.strip():
            raise CorpusError(f"Turn.text is empty (turn_id={self.turn_id!r})")


@dataclass(frozen=True, slots=True)
class Session:
    """One conversational session. Order within `turns` is chronological and
    load-bearing: knowledge-update questions are only answerable if the store
    saw the superseded fact before the one that replaces it."""

    session_id: str
    turns: tuple[Turn, ...]
    timestamp: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id:
            raise CorpusError("Session.session_id is empty")
        if not self.turns:
            raise CorpusError(f"Session {self.session_id!r} has no turns")


@dataclass(frozen=True, slots=True)
class Haystack:
    """The isolation unit -- one haystack becomes one scope (design/09
    §Isolation). Sessions are in chronological order.

    LoCoMo has ~10 haystacks each carrying ~150 questions; LongMemEval has one
    haystack *per question* (500 of them, each with its own session history).
    Both shapes are expressible here, which is the whole reason this type exists
    separately from `Session`.
    """

    haystack_id: str
    sessions: tuple[Session, ...]

    def __post_init__(self) -> None:
        if not self.haystack_id:
            raise CorpusError("Haystack.haystack_id is empty")
        if not self.sessions:
            raise CorpusError(f"Haystack {self.haystack_id!r} has no sessions")
        seen: set[str] = set()
        for s in self.sessions:
            if s.session_id in seen:
                raise CorpusError(
                    f"Haystack {self.haystack_id!r} repeats session id {s.session_id!r}"
                )
            seen.add(s.session_id)

    @property
    def turn_count(self) -> int:
        return sum(len(s.turns) for s in self.sessions)


@dataclass(frozen=True, slots=True)
class Question:
    """One scored question.

    `category` is the suite's own string, verbatim. `abstain_expected` marks the
    questions whose correct answer is a refusal (LongMemEval's `_abs` set): they
    are graded on whether the system correctly declined, which is a different
    question from "is this answer right" and is why `Scorer` is a seam.
    """

    question_id: str
    haystack_id: str
    text: str
    category: str
    gold_answer: str
    evidence: tuple[str, ...] = ()
    abstain_expected: bool = False

    def __post_init__(self) -> None:
        if not self.question_id:
            raise CorpusError("Question.question_id is empty")
        if not self.text or not self.text.strip():
            raise CorpusError(f"Question {self.question_id!r} has empty text")
        if not self.category:
            raise CorpusError(f"Question {self.question_id!r} has empty category")
        # An abstention question legitimately has no gold answer; every other
        # kind must have one or it cannot be judged.
        if not self.abstain_expected and not str(self.gold_answer).strip():
            raise CorpusError(f"Question {self.question_id!r} has empty gold_answer")


@dataclass(frozen=True, slots=True)
class Corpus:
    """A whole benchmark, lowered. `notes` carries loader-side observations that
    belong in the run manifest -- dropped image turns, coerced answer types,
    anything the loader had to decide. They are surfaced, never swallowed."""

    name: str
    haystacks: tuple[Haystack, ...]
    questions: tuple[Question, ...]
    notes: dict = field(default_factory=dict)

    def validate(self) -> Corpus:
        """Structural checks a loader cannot do per-record. Returns self so
        loaders can `return Corpus(...).validate()`."""
        if not self.haystacks:
            raise CorpusError(f"Corpus {self.name!r} has no haystacks")
        if not self.questions:
            raise CorpusError(f"Corpus {self.name!r} has no questions")

        ids = {h.haystack_id for h in self.haystacks}
        if len(ids) != len(self.haystacks):
            raise CorpusError(f"Corpus {self.name!r} has duplicate haystack ids")

        seen_q: set[str] = set()
        for q in self.questions:
            if q.question_id in seen_q:
                raise CorpusError(f"Corpus {self.name!r} repeats question id {q.question_id!r}")
            seen_q.add(q.question_id)
            if q.haystack_id not in ids:
                # The isolation model has no answer for a question that spans
                # haystacks -- it would have to search two scopes at once, which
                # is exactly the cross-contamination the scoping prevents.
                raise CorpusError(
                    f"Question {q.question_id!r} names unknown haystack {q.haystack_id!r}"
                )
        return self

    @property
    def categories(self) -> tuple[str, ...]:
        """Distinct categories, sorted -- the report's row order."""
        return tuple(sorted({q.category for q in self.questions}))

    def questions_for(self, haystack_id: str) -> tuple[Question, ...]:
        return tuple(q for q in self.questions if q.haystack_id == haystack_id)

    def subset(self, limit: int, *, seed: int = 0) -> Corpus:
        """A deterministic sample, stratified by category, for smoke runs and
        the HTTP cross-check subset.

        Deterministic by construction (sorted by a hash of the question id, not
        by `random`) so that "the 50-question subset" means the same 50
        questions on every machine and in every rerun. Haystacks are filtered to
        those the sample actually needs, so a subset run does not pay to ingest
        the whole corpus.
        """
        if limit <= 0:
            raise CorpusError("subset limit must be positive")

        by_cat: dict[str, list[Question]] = {}
        for q in self.questions:
            by_cat.setdefault(q.category, []).append(q)
        for cat_questions in by_cat.values():
            cat_questions.sort(key=lambda q: hashlib.sha256(f"{seed}:{q.question_id}".encode()).digest())

        picked: list[Question] = []
        cats = sorted(by_cat)
        i = 0
        # Round-robin across categories so a small subset still touches every
        # category rather than over-sampling the largest one.
        while len(picked) < limit and any(by_cat[c] for c in cats):
            cat = cats[i % len(cats)]
            if by_cat[cat]:
                picked.append(by_cat[cat].pop(0))
            i += 1

        keep = {q.haystack_id for q in picked}
        return Corpus(
            name=f"{self.name}[subset:{limit}:seed{seed}]",
            haystacks=tuple(h for h in self.haystacks if h.haystack_id in keep),
            questions=tuple(picked),
            notes={**self.notes, "subset_of": self.name, "subset_seed": seed},
        ).validate()

    def stats(self) -> dict:
        """The numbers that go in the manifest so a reader can confirm the
        loader saw the dataset they think it saw."""
        per_cat: dict[str, int] = {}
        for q in self.questions:
            per_cat[q.category] = per_cat.get(q.category, 0) + 1
        return {
            "name": self.name,
            "haystacks": len(self.haystacks),
            "sessions": sum(len(h.sessions) for h in self.haystacks),
            "turns": sum(h.turn_count for h in self.haystacks),
            "questions": len(self.questions),
            "questions_per_category": dict(sorted(per_cat.items())),
            "abstain_questions": sum(1 for q in self.questions if q.abstain_expected),
            "notes": self.notes,
        }


@runtime_checkable
class BenchmarkLoader(Protocol):
    """Shim boundary 1. The *entire* integration surface for a new benchmark:
    implement this, and the shared core does the rest. Adding BEAM later should
    be one file (design/09 §Deferred work)."""

    name: str

    def load(self, path: Path) -> Corpus: ...


def dataset_digest(path: Path) -> str:
    """SHA-256 of the raw dataset file, for the run manifest.

    A published accuracy number is meaningless without knowing which file
    produced it -- LoCoMo in particular circulates in several revisions.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def load_json(path: Path) -> object:
    """Read a dataset file, with a loud error naming the file on bad JSON."""
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise CorpusError(f"dataset file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CorpusError(f"dataset file {path} is not valid JSON: {exc}") from exc


def require(obj: object, key: str, *, where: str) -> object:
    """Strict field access. design/09 §Open questions: the loaders are written
    against the published schemas and must fail loudly on an unrecognized shape
    rather than silently mis-parsing -- a loader that quietly treats a missing
    `answer` as "" would report a plausible-looking accuracy against nothing."""
    if not isinstance(obj, dict):
        raise CorpusError(f"{where}: expected an object, got {type(obj).__name__}")
    if key not in obj:
        raise CorpusError(f"{where}: missing required field {key!r} (have: {sorted(obj)})")
    return obj[key]


def as_text(value: object, *, where: str) -> str:
    """Coerce a scalar answer to text.

    LoCoMo answers are sometimes numbers or dates rather than strings; the judge
    compares text. Coercion is recorded by the caller in `Corpus.notes` so it
    appears in the manifest instead of being an invisible loader liberty.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        raise CorpusError(f"{where}: unexpected boolean answer")
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Iterable) and not isinstance(value, (bytes, dict)):
        parts = [as_text(v, where=where) for v in value]
        return "; ".join(parts)
    raise CorpusError(f"{where}: cannot coerce {type(value).__name__} to answer text")
