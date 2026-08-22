"""Reader-input rendering (bench/core/answer.py::render_envelope).

No LLM, no DB. The renderer is a pure function; these tests pin the two
properties that matter -- it is FAITHFUL (no title, body, or attribute is
dropped; provenance renders only fields the node carries) and it strips only
SCAFFOLDING (ids, status, raw scores/labels) -- plus the one-line provenance
(recorded-date · author · scope), rank order, the three strategy shapes,
empty-result signalling, and determinism.
"""
from __future__ import annotations

from bench.core.answer import RENDER_FORMAT_VERSION, render_envelope


def _node(nid, title, body="", ntype="fact", **extra):
    n = {
        "id": nid,
        "type": ntype,
        "scope": "hs-conv-26-llm",
        "title": title,
        "body": body,
        "attrs": {},
        "status": "active",
        "author": "bench",
        "created_at": "2026-07-23T16:14:35.775969+00:00",
    }
    n.update(extra)
    return n


def _search_env(nodes):
    return {
        "v": 1,
        "detail": "full",
        "results": [
            {"node": n, "score": 0.0163 - i * 0.001, "similarity": 0.8,
             "edge_count": 2}
            for i, n in enumerate(nodes)
        ],
        "scopes_searched": ["hs-conv-26-llm"],
        "truncated": False,
    }


# ---- faithfulness: every piece of content survives -------------------------

def test_titles_and_bodies_all_present():
    env = _search_env([
        _node("d0d6d7ae-860f-4e28-955a-445dbb8f35f2", "Caroline plans to study",
              "Caroline: Gonna continue my edu and check out career options."),
        _node("b7150520-efff-477d-8488-376bf8667a76", "Melanie painted a sunrise",
              "Melanie painted a lake sunrise in 2022."),
    ])
    out = render_envelope(env)
    for frag in ("Caroline plans to study",
                 "Caroline: Gonna continue my edu and check out career options.",
                 "Melanie painted a sunrise",
                 "Melanie painted a lake sunrise in 2022."):
        assert frag in out, frag


def test_non_empty_attrs_are_surfaced():
    # attrs can carry answer-bearing data (a date); it must not be stripped.
    env = _search_env([
        _node("id-1", "Trip to Kyoto", "They visited Kyoto.",
              attrs={"occurred": "2023-04-10"}),
    ])
    out = render_envelope(env)
    assert "occurred: 2023-04-10" in out


def test_verbatim_source_marker_preserved():
    body = ("Melanie paints to relax.\n\nSource (verbatim):\n"
            "Melanie: Painting's a fun way to express my feelings.")
    out = render_envelope(_search_env([_node("id-1", "Melanie paints", body)]))
    assert "Source (verbatim):" in out
    assert "Melanie: Painting's a fun way to express my feelings." in out


# ---- scaffolding is stripped -----------------------------------------------

def test_scaffolding_removed():
    env = _search_env([
        _node("d0d6d7ae-860f-4e28-955a-445dbb8f35f2", "A title", "A body"),
    ])
    out = render_envelope(env)
    # uuid, status, the RAW iso timestamp, raw scores and internal field labels:
    # none reach the reader. (scope/author/recorded-date DO appear, but only as
    # the one compact provenance line -- see test_provenance_line.)
    for scaffold in ("d0d6d7ae-860f-4e28-955a-445dbb8f35f2",
                     "2026-07-23T16:14", "0.0163", "similarity",
                     "edge_count", "status"):
        assert scaffold not in out, scaffold


# ---- provenance line -------------------------------------------------------

def test_provenance_line():
    env = _search_env([_node("id-1", "A title", "A body")])
    out = render_envelope(env)
    # one compact line: recorded-date (readable, not raw iso) · by author · in scope
    assert "recorded 23 Jul 2026" in out       # 2026-07-23T... -> readable date
    assert "by bench" in out                    # author
    assert "in hs-conv-26-llm" in out           # scope / session
    # honest labelling: it is the RECORDED date, never presented as when it occurred
    assert "occurred" not in out.lower()


def test_provenance_omits_absent_fields_never_invents():
    node = {"type": "fact", "title": "bare fact", "body": "b", "attrs": {}}
    # no created_at / author / scope on this node
    out = render_envelope(_search_env([node]))
    assert "bare fact" in out
    for invented in ("recorded", "by ", "in hs-", "None"):
        assert invented not in out, invented


def test_provenance_date_falls_back_when_unparseable():
    env = _search_env([_node("id-1", "t", "b", created_at="not-a-date")])
    out = render_envelope(env)
    assert "not-a-date" in out          # raw value kept, not guessed at
    assert "by bench" in out


# ---- rank order preserved --------------------------------------------------

def test_rank_order_preserved():
    env = _search_env([
        _node("id-1", "FIRST fact"),
        _node("id-2", "SECOND fact"),
        _node("id-3", "THIRD fact"),
    ])
    out = render_envelope(env)
    assert out.index("[1]") < out.index("[2]") < out.index("[3]")
    assert out.index("FIRST fact") < out.index("SECOND fact") < out.index("THIRD fact")


# ---- empty results are a signal, not silence -------------------------------

def test_empty_results_signalled():
    out = render_envelope(_search_env([]))
    assert "(no memories retrieved)" in out


# ---- strategy shapes -------------------------------------------------------

def test_search_then_traverse_labels_graph_neighbours():
    env = _search_env([_node("id-1", "Seed hit", "seed body")])
    env["traversed"] = [
        _node("id-2", "Melanie, friend of Caroline", ntype="person", depth=1),
    ]
    # traversed nodes are summary detail (no body)
    env["traversed"][0]["body"] = ""
    out = render_envelope(env)
    assert "Seed hit" in out
    assert "Melanie, friend of Caroline" in out
    assert "graph links" in out
    assert "reached via 1 link" in out


def test_briefing_then_search_shape():
    env = {
        "briefing": {
            "v": 1,
            "sections": [
                {"name": "standing_preferences", "nodes": []},
                {"name": "relevant",
                 "nodes": [_node("id-b", "Melanie enjoys painting",
                                 "Painting is her way to relax.")]},
            ],
            "footer": {"inbox_pending": 0},
        },
        "search": _search_env([_node("id-s", "Caroline studies psychology",
                                     "She wants a counseling certification.")]),
    }
    out = render_envelope(env)
    assert "standing_preferences" in out
    assert "(no memories retrieved)" in out          # the empty section is shown
    assert "Melanie enjoys painting" in out          # briefing content
    assert "Caroline studies psychology" in out      # search content
    assert "She wants a counseling certification." in out
    # briefing section content precedes the search block
    assert out.index("Melanie enjoys painting") < out.index("Caroline studies psychology")


# ---- robustness ------------------------------------------------------------

def test_unknown_shape_never_loses_content():
    weird = {"mystery": [{"title": "kept anyway", "value": 42}]}
    out = render_envelope(weird)
    assert "kept anyway" in out


def test_deterministic():
    env = _search_env([
        _node("id-1", "One", "b1", attrs={"z": "1", "a": "2"}),
        _node("id-2", "Two", "b2"),
    ])
    assert render_envelope(env) == render_envelope(env)


def test_format_version_is_a_string():
    assert isinstance(RENDER_FORMAT_VERSION, str) and RENDER_FORMAT_VERSION
