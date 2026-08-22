"""The judge phase's resume and failure behaviour (design/09 §Neutrality).

These tests exist because of a specific incident, and they are shaped by it.

A live run's judge began returning 429s. `phase_judge` recorded each failure as
a verdict of `correct=False` and checkpointed it, so 60 rows became permanent
wrong answers that a resume would never retry. Nothing failed loudly. The run
would have finished and produced a complete-looking LoCoMo accuracy figure --
Wilson intervals, prompt hashes, manifest and all -- that measured a rate limit.

The rule that came out of it: **a harness must never persist a failed
measurement as a measurement.** An absent grade shrinks the denominator, which
is visible and recoverable; a fabricated grade is neither.

The second incident is the ordering one. Grading ran in file order, the daily
quota stopped it partway, and what survived was one arm graded completely, a
second arm graded only within a single conversation, and the multi-hop traverse
arm not graded at all -- 606 rows of work that answered no comparison the run
existed to make. Interruption is the normal case on a capped free tier, so the
order has to make any prefix a usable sample.

Hermetic: `StubLLM`, a tmp checkpoint dir, no database and no network.
"""

from __future__ import annotations

import json

import pytest

from bench.core.corpus import Corpus, Haystack, Question, Session, Turn
from bench.core.judge import Judge, JUDGE_PASSES
from bench.core.llm import LLMError, StubLLM
from bench.core.run import Checkpoint, _judge_order, phase_calibrate, phase_judge

ARMS = ("verbatim/search_only/always_distinct",
        "llm/search_only/always_distinct",
        "llm/search_then_traverse/always_distinct")
CATS = ("single-hop", "multi-hop", "temporal-reasoning", "adversarial")
HAYSTACKS = ("conv-a", "conv-b")


def _corpus() -> Corpus:
    turns = (Turn(speaker="A", text="hello there", turn_id="D1:1"),)
    questions = []
    for h in HAYSTACKS:
        for c in CATS:
            for i in range(4):
                adv = c == "adversarial"
                questions.append(Question(
                    question_id=f"{h}:{c}:{i}", haystack_id=h,
                    text=f"question {i} about {c}?", category=c,
                    gold_answer="" if adv else "the gold answer",
                    abstain_expected=adv,
                ))
    return Corpus(
        name="synthetic",
        haystacks=tuple(Haystack(h, (Session(f"{h}-s1", turns),)) for h in HAYSTACKS),
        questions=tuple(questions),
    ).validate()


@pytest.fixture
def ck(tmp_path) -> Checkpoint:
    """A checkpoint dir holding one answered row per (arm, question)."""
    c = Checkpoint(tmp_path / "run")
    for arm in ARMS:
        for q in _corpus().questions:
            # The traverse arm only asks multi-hop, matching the real config.
            if "traverse" in arm and q.category != "multi-hop":
                continue
            c.append("answers.jsonl", {
                "arm": arm, "question_id": q.question_id, "haystack_id": q.haystack_id,
                "category": q.category, "abstain_expected": q.abstain_expected,
                "answer": "some answer", "envelope_bytes": 100,
                "reader_seconds": 0.1, "retrieval_seconds": 0.01,
            })
    return c


def _ok_judge(n: int = 5000) -> Judge:
    return Judge(StubLLM(responses=[{"correct": True, "reason": "stub"}] * n))


def test_judge_order_makes_every_prefix_a_balanced_sample(ck):
    """The property that matters when quota interrupts grading.

    File order is arm-major, so a truncated pass grades arm 1 exhaustively and
    never reaches arm 3. A prefix must instead look like a smaller version of
    the whole comparison.
    """
    rows = ck.rows("answers.jsonl")
    ordered = _judge_order(rows)
    assert len(ordered) == len(rows)

    prefix = ordered[: len(rows) // 4]
    assert {r["arm"] for r in prefix} == set(ARMS), "a quarter-prefix missed an arm"
    assert {r["category"] for r in prefix} == set(CATS), "a quarter-prefix missed a category"
    assert {r["haystack_id"] for r in prefix} == set(HAYSTACKS)

    # The regression this encodes: in file order the last arm appears only in
    # the final third, so a prefix this size contains none of it.
    assert [r["arm"] for r in rows[: len(rows) // 4]].count(ARMS[-1]) == 0
    assert [r["arm"] for r in prefix].count(ARMS[-1]) > 0


def test_judge_order_is_deterministic(ck):
    rows = ck.rows("answers.jsonl")
    first = [(r["arm"], r["question_id"]) for r in _judge_order(rows)]
    second = [(r["arm"], r["question_id"]) for r in _judge_order(list(reversed(rows)))]
    assert first == second, "grading order must not depend on input order"


def test_adversarial_questions_never_reach_the_judge(ck):
    """Their correct answer is a refusal, decided by rule. On LoCoMo this is 22%
    of the suite and therefore 22% of a hard daily quota not spent."""
    judge = _ok_judge()
    assert phase_judge(ck, _corpus(), lambda: judge) is True
    verdicts = ck.rows("verdicts.jsonl")
    adversarial = [r for r in verdicts if r["graded_by"] == "abstention_rule"]
    # 2 haystacks x 4 adversarial questions = 8, asked by the 2 non-traverse arms.
    assert len(adversarial) == 16, "one per adversarial question per non-traverse arm"
    # Default scoring is best-of-JUDGE_PASSES, so each judged (non-adversarial)
    # verdict costs JUDGE_PASSES calls; adversarial rows still cost zero.
    assert judge.calls == JUDGE_PASSES * (len(verdicts) - len(adversarial))


def test_a_failed_grade_is_never_persisted(ck):
    """The incident this module exists for.

    A judge_error verdict carries correct=False. Writing it makes a rate limit
    permanently indistinguishable from a wrong answer, and no resume can undo
    it because the row now counts as graded.
    """
    judge = Judge(StubLLM(responses=[LLMError("simulated 429")] * 500))
    assert phase_judge(ck, _corpus(), lambda: judge) is False

    verdicts = ck.rows("verdicts.jsonl")
    assert not any(r["graded_by"] == "judge_error" for r in verdicts)
    assert all(r["graded_by"] == "abstention_rule" for r in verdicts), (
        "only rule-graded rows may survive a judge outage"
    )
    assert judge.calls <= 5, "the breaker must stop rather than grade a whole run badly"


def test_a_resume_regrades_rows_the_outage_left_ungraded(ck):
    """End to end: an outage, then a working judge, then a complete result."""
    corpus = _corpus()
    phase_judge(ck, corpus, lambda: Judge(StubLLM(responses=[LLMError("429")] * 500)))
    after_outage = len(ck.rows("verdicts.jsonl"))

    judge = _ok_judge()
    assert phase_judge(ck, corpus, lambda: judge) is True
    verdicts = ck.rows("verdicts.jsonl")
    assert len(verdicts) == len(ck.rows("answers.jsonl"))
    assert len(verdicts) > after_outage

    keys = [(r["arm"], r["question_id"]) for r in verdicts]
    assert len(keys) == len(set(keys)), "a resume must not double-grade a row"


def test_calibration_double_grades_and_reports_instability(ck):
    """design/09 requires judge noise beside the accuracy figure. This had never
    executed once -- the daily quota killed every real run before reaching it."""
    corpus = _corpus()
    judge = _ok_judge()
    phase_judge(ck, corpus, lambda: judge)
    before = judge.calls

    cal = phase_calibrate(ck, corpus, judge, 6)
    assert cal["items_graded_twice"] == 6
    assert cal["instability"] == 0.0, "a stub that always agrees must show zero instability"
    assert judge.calls == before + 12, "calibration costs two judge calls per item"
    # The human-labelled half of the calibration does not exist yet and must be
    # reported as absent rather than omitted.
    assert cal["human_agreement"] is None
    assert "outstanding" in cal["human_agreement_note"]


def test_calibration_disagreement_is_detected(ck):
    """The figure has to be able to come out nonzero, or it proves nothing."""
    corpus = _corpus()
    phase_judge(ck, corpus, _ok_judge)
    flipping = Judge(StubLLM(responses=[
        {"correct": bool(i % 2), "reason": "alternating"} for i in range(200)
    ]))
    cal = phase_calibrate(ck, corpus, flipping, 4)
    assert cal["instability"] == 1.0, "every pair disagrees, so instability must be 1.0"
    assert len(cal["disagreeing_items"]) == 4


def test_checkpoint_rows_survive_a_reread(tmp_path):
    c = Checkpoint(tmp_path / "r")
    c.append("x.jsonl", {"a": 1})
    c.append("x.jsonl", {"a": 2})
    assert [r["a"] for r in Checkpoint(tmp_path / "r").rows("x.jsonl")] == [1, 2]
    assert json.loads((tmp_path / "r" / "x.jsonl").read_text().splitlines()[0]) == {"a": 1}
