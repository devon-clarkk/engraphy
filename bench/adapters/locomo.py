"""LoCoMo loader -- shim (design/09 §Interface 1).

LoCoMo is the lead benchmark: 1,540 questions over ten long multi-session
dialogues between two named speakers, and the only public suite whose multi-hop
category rewards graph traversal.

Raw shape (one object per conversation, top level is a list)::

    {
      "sample_id": "conv-26",
      "conversation": {
        "speaker_a": "Caroline",
        "speaker_b": "Melanie",
        "session_1_date_time": "1:56 pm on 8 May, 2023",
        "session_1": [{"speaker": "Caroline", "dia_id": "D1:1", "text": "..."}, ...],
        "session_2_date_time": "...",
        "session_2": [...]
      },
      "qa": [{"question": "...", "answer": "...", "evidence": ["D1:2"], "category": 1}]
    }

Two things this loader deliberately does NOT do:

* **It does not flatten speaker names to user/assistant.** LoCoMo's dialogues are
  between two named humans and its single-hop questions ask which of them said
  or did a thing; collapsing the names would destroy the attribution the
  questions are scored on.
* **It does not read the dataset's `event_summary` / `observation` /
  `session_summary` fields.** Those are pre-digested annotations shipped
  alongside the dialogue. Ingesting them would be feeding the harness a
  cleaned-up version of the corpus that a real deployment would never receive --
  the single most effective way to inflate a LoCoMo score, and precisely the
  "claimed vs observed" adapter breach design/09 §Neutrality exists to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

from bench.core.corpus import (
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

__all__ = ["CATEGORY_NAMES", "LoCoMoLoader"]

# LoCoMo encodes categories as integers. This table is the published mapping;
# it is written out explicitly (rather than inferred) so that an auditor can
# check it against the paper in one glance, and an unknown code is a hard error
# rather than an "other" bucket that would quietly absorb a schema revision.
CATEGORY_NAMES: dict[int, str] = {
    1: "multi-hop",
    2: "temporal-reasoning",
    3: "open-domain-knowledge",
    4: "single-hop",
    5: "adversarial",
}

# Category 5 (adversarial) asks something the dialogue cannot answer, and the
# dataset carries its gold under a different key. Those are abstention
# questions: correct behavior is to decline, not to guess.
_ADVERSARIAL = 5

_SESSION_KEY = re.compile(r"^session_(\d+)$")


class LoCoMoLoader:
    """`BenchmarkLoader` for LoCoMo. One conversation -> one haystack."""

    name = "locomo"

    def load(self, path: Path) -> Corpus:
        raw = load_json(path)
        if not isinstance(raw, list):
            raise CorpusError(f"{path}: expected a list of conversations, got {type(raw).__name__}")

        haystacks: list[Haystack] = []
        questions: list[Question] = []
        dropped_image_turns = 0
        coerced_answers = 0

        for i, sample in enumerate(raw):
            where = f"{path.name}[{i}]"
            sample_id = str(require(sample, "sample_id", where=where))
            convo = require(sample, "conversation", where=where)
            if not isinstance(convo, dict):
                raise CorpusError(f"{where}: 'conversation' is not an object")

            sessions, dropped = _parse_sessions(convo, where=f"{where}.conversation")
            dropped_image_turns += dropped
            haystacks.append(Haystack(haystack_id=sample_id, sessions=sessions))

            qa = require(sample, "qa", where=where)
            if not isinstance(qa, list):
                raise CorpusError(f"{where}: 'qa' is not a list")
            for j, item in enumerate(qa):
                q, coerced = _parse_question(item, sample_id, j, where=f"{where}.qa[{j}]")
                coerced_answers += coerced
                questions.append(q)

        return Corpus(
            name="locomo",
            haystacks=tuple(haystacks),
            questions=tuple(questions),
            notes={
                "source_file": path.name,
                # Surfaced, not swallowed: both of these are loader liberties and
                # belong in the manifest where a reader can weigh them.
                "dropped_image_only_turns": dropped_image_turns,
                "coerced_non_string_answers": coerced_answers,
                "category_mapping": {str(k): v for k, v in CATEGORY_NAMES.items()},
                "summaries_ingested": False,
            },
        ).validate()


def _parse_sessions(convo: dict, *, where: str) -> tuple[tuple[Session, ...], int]:
    """Pull `session_N` / `session_N_date_time` pairs out in numeric order.

    Numeric order matters and string order will not do: `session_10` sorts
    before `session_2` lexicographically, which would feed a decade-spanning
    dialogue to the store out of sequence and silently break every
    knowledge-update and temporal question.
    """
    numbered: list[tuple[int, str]] = []
    for key in convo:
        m = _SESSION_KEY.match(key)
        if m:
            numbered.append((int(m.group(1)), key))
    if not numbered:
        raise CorpusError(f"{where}: no session_N keys found")
    numbered.sort()

    sessions: list[Session] = []
    dropped = 0
    for n, key in numbered:
        raw_turns = convo[key]
        if not isinstance(raw_turns, list):
            raise CorpusError(f"{where}.{key}: expected a list of turns")
        turns: list[Turn] = []
        for k, t in enumerate(raw_turns):
            tw = f"{where}.{key}[{k}]"
            if not isinstance(t, dict):
                raise CorpusError(f"{tw}: expected a turn object, got {type(t).__name__}")
            # `.get`, not `require`: an image-only turn carries no `text` key at
            # all (only `img_url` / `blip_caption`), so a strict field access
            # here would abort the parse on a turn we intend to drop.
            text = t.get("text")
            if not isinstance(text, str) or not text.strip():
                # Image-only turns exist in LoCoMo (they carry `img_url` and a
                # `blip_caption`). We ingest text only and count what we
                # dropped: substituting the caption would be feeding the store a
                # machine-written description a real client never sent, and it
                # can only be counted honestly if it is counted at all.
                dropped += 1
                continue
            turns.append(
                Turn(
                    speaker=str(require(t, "speaker", where=tw)),
                    text=text,
                    turn_id=str(t["dia_id"]) if "dia_id" in t else None,
                    timestamp=_session_timestamp(convo, n),
                )
            )
        if not turns:
            # A session with no usable text is skipped rather than erroring:
            # Haystack forbids empty sessions, and an all-image session is a
            # property of the dataset, not a parse failure.
            continue
        sessions.append(
            Session(
                session_id=f"session_{n}",
                turns=tuple(turns),
                timestamp=_session_timestamp(convo, n),
            )
        )
    if not sessions:
        raise CorpusError(f"{where}: every session was empty after dropping image-only turns")
    return tuple(sessions), dropped


def _session_timestamp(convo: dict, n: int) -> str | None:
    """LoCoMo timestamps are human strings ("1:56 pm on 8 May, 2023"), not
    RFC-3339. They are carried through verbatim and never parsed into a
    queryable field -- design/09 §temporal is measured, not chased rejects
    exactly that workaround."""
    v = convo.get(f"session_{n}_date_time")
    return str(v) if v is not None else None


def _parse_question(item: object, sample_id: str, j: int, *, where: str) -> tuple[Question, int]:
    raw_cat = require(item, "category", where=where)
    try:
        cat_int = int(raw_cat)
    except (TypeError, ValueError) as exc:
        raise CorpusError(f"{where}: category {raw_cat!r} is not an integer") from exc
    if cat_int not in CATEGORY_NAMES:
        raise CorpusError(
            f"{where}: unknown category code {cat_int}. Known: {sorted(CATEGORY_NAMES)}. "
            "The dataset schema may have revised -- update CATEGORY_NAMES deliberately "
            "rather than defaulting, so the report's categories stay the suite's own."
        )
    category = CATEGORY_NAMES[cat_int]
    abstain = cat_int == _ADVERSARIAL

    if abstain:
        # Adversarial gold lives under its own key; absence is tolerated because
        # the question is graded on declining, not on matching.
        raw_answer = item.get("adversarial_answer", item.get("answer", ""))
    else:
        raw_answer = require(item, "answer", where=where)

    coerced = 0
    if not isinstance(raw_answer, str):
        coerced = 1
    answer = as_text(raw_answer, where=where) if raw_answer != "" else ""

    evidence = item.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    if not isinstance(evidence, list):
        raise CorpusError(f"{where}: 'evidence' is neither a string nor a list")

    return (
        Question(
            question_id=f"{sample_id}:q{j}",
            haystack_id=sample_id,
            text=str(require(item, "question", where=where)),
            category=category,
            gold_answer=answer,
            evidence=tuple(str(e) for e in evidence),
            abstain_expected=abstain,
        ),
        coerced,
    )
