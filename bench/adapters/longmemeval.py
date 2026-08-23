"""LongMemEval loader -- shim (design/09 §Interface 1).

500 questions across six categories, and the more carefully constructed of the
two public suites. Its `knowledge-update` category maps directly onto Engraphy's
supersede chains, which is why it is the second benchmark rather than the
fourth.

Raw shape (top level is a list, one object per question)::

    {
      "question_id": "gpt4_1234",          # a trailing "_abs" marks abstention
      "question_type": "knowledge-update",
      "question": "...",
      "answer": "...",
      "question_date": "2023/05/20 (Sat) 02:33",
      "haystack_session_ids": ["s1", "s2", ...],
      "haystack_dates": ["2023/05/01 ...", ...],
      "haystack_sessions": [[{"role": "user", "content": "...",
                              "has_answer": true}, ...], ...],
      "answer_session_ids": ["s2"]
    }

**Each question carries its own haystack.** That is the structural difference
from LoCoMo and the reason `Haystack` is a separate type: a LongMemEval run
provisions one scope per question (500 of them) and ingests each history
independently. It is expensive and it is correct -- sharing a scope across
questions would let one question's sessions become dedup candidates for
another's, contaminating both the dedup and the accuracy numbers.

`has_answer` is **not** propagated into the store. It is the dataset's own
annotation of which turns contain the evidence; writing it through would hand
retrieval a labelled needle and make every accuracy number meaningless. It is
read only to populate `Question.evidence` for post-hoc analysis.
"""

from __future__ import annotations

from pathlib import Path

from bench.core.corpus import (
    ROLE_ASSISTANT,
    ROLE_USER,
    Corpus,
    CorpusError,
    Haystack,
    Question,
    Session,
    Turn,
    as_text,
    load_json,
    require,
)

__all__ = ["QUESTION_TYPES", "LongMemEvalLoader"]

# The six published categories, carried through verbatim as category strings.
# Listed explicitly so an unrecognized type is a loud failure rather than a
# silent new bucket appearing in a report.
QUESTION_TYPES: frozenset[str] = frozenset(
    {
        "single-session-user",
        "single-session-assistant",
        "single-session-preference",
        "multi-session",
        "knowledge-update",
        "temporal-reasoning",
    }
)

_ABSTAIN_SUFFIX = "_abs"


class LongMemEvalLoader:
    """`BenchmarkLoader` for LongMemEval. One question -> one haystack."""

    name = "longmemeval"

    def load(self, path: Path) -> Corpus:
        raw = load_json(path)
        if not isinstance(raw, list):
            raise CorpusError(f"{path}: expected a list of questions, got {type(raw).__name__}")

        haystacks: list[Haystack] = []
        questions: list[Question] = []
        unknown_roles: set[str] = set()

        for i, item in enumerate(raw):
            where = f"{path.name}[{i}]"
            question_id = str(require(item, "question_id", where=where))
            qtype = str(require(item, "question_type", where=where))
            if qtype not in QUESTION_TYPES:
                raise CorpusError(
                    f"{where}: unknown question_type {qtype!r}. Known: {sorted(QUESTION_TYPES)}. "
                    "Update QUESTION_TYPES deliberately rather than letting a new category "
                    "appear unnoticed in the report."
                )

            sessions, evidence, roles = _parse_haystack(item, where=where)
            unknown_roles |= roles
            haystacks.append(Haystack(haystack_id=question_id, sessions=sessions))

            abstain = question_id.endswith(_ABSTAIN_SUFFIX)
            raw_answer = item.get("answer", "")
            questions.append(
                Question(
                    question_id=question_id,
                    haystack_id=question_id,
                    text=str(require(item, "question", where=where)),
                    # Abstention questions keep their base category and are
                    # flagged, rather than being moved to a category of their
                    # own -- the suite scores them within their type.
                    category=qtype,
                    gold_answer=as_text(raw_answer, where=where) if raw_answer != "" else "",
                    evidence=evidence,
                    abstain_expected=abstain,
                )
            )

        return Corpus(
            name="longmemeval",
            haystacks=tuple(haystacks),
            questions=tuple(questions),
            notes={
                "source_file": path.name,
                "has_answer_propagated_to_store": False,
                "unknown_roles_preserved_verbatim": sorted(unknown_roles),
            },
        ).validate()


def _parse_haystack(item: dict, *, where: str) -> tuple[tuple[Session, ...], tuple[str, ...], set[str]]:
    sessions_raw = require(item, "haystack_sessions", where=where)
    if not isinstance(sessions_raw, list):
        raise CorpusError(f"{where}: 'haystack_sessions' is not a list")

    session_ids = item.get("haystack_session_ids") or []
    dates = item.get("haystack_dates") or []
    if session_ids and len(session_ids) != len(sessions_raw):
        raise CorpusError(
            f"{where}: haystack_session_ids has {len(session_ids)} entries but "
            f"haystack_sessions has {len(sessions_raw)} -- the file is internally inconsistent"
        )

    sessions: list[Session] = []
    evidence: list[str] = []
    unknown_roles: set[str] = set()

    for n, raw_turns in enumerate(sessions_raw):
        sw = f"{where}.haystack_sessions[{n}]"
        if not isinstance(raw_turns, list):
            raise CorpusError(f"{sw}: expected a list of turns")
        session_id = str(session_ids[n]) if n < len(session_ids) else f"session_{n}"
        timestamp = str(dates[n]) if n < len(dates) else None

        turns: list[Turn] = []
        for k, t in enumerate(raw_turns):
            tw = f"{sw}[{k}]"
            content = require(t, "content", where=tw)
            if not isinstance(content, str) or not content.strip():
                raise CorpusError(f"{tw}: 'content' is empty or not a string")
            role = str(require(t, "role", where=tw))
            if role not in (ROLE_USER, ROLE_ASSISTANT):
                # Preserved verbatim and recorded, not coerced: silently
                # relabelling an unexpected role would misattribute utterances,
                # and misattribution is what half these questions test.
                unknown_roles.add(role)
            turn_id = f"{session_id}:{k}"
            turns.append(Turn(speaker=role, text=content, turn_id=turn_id, timestamp=timestamp))
            if t.get("has_answer"):
                evidence.append(turn_id)

        if not turns:
            raise CorpusError(f"{sw}: session has no turns")
        sessions.append(Session(session_id=session_id, turns=tuple(turns), timestamp=timestamp))

    if not sessions:
        raise CorpusError(f"{where}: 'haystack_sessions' is empty")
    return tuple(sessions), tuple(evidence), unknown_roles
