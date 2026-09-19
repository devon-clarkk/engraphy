"""bench.core.completeness: the pure parts (no database, no embedder)."""
from bench.core.answer import render_envelope
from bench.core.completeness import entity_names, names_in, render_roster, render_turns


def test_entity_names_take_the_title_up_to_a_comma_or_parenthesis():
    names = entity_names(["Jon, aspiring dance studio founder", "Gina (dancer)", "Caroline",
                          "Caroline", "pottery class", "x"])
    assert names == ["Caroline", "Gina", "Jon"]


def test_names_match_whole_words_case_sensitively():
    names = ["Caroline", "Jon", "Sam"]
    assert names_in("What did Caroline's friend Jon say?", names) == ["Caroline", "Jon"]
    assert names_in("Is Samantha here?", names) == []
    assert names_in("what did jon say", names) == []


def test_roster_and_turns_render_after_results_with_continuous_numbering():
    env = {"results": [{"node": {"title": "A fact", "body": "A fact"}}],
           "entities": ["Melanie"],
           "entity_roster": [{"type": "event", "title": "Melanie went camping",
                              "attrs": {"occurred_on": "2023-07"}}],
           "source_turns": [{"speaker": "Melanie", "text": "We camped at the beach.",
                             "when": "1:14 pm on 25 May, 2023"}]}
    text = render_envelope(env)
    assert "[1] A fact" in text
    assert "mentions Melanie" in text
    assert "[2] [event] (occurred_on: 2023-07) Melanie went camping" in text
    assert "[3] Melanie (1:14 pm on 25 May, 2023): We camped at the beach." in text
    assert text.index("[1]") < text.index("[2]") < text.index("[3]")


def test_plain_envelopes_render_exactly_as_before():
    env = {"results": [{"node": {"title": "A fact", "body": "Body"}}]}
    assert "mentions" not in render_envelope(env)
    assert "Conversation turns" not in render_envelope(env)
    assert render_roster([], 1) == "" and render_turns([], 1) == ""
