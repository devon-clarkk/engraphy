"""Corpus IR + both loaders (design/09 §Interface 1).

These are spec, in the repo's fixtures-are-spec sense: the miniature fixtures
encode the published LoCoMo and LongMemEval schemas, and the assertions here are
the properties the shared core is entitled to assume. The one thing they cannot
prove is that the real downloads match the published schema -- that stays open
in design/09 until a real file is parsed, which is exactly why the loaders parse
strictly and fail loudly rather than coercing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bench.adapters.locomo import CATEGORY_NAMES, LoCoMoLoader
from bench.adapters.longmemeval import LongMemEvalLoader
from bench.core.corpus import Corpus, CorpusError, Haystack, Question, Session, Turn

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------
# IR invariants
# --------------------------------------------------------------------------


def _turn(text="hello", speaker="user"):
    return Turn(speaker=speaker, text=text)


def _corpus(questions, haystacks=None):
    hs = haystacks or (Haystack("h1", (Session("s1", (_turn(),)),)),)
    return Corpus(name="t", haystacks=hs, questions=tuple(questions))


def test_empty_turn_text_is_rejected():
    with pytest.raises(CorpusError, match="text is empty"):
        Turn(speaker="user", text="   ")


def test_session_without_turns_is_rejected():
    with pytest.raises(CorpusError, match="no turns"):
        Session("s1", ())


def test_haystack_rejects_duplicate_session_ids():
    s = Session("s1", (_turn(),))
    with pytest.raises(CorpusError, match="repeats session id"):
        Haystack("h1", (s, s))


def test_question_requires_gold_answer_unless_abstaining():
    with pytest.raises(CorpusError, match="empty gold_answer"):
        Question("q1", "h1", "text?", "cat", "")
    # An abstention question legitimately has none.
    Question("q1", "h1", "text?", "cat", "", abstain_expected=True)


def test_question_naming_unknown_haystack_is_rejected():
    """The isolation model has no answer for a cross-haystack question: it would
    have to search two scopes at once, which is the contamination scoping
    prevents."""
    q = Question("q1", "nope", "text?", "cat", "a")
    with pytest.raises(CorpusError, match="unknown haystack"):
        _corpus([q]).validate()


def test_duplicate_question_ids_are_rejected():
    q = Question("q1", "h1", "text?", "cat", "a")
    with pytest.raises(CorpusError, match="repeats question id"):
        _corpus([q, q]).validate()


def test_subset_is_deterministic_and_stratified():
    qs = [
        Question(f"q{i}", "h1", "t?", "even" if i % 2 == 0 else "odd", "a")
        for i in range(20)
    ]
    c = _corpus(qs).validate()
    a = c.subset(6)
    b = c.subset(6)
    assert [q.question_id for q in a.questions] == [q.question_id for q in b.questions]
    # Round-robin across categories: a small subset touches both, not just the
    # larger one.
    assert {q.category for q in a.questions} == {"even", "odd"}
    assert len(a.questions) == 6


def test_subset_drops_haystacks_it_does_not_need():
    hs = tuple(Haystack(f"h{i}", (Session("s1", (_turn(),)),)) for i in range(3))
    qs = tuple(Question(f"q{i}", f"h{i}", "t?", "c", "a") for i in range(3))
    c = Corpus("t", hs, qs).validate()
    sub = c.subset(1)
    assert len(sub.haystacks) == 1
    assert sub.haystacks[0].haystack_id == sub.questions[0].haystack_id


# --------------------------------------------------------------------------
# LoCoMo
# --------------------------------------------------------------------------


def test_locomo_loads_and_orders_sessions_numerically():
    c = LoCoMoLoader().load(FIXTURES / "locomo_mini.json")
    (h,) = c.haystacks
    assert h.haystack_id == "conv-1"
    # session_10 must sort AFTER session_2, not between session_1 and session_2.
    # Lexicographic ordering here would feed the dialogue to the store out of
    # sequence and silently break every temporal and knowledge-update question.
    assert [s.session_id for s in h.sessions] == ["session_1", "session_2", "session_10"]


def test_locomo_preserves_speaker_names():
    """Flattening two named humans to user/assistant would destroy the
    attribution the single-hop questions are scored on."""
    c = LoCoMoLoader().load(FIXTURES / "locomo_mini.json")
    speakers = {t.speaker for s in c.haystacks[0].sessions for t in s.turns}
    assert speakers == {"Caroline", "Melanie"}


def test_locomo_maps_every_category_verbatim():
    c = LoCoMoLoader().load(FIXTURES / "locomo_mini.json")
    assert set(c.categories) == set(CATEGORY_NAMES.values())


def test_locomo_marks_adversarial_as_abstention():
    c = LoCoMoLoader().load(FIXTURES / "locomo_mini.json")
    (adv,) = [q for q in c.questions if q.category == "adversarial"]
    assert adv.abstain_expected is True


def test_locomo_coerces_numeric_answer_and_records_it():
    c = LoCoMoLoader().load(FIXTURES / "locomo_mini.json")
    (od,) = [q for q in c.questions if q.category == "open-domain-knowledge"]
    assert od.gold_answer == "20"
    # The liberty is surfaced into the manifest, not swallowed.
    assert c.notes["coerced_non_string_answers"] == 1


def test_locomo_drops_image_only_turns_and_counts_them():
    c = LoCoMoLoader().load(FIXTURES / "locomo_mini.json")
    assert c.notes["dropped_image_only_turns"] == 1
    texts = [t.text for s in c.haystacks[0].sessions for t in s.turns]
    assert not any("beach" in t for t in texts)


def test_locomo_never_ingests_the_dataset_summaries():
    """event_summary / session_summary are pre-digested annotations. Ingesting
    them would feed the harness a cleaned-up corpus no deployment receives --
    the single most effective way to inflate a LoCoMo score."""
    c = LoCoMoLoader().load(FIXTURES / "locomo_mini.json")
    blob = " ".join(t.text for s in c.haystacks[0].sessions for t in s.turns)
    assert "must never be ingested" not in blob
    assert c.notes["summaries_ingested"] is False


def test_locomo_unknown_category_fails_loudly(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(
        '[{"sample_id":"c1","conversation":{"session_1":[{"speaker":"A","text":"hi"}]},'
        '"qa":[{"question":"q?","answer":"a","category":9}]}]',
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="unknown category code 9"):
        LoCoMoLoader().load(bad)


def test_locomo_missing_required_field_names_the_record(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(
        '[{"sample_id":"c1","conversation":{"session_1":[{"speaker":"A","text":"hi"}]},'
        '"qa":[{"question":"q?","category":4}]}]',
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="missing required field 'answer'"):
        LoCoMoLoader().load(bad)


# --------------------------------------------------------------------------
# LongMemEval
# --------------------------------------------------------------------------


def test_longmemeval_gives_each_question_its_own_haystack():
    """The structural difference from LoCoMo: sharing a scope across questions
    would let one question's sessions become dedup candidates for another's."""
    c = LongMemEvalLoader().load(FIXTURES / "longmemeval_mini.json")
    assert len(c.haystacks) == len(c.questions) == 2
    assert {h.haystack_id for h in c.haystacks} == {q.question_id for q in c.questions}


def test_longmemeval_preserves_session_ids_and_order():
    c = LongMemEvalLoader().load(FIXTURES / "longmemeval_mini.json")
    upd = next(h for h in c.haystacks if h.haystack_id == "q_update_001")
    assert [s.session_id for s in upd.sessions] == ["s_a", "s_b"]
    # The superseded fact must precede the one that replaces it.
    assert "hardware store" in upd.sessions[0].turns[0].text
    assert "bakery" in upd.sessions[1].turns[0].text


def test_longmemeval_abs_suffix_marks_abstention():
    c = LongMemEvalLoader().load(FIXTURES / "longmemeval_mini.json")
    abs_q = next(q for q in c.questions if q.question_id.endswith("_abs"))
    assert abs_q.abstain_expected is True
    # Kept in its own published category rather than moved to an "abstain"
    # bucket -- the suite scores it within its type.
    assert abs_q.category == "single-session-preference"


def test_longmemeval_has_answer_becomes_evidence_not_store_content():
    """`has_answer` labels the needle. Propagating it into the store would hand
    retrieval the answer key."""
    c = LongMemEvalLoader().load(FIXTURES / "longmemeval_mini.json")
    upd = next(q for q in c.questions if q.question_id == "q_update_001")
    assert upd.evidence == ("s_b:0",)
    assert c.notes["has_answer_propagated_to_store"] is False


def test_longmemeval_unknown_question_type_fails_loudly(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(
        '[{"question_id":"x","question_type":"brand-new-category","question":"q?",'
        '"answer":"a","haystack_sessions":[[{"role":"user","content":"hi"}]]}]',
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="unknown question_type"):
        LongMemEvalLoader().load(bad)


def test_longmemeval_inconsistent_session_id_count_fails_loudly(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(
        '[{"question_id":"x","question_type":"multi-session","question":"q?","answer":"a",'
        '"haystack_session_ids":["a","b"],'
        '"haystack_sessions":[[{"role":"user","content":"hi"}]]}]',
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="internally inconsistent"):
        LongMemEvalLoader().load(bad)


def test_stats_shape_is_manifest_ready():
    c = LoCoMoLoader().load(FIXTURES / "locomo_mini.json")
    st = c.stats()
    assert st["haystacks"] == 1
    assert st["questions"] == 5
    assert st["abstain_questions"] == 1
    assert sum(st["questions_per_category"].values()) == 5
