"""LLM-as-judge, on a rival vendor's model (design/09 §Answer extraction).

The judge is **deliberately not Anthropic**. A harness that grades Engraphy's
answers with the same vendor that produced them invites the obvious objection
that it marked its own homework, and no amount of prompt discipline answers that
objection from the inside. Running the judge on Gemini removes it structurally.
That the route is also free is a coincidence, not the reason: design/09 records
that this arrangement is kept even if funding appears.

What the judge is allowed to see is as narrow as the reader's context, for the
same reason: **the question, the gold answer, and the candidate answer.** It
never sees the retrieval envelope, the strategy, the extractor, or the confirm
policy, so it cannot be biased toward a configuration it cannot observe. This is
what makes the extractor-gap comparison meaningful -- the same judge, blind to
which arm it is grading, grades both.

## Judge noise is measured, not assumed away

There is no `temperature` to pin on the reader's side (removed on Opus 4.8/4.7,
returns HTTP 400), and while Gemini still accepts one, a run whose reproducibility
rests on a parameter that only half the stack supports is not reproducible. So
determinism is measured instead: `self_consistency` grades a stratified sample
twice and reports how often the two verdicts disagree. **An accuracy delta
smaller than that disagreement rate is not a result**, and the report states the
figure beside every delta rather than in a footnote.

The second half of the calibration design/09 specifies -- agreement against a
committed human-labelled subset -- needs labels that do not exist yet. It is
recorded as outstanding rather than quietly dropped.
"""

from __future__ import annotations

from dataclasses import dataclass

from bench.core.corpus import Question
from bench.core.llm import LLMError, load_prompt, prompt_hash

__all__ = ["Verdict", "Judge", "JUDGE_PROMPT", "JUDGE_SCHEMA", "JUDGE_PASSES",
           "self_consistency"]

JUDGE_PROMPT = "judge.md"

# The default scoring path grades each answer JUDGE_PASSES times and takes the
# majority verdict. Measured motivation: a two-pass audit of a 500-question run
# disagreed with itself on 16.4% of questions -- a single grade is a coin the
# judge occasionally flips. An odd number >=3 collapses that per-pass instability
# to the rate at which a majority flips, which is far lower. Kept odd so majority
# is always decisive.
JUDGE_PASSES = 3

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "correct": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["correct", "reason"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class Verdict:
    """One binary grade. `graded_by` records which mechanism produced it, so a
    row graded without an LLM (an abstention check) is never mistaken in the
    aggregate for one the judge actually saw."""

    correct: bool
    reason: str = ""
    graded_by: str = "judge"
    model: str = ""
    seconds: float = 0.0
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "correct": self.correct,
            "reason": self.reason[:500],
            "graded_by": self.graded_by,
            "judge_model": self.model,
            "judge_seconds": round(self.seconds, 4),
            "judge_error": self.error,
        }


class Judge:
    """One structured call per graded question."""

    def __init__(self, client, *, prompt_name: str = JUDGE_PROMPT, max_tokens: int = 512) -> None:
        self.client = client
        self.prompt_name = prompt_name
        self.system = load_prompt(prompt_name)
        self.max_tokens = max_tokens
        self.calls = 0

    @property
    def prompt_hash(self) -> str:
        return prompt_hash(self.prompt_name)

    def grade(self, question: Question, answer: str) -> Verdict:
        """One grading pass. Raises nothing except `QuotaExhausted`.

        This is the SINGLE-pass grade. It is kept as its own method because
        `self_consistency` needs to measure per-pass instability, and because the
        majority path is built on top of it. The default *scoring* path is
        `grade_majority` (best-of-`JUDGE_PASSES`), not this.
        """
        return self._grade_once(question, answer)

    def grade_majority(self, question: Question, answer: str,
                       passes: int = JUDGE_PASSES) -> Verdict:
        """Best-of-`passes` grade -- the default scoring path.

        Runs `passes` independent single grades and returns the majority verdict.
        This is what makes a run's accuracy robust to the judge's per-pass
        instability (measured at 16.4% two-pass disagreement): a majority-of-3
        flips only when >=2 of 3 passes flip, which is much rarer.

        Error handling preserves the invariant the caller relies on: a verdict is
        only trustworthy if it rests on real grades. If ANY pass hard-errors, this
        short-circuits and returns that `graded_by="judge_error"` verdict
        immediately -- it cannot contribute to a majority, and spending the
        remaining passes during an outage is waste. That verdict trips the
        harness's never-checkpoint guard, so the row is re-graded on resume rather
        than persisted as a real `correct=False`, and the run's error-breaker sees
        one errored call per row exactly as it did in the single-pass world.
        `QuotaExhausted` still propagates so the run stops clean.
        """
        votes: list[Verdict] = []
        for _ in range(max(1, passes)):
            v = self._grade_once(question, answer)
            if v.graded_by == "judge_error":
                return v
            votes.append(v)
        yes = sum(1 for v in votes if v.correct)
        correct = yes > len(votes) / 2
        # Reason: carry a representative reason from the winning side, plus the tally.
        winner = next((v for v in votes if v.correct == correct), votes[0])
        return Verdict(
            correct=correct,
            reason=f"[best-of-{len(votes)}: {yes} correct / {len(votes)-yes} wrong] {winner.reason}",
            graded_by="judge",
            model=winner.model,
            seconds=sum(v.seconds for v in votes),
        )

    def _grade_once(self, question: Question, answer: str) -> Verdict:
        """One structured judge call. Raises nothing except `QuotaExhausted`.

        Quota exhaustion is re-raised deliberately and is the one failure this
        function does not absorb: it ends the run cleanly with every completed
        row already checkpointed, whereas swallowing it would silently grade the
        remainder of a 500-question run as incorrect.
        """
        from bench.core.providers import QuotaExhausted

        self.calls += 1
        try:
            resp = self.client.complete(
                self.system, _render(question, answer),
                schema=JUDGE_SCHEMA, max_tokens=self.max_tokens,
            )
        except QuotaExhausted:
            raise
        except LLMError as exc:
            # Fails to `correct=False` and says so. The alternative -- dropping
            # the row -- would shrink the denominator, which silently improves
            # accuracy every time the judge errors. A recorded failure is
            # visible in the report and can be re-graded on a resume.
            return Verdict(
                correct=False, reason="", graded_by="judge_error",
                error=f"{type(exc).__name__}: {exc}"[:300],
            )

        data = resp.data or {}
        if "correct" not in data:
            return Verdict(
                correct=False, reason=str(data)[:200], graded_by="judge_error",
                model=resp.model, seconds=resp.seconds,
                error="structured response carried no 'correct' field",
            )
        return Verdict(
            correct=bool(data["correct"]),
            reason=str(data.get("reason") or ""),
            graded_by="judge",
            model=resp.model,
            seconds=resp.seconds,
        )


def _render(question: Question, answer: str) -> str:
    """The judge's entire user turn.

    Note what is absent: no category, no evidence, no envelope, no strategy, no
    extractor name. Adding any of them would let the judge condition on the
    configuration it is grading.
    """
    return (
        f"Question: {question.text}\n"
        f"Gold answer: {question.gold_answer}\n"
        f"Candidate answer: {answer if answer.strip() else '(no answer produced)'}"
    )


def self_consistency(judge: Judge, items: list[tuple[Question, str]]) -> dict:
    """Grade the same items twice and report how often the verdicts disagree.

    This measures **per-pass** instability (two single `grade` calls), which is
    the input the majority path exists to suppress -- it is NOT the stability of
    the scorer itself. Since the default scoring path is `grade_majority`
    (best-of-`JUDGE_PASSES`), the figure reported here is the raw single-grade
    disagreement rate; the majority verdict flips far less often (only when a
    majority of passes flip). Report it as "per-pass instability", not as the
    scorer's error bar.

    It remains the measured stand-in for the determinism that pinned sampling
    parameters used to provide. design/09 requires it beside the accuracy figure,
    because a reported delta smaller than the *scorer's* stability is noise
    wearing a result's clothes -- and per-pass instability upper-bounds that.

    Costs two judge calls per item, which on the free tier is the run's scarcest
    budget -- hence a stratified sample rather than the whole set.
    """
    disagreements = 0
    graded = 0
    errors = 0
    detail: list[dict] = []
    for question, answer in items:
        first = judge.grade(question, answer)
        second = judge.grade(question, answer)
        if first.graded_by == "judge_error" or second.graded_by == "judge_error":
            errors += 1
            continue
        graded += 1
        if first.correct != second.correct:
            disagreements += 1
            detail.append({
                "question_id": question.question_id,
                "first": first.correct,
                "second": second.correct,
            })
    return {
        "items_graded_twice": graded,
        "disagreements": disagreements,
        "instability": round(disagreements / graded, 4) if graded else None,
        "judge_errors": errors,
        "disagreeing_items": detail,
        "human_agreement": None,
        "human_agreement_note": (
            "design/09 also specifies agreement against a committed human-labelled "
            "subset. No such labels exist yet; this half of the calibration is "
            "outstanding and is recorded as absent rather than omitted."
        ),
    }
