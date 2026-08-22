"""The two 2026-07-22 improvements, and the neutrality guards on them.

Both changes respond to a measured weakness, so both carry a flattery risk, and
these tests pin the specific properties that keep them honest:

* source-text retention must never append text a turn did not contain, must not
  double-count a quote already in the body, and must not collapse the LLM node
  into the verbatim floor's shape;
* `SearchThenTraverse` must seed at the same width as `SearchOnly`, or the
  multi-hop comparison confounds the graph with search width.

Hermetic: no LLM, no database.
"""

from __future__ import annotations

from bench.core.corpus import Session, Turn
from bench.core.extract import (
    ExtractWindow,
    VerbatimExtractor,
    _append_source_text,
    _drafts_from_payload,
    _render_window,
)
from bench.core.retrieve import SearchOnly, SearchThenTraverse

TURNS = (
    Turn(speaker="Caroline", text="I adopted a greyhound named Pepper.", turn_id="D1:1"),
    Turn(speaker="Melanie", text="How is she settling in?", turn_id="D1:2"),
    Turn(speaker="Caroline", text="She sleeps twenty hours a day.", turn_id="D1:3"),
)
WINDOW = ExtractWindow(
    haystack_id="h", session=Session("s1", TURNS), turns=TURNS, window_index=0,
)
NODE_TYPES = {"note": {}, "event": {"attrs": {}}, "preference": {"attrs": {}}}


# --- source-text retention -------------------------------------------------
def test_append_only_cites_turns_actually_shown():
    """A cited id absent from the window resolves to nothing: the model cannot
    smuggle in text it was not given."""
    turn_text = {"D1:1": "Caroline: I adopted a greyhound named Pepper."}
    out = _append_source_text("Caroline owns a dog.", ["D1:1", "D9:9"], turn_text)
    assert "greyhound named Pepper" in out
    assert "D9:9" not in out  # the unknown id contributed nothing


def test_append_skips_a_quote_already_in_the_body():
    """No double-counting: text already present is not appended again."""
    body = "Caroline: I adopted a greyhound named Pepper."
    turn_text = {"D1:1": "Caroline: I adopted a greyhound named Pepper."}
    out = _append_source_text(body, ["D1:1"], turn_text)
    assert out == body  # nothing to add; the words are already there


def test_append_adds_only_the_missing_words():
    turn_text = {"D1:3": "Caroline: She sleeps twenty hours a day."}
    out = _append_source_text("Pepper sleeps a lot.", ["D1:3"], turn_text)
    assert "Source (verbatim):" in out
    assert "She sleeps twenty hours a day." in out
    assert out.startswith("Pepper sleeps a lot.")


def test_append_no_ids_returns_body_unchanged():
    assert _append_source_text("A fact.", None, {"D1:1": "x"}) == "A fact."
    assert _append_source_text("A fact.", [], {"D1:1": "x"}) == "A fact."


def test_retention_keeps_the_node_typed_and_word_searchable():
    payload = {
        "memories": [{
            "local_id": "m1", "node_type": "event",
            "title": "Caroline adopted a greyhound named Pepper",
            "body": "Caroline adopted a greyhound named Pepper.",
            "source_turn_ids": ["D1:1", "D1:3"],
        }],
        "edges": [],
    }
    nodes, _ = _drafts_from_payload(payload, WINDOW, NODE_TYPES, retain_source_text=True)
    assert len(nodes) == 1
    n = nodes[0]
    assert n.node_type == "event"                      # still typed
    assert n.title == "Caroline adopted a greyhound named Pepper"
    assert "Source (verbatim):" in n.body              # word-searchable
    assert "She sleeps twenty hours a day." in n.body  # the words the summary dropped


def test_retention_off_leaves_body_untouched():
    payload = {"memories": [{"local_id": "m1", "node_type": "note",
                             "title": "A note about Pepper", "body": "Pepper is a greyhound.",
                             "source_turn_ids": ["D1:1"]}], "edges": []}
    nodes, _ = _drafts_from_payload(payload, WINDOW, NODE_TYPES, retain_source_text=False)
    assert "Source (verbatim):" not in nodes[0].body


def test_llm_retained_node_is_not_the_verbatim_floor():
    """The point is a node that is BOTH typed and word-searchable, not a copy of
    the floor. The LLM node is one typed node carrying a summary + quote; the
    floor emits one raw untyped note per turn."""
    payload = {"memories": [{"local_id": "m1", "node_type": "event",
                             "title": "Caroline adopted Pepper",
                             "body": "Caroline adopted a greyhound named Pepper.",
                             "source_turn_ids": ["D1:1"]}], "edges": []}
    llm_nodes, _ = _drafts_from_payload(payload, WINDOW, NODE_TYPES, retain_source_text=True)
    verb_nodes = VerbatimExtractor().extract(WINDOW).nodes
    # One typed node vs three raw notes: different shapes, not identical arms.
    assert len(llm_nodes) == 1 and llm_nodes[0].node_type == "event"
    assert len(verb_nodes) == 3 and all(n.node_type == "note" for n in verb_nodes)
    assert llm_nodes[0].title != verb_nodes[0].title


def test_render_shows_turn_ids_only_when_asked():
    with_ids = _render_window(WINDOW, NODE_TYPES, ("relates_to",), show_turn_ids=True)
    without = _render_window(WINDOW, NODE_TYPES, ("relates_to",), show_turn_ids=False)
    assert "[D1:1] Caroline:" in with_ids
    assert "[D1:1]" not in without
    # The transcript text itself is present either way; only the id prefix moves.
    assert "I adopted a greyhound named Pepper." in with_ids
    assert "I adopted a greyhound named Pepper." in without


# --- width-matched traverse ------------------------------------------------
def test_traverse_seed_width_matches_search_baseline():
    """The invariant that makes the multi-hop comparison interpretable: the
    traverse arm seeds at the same width the SearchOnly baseline searches."""
    assert SearchThenTraverse().seed_limit == SearchOnly().limit


def test_traverse_walks_every_seed_by_default():
    """Walking only the top-k reintroduces the width confound; the default walks
    all seeds."""
    assert SearchThenTraverse().walk_seeds is None


def test_extract_prompt_assembles_and_stays_test_neutral():
    """The extraction prompt loads and carries the fidelity guidance -- and that
    guidance must be GENERAL, never naming a dataset fact/object/person. The
    provenance is clean even though it was motivated by observed failures."""
    import re

    from bench.core.llm import load_prompt, prompt_hash

    text = load_prompt("extract.md")
    assert "Preserve concrete specifics" in text
    assert prompt_hash("extract.md").startswith("sha256:")
    # No LoCoMo-specific terms may leak into a shipped extraction prompt.
    banned = ["palm", "sunset", "greyhound", "pepper", "melanie", "caroline",
              "pottery", "clarinet", "charlotte", "becoming nicole", "doordash"]
    leaked = [w for w in banned if re.search(rf"\b{re.escape(w)}\b", text, re.I)]
    assert not leaked, f"extraction prompt leaked dataset terms: {leaked}"
