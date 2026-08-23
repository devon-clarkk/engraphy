"""Rule-based fair-scoring pass (design/09 §Metrics; added 2026-07-23).

The binary LLM judge marks a substantively-correct answer wrong in three
recurring, mechanical ways the failure analysis surfaced:

* **Superset** -- the answer contains the gold answer plus extra correct detail
  ("runs, reads, violin, pottery, painting" when gold is "running, pottery").
* **Multi-part** -- a gold of N comma/and-separated parts is marked wrong for
  covering only some of them, with no partial credit.
* **Format** -- "early July 2023" vs "2 July 2023", "two" vs "2", case and
  whitespace: the same fact in a different shape.

This module is **deterministic and LLM-free**. It normalises both sides, splits
the gold into parts, and scores coverage. It exists for two jobs:

1. A no-credit PREVIEW: re-score the committed answers and count how many judge
   "wrong" verdicts flip to correct on normalisation + superset alone -- a lower
   bound on the fair-scoring effect, because it decides only the rule-decidable
   cases and leaves the semantic ones to the LLM judge.
2. To feed `report.py` a **strict / fair / partial-credit** triple so the report
   shows all three rather than silently replacing the strict number.

It is a lower bound by design: no stemming (so "runs" != "running"), so it
under-counts flips rather than inventing them. The LLM judge's updated rubric
(bench/prompts/judge.md) catches the fuzzy cases on the next graded run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["RuleScore", "coverage", "gold_parts", "normalize", "score_answer"]

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_NUM_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20", "thirty": "30", "forty": "40",
    "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90",
}
# Words dropped before matching content: fillers plus the vague date qualifiers
# that make "early July" and "2 July" the same month.
_DROP = {
    "the", "a", "an", "of", "in", "on", "at", "and", "or", "to", "her", "his",
    "she", "he", "they", "about", "around", "approximately", "roughly",
    "early", "mid", "late", "beginning", "end", "start", "middle",
}
_SPLIT = re.compile(r"\s*(?:,| and | & |;|/|\bplus\b)\s*", re.IGNORECASE)


def normalize(text: str) -> str:
    """Lowercase, expand number words, drop fillers, collapse to bare tokens."""
    t = (text or "").lower()
    t = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", t)          # 2nd -> 2
    toks = re.findall(r"[a-z0-9]+", t)
    out = [_NUM_WORDS.get(w, w) for w in toks if w not in _DROP]
    return " ".join(out)


def _content_words(text: str) -> set[str]:
    return {w for w in normalize(text).split() if len(w) >= 3 or w.isdigit()}


# A vague within-month qualifier maps to a DAY RANGE, not "any day". This is
# what keeps the date tolerance honest: "early July" matches the specific 2nd
# (2 in 1-10), but "mid-May" does NOT match the 25th (25 not in 11-20) -- so a
# genuinely-different day is never flipped to correct.
_QUAL = {"early": frozenset(range(1, 11)), "beginning": frozenset(range(1, 11)),
         "start": frozenset(range(1, 11)), "mid": frozenset(range(11, 21)),
         "middle": frozenset(range(11, 21)), "late": frozenset(range(21, 32)),
         "end": frozenset(range(21, 32))}


def _dates(text: str):
    """Extract (year, month, day) triples from free text.

    `day` is `None` (no day given -> any day in the month), an `int` (specific
    day), or a frozenset (a qualifier's range). Matching is tolerant across
    format but NOT across a genuinely different day (see `_date_match`)."""
    t = (text or "").lower()
    found: list = []

    def add(y, m, day):
        found.append((int(y), m, day))

    for y, m, d in re.findall(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t):
        add(y, int(m), int(d))
    mon = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    for q, mo, y in re.findall(
            rf"\b(early|mid|middle|late|beginning|start|end)(?:\s+of)?[\s-]+{mon}[a-z]*\.?,?\s+(\d{{4}})\b", t):
        add(y, _MONTHS[mo], _QUAL[q])
    for d, mo, y in re.findall(rf"\b(\d{{1,2}})\s+{mon}[a-z]*\.?,?\s+(\d{{4}})\b", t):
        add(y, _MONTHS[mo], int(d))
    for mo, d, y in re.findall(rf"\b{mon}[a-z]*\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", t):
        add(y, _MONTHS[mo], int(d))
    for mo, y in re.findall(rf"\b{mon}[a-z]*\.?,?\s+(\d{{4}})\b", t):
        if not any(f[0] == int(y) and f[1] == _MONTHS[mo] and f[2] is not None
                   for f in found):
            add(y, _MONTHS[mo], None)
    if not found:  # bare year, last resort
        for y in re.findall(r"\b(20\d{2}|19\d{2})\b", t):
            add(y, None, None)
    return found


def _day_set(day):
    if day is None:
        return None
    return frozenset({day}) if isinstance(day, int) else frozenset(day)


def _date_match(a, b) -> bool:
    (ya, ma, da), (yb, mb, db) = a, b
    if ya != yb:
        return False
    if ma is not None and mb is not None and ma != mb:
        return False
    sa, sb = _day_set(da), _day_set(db)
    # Not collapsed into `return not (...)` (ruff SIM103): each early return is a
    # separate reason two dates are considered different, and the trailing comment
    # belongs to this one.
    if sa is not None and sb is not None and not (sa & sb):  # noqa: SIM103
        return False   # both name a day/range and they do not overlap -> different day
    return True


def gold_parts(gold: str) -> list[str]:
    parts = [p.strip() for p in _SPLIT.split(gold or "") if p.strip()]
    return parts or ([gold.strip()] if (gold or "").strip() else [])


_REL = re.compile(r"\b(before|after|prior|following|leading\s+up|ago|since|until|by)\b")


def _part_present(part: str, answer: str, answer_dates) -> bool:
    pd = _dates(part)
    if pd:
        # A relational reference point changes which date is meant: "week OF the
        # 23rd" vs "week BEFORE the 23rd" both parse to the 23rd but denote
        # different weeks. If exactly one side carries a before/after relation,
        # refuse the match rather than flip a genuinely different date.
        if bool(_REL.search(part.lower())) != bool(_REL.search((answer or "").lower())):
            return False
        return any(_date_match(x, y) for x in pd for y in answer_dates)
    pn = normalize(part)
    if not pn:
        return True
    an = normalize(answer)
    if pn in an:
        return True
    pw = _content_words(part)
    return bool(pw) and pw <= _content_words(answer)


@dataclass(frozen=True, slots=True)
class RuleScore:
    """A deterministic re-score of one answer against its gold."""

    coverage: float          # fraction of gold parts present (0..1)
    n_parts: int
    strict_correct: bool     # every gold part present (superset-tolerant)
    is_answer: bool          # not empty and not an abstention

    @property
    def partial(self) -> float:
        """Graded credit: coverage for a real answer, 0 for a non-answer."""
        return self.coverage if self.is_answer else 0.0


def score_answer(gold: str, answer: str) -> RuleScore:
    """Rule-based fair score. `strict_correct` means the answer covers ALL gold
    parts (extras allowed) after normalisation -- the superset-tolerant,
    format-normalised, date-tolerant match."""
    from bench.core.answer import is_abstention

    ans = (answer or "").strip()
    is_ans = bool(ans) and not is_abstention(ans)
    parts = gold_parts(gold)
    if not is_ans or not parts:
        return RuleScore(0.0, len(parts), False, is_ans)
    adates = _dates(answer)
    present = sum(1 for p in parts if _part_present(p, answer, adates))
    cov = present / len(parts)
    return RuleScore(cov, len(parts), present == len(parts), is_ans)


def coverage(gold: str, answer: str) -> float:
    return score_answer(gold, answer).coverage
