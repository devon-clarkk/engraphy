"""Rule-based fair scoring (design/09 §Metrics). Hermetic: no LLM, no DB.

Pins the three mechanical judge-harshness fixes the failure analysis found --
superset tolerance, multi-part coverage, and format/date/number normalisation --
each on the concrete case that motivated it.
"""

from __future__ import annotations

from bench.core.scoring import gold_parts, normalize, score_answer


# --- normalization ---------------------------------------------------------
def test_normalize_numbers_case_whitespace():
    assert normalize("Twenty  Hours") == "20 hours"
    assert normalize("Two") == "2"
    assert normalize("A GREYHOUND") == "greyhound"  # filler 'a' dropped


# --- superset tolerance ----------------------------------------------------
def test_superset_is_correct():
    # gold's parts all present, plus extras -> correct, not "adds unsupported"
    s = score_answer("running, pottery",
                     "runs, reads, violin, pottery, painting")
    # 'runs' != 'running' (no stemming), so this is a conservative miss...
    assert s.n_parts == 2
    # ...but the exact-word superset case IS caught:
    s2 = score_answer("running, pottery", "running, pottery, painting, and reading")
    assert s2.strict_correct is True and s2.coverage == 1.0


def test_single_phrase_superset():
    s = score_answer("greyhound", "Her dog is a greyhound named Pepper.")
    assert s.strict_correct is True


# --- multi-part coverage ---------------------------------------------------
def test_multipart_partial_coverage():
    s = score_answer("beach, mountains, forest", "the mountains and the beach")
    assert s.n_parts == 3
    assert abs(s.coverage - 2 / 3) < 1e-9
    assert s.strict_correct is False        # incomplete -> not strict-correct
    assert abs(s.partial - 2 / 3) < 1e-9     # but earns partial credit


def test_multipart_full_coverage_any_order():
    s = score_answer("dinosaurs, nature", "nature and dinosaurs, mostly")
    assert s.strict_correct is True and s.coverage == 1.0


# --- date tolerance --------------------------------------------------------
def test_date_month_tolerance():
    # "early July 2023" (month-level) matches the specific "2 July 2023"
    assert score_answer("2 July 2023", "early July 2023").strict_correct is True
    assert score_answer("2 July 2023", "2023-07-02").strict_correct is True
    assert score_answer("July 2023", "on the 2nd of July, 2023").strict_correct is True


def test_date_mismatch_stays_wrong():
    assert score_answer("2 July 2023", "sometime in August 2023").strict_correct is False
    assert score_answer("2 July 2023", "2 July 2024").strict_correct is False


def test_qualifier_range_excludes_wrong_day():
    # The real false-flip that a naive month-level tolerance produced: a vague
    # "mid-May" must NOT be accepted as the specific 25th (25 not in 11-20).
    assert score_answer("25 May 2023", "around mid-May 2023").strict_correct is False
    # ...while the qualifier DOES cover a day inside its range.
    assert score_answer("15 July 2023", "mid-July 2023").strict_correct is True
    assert score_answer("2 July 2023", "early July 2023").strict_correct is True


# --- non-answers never flip ------------------------------------------------
def test_abstention_and_empty_never_correct():
    assert score_answer("a greyhound", "INSUFFICIENT").strict_correct is False
    assert score_answer("a greyhound", "").strict_correct is False
    assert score_answer("a greyhound", "").is_answer is False


def test_gold_parts_splitting():
    assert gold_parts("running, pottery and swimming; hiking") == \
        ["running", "pottery", "swimming", "hiking"]
    assert gold_parts("a single phrase answer") == ["a single phrase answer"]
