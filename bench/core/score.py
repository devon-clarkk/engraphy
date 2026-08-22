"""Scoring -- shim boundary 2 (design/09 §Interface 2).

```python
class Scorer(Protocol):
    def grade(self, q: Question, answer: str, judge: Judge) -> Verdict: ...
```

One scorer handles every suite. It branches on exactly one thing:
`Question.abstain_expected`, which is a field of the corpus IR rather than a
suite name. That distinction matters more than it looks. "If the suite is
LoCoMo, do X" is per-suite branching and is forbidden by design/09 §Neutrality
item 2; "if this question's correct answer is a refusal, grade the refusal" is a
property the loader read out of the dataset, and it applies identically to
LoCoMo's adversarial category and LongMemEval's `_abs` set. The rule survives
because the branch is on data, not on identity.

**An abstention question is graded without an LLM.** Its gold answer is not a
fact to match -- it is the absence of one. Asking a judge whether "INSUFFICIENT"
conveys the same fact as an empty gold answer would be asking the wrong
question, and it would spend a scarce free-tier judge call to do it. LoCoMo's
446 adversarial questions are 22% of the suite, so this is a fifth of the judge
budget saved by not misusing the judge.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from bench.core.answer import is_abstention
from bench.core.corpus import Question
from bench.core.judge import Judge, Verdict

__all__ = ["Scorer", "LLMJudgeScorer", "grade_abstention"]


@runtime_checkable
class Scorer(Protocol):
    name: str

    def grade(self, question: Question, answer: str, judge: Judge) -> Verdict: ...


def grade_abstention(answer: str) -> Verdict:
    """Correct iff the system declined.

    LoCoMo's adversarial questions ask something the dialogue cannot answer;
    LongMemEval's abstention set is built the same way. Correct behaviour is to
    say so. A confident wrong answer and a confident right-sounding answer are
    both failures here, which is exactly what the category exists to detect --
    and it is the one category where a memory system that returns nothing scores
    perfectly, so the aggregate figure must always be reported per-category
    beside it.
    """
    declined = is_abstention(answer)
    return Verdict(
        correct=declined,
        reason=("declined, as the question requires" if declined
                else "answered a question whose correct response is to decline"),
        graded_by="abstention_rule",
    )


class LLMJudgeScorer:
    """The default scorer for every suite."""

    name = "llm_judge"

    def grade(self, question: Question, answer: str, judge: Judge) -> Verdict:
        if question.abstain_expected:
            return grade_abstention(answer)
        # Default scoring is best-of-N majority, not a single grade: a lone judge
        # pass disagrees with itself ~16% of the time (measured), so a run scored
        # on single passes carries that noise into its headline. `grade_majority`
        # collapses it.
        return judge.grade_majority(question, answer)
