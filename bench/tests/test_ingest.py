"""Ingest against a live database (design/09 §Ingest, §Isolation).

The isolation gate is the acceptance criterion here, not a nicety: if two
haystacks can see each other's nodes, every dedup number and every accuracy
number in the harness is wrong, and wrong in the flattering direction.

Embeddings are real. These tests load the actual model, so they are slower than
the engine's unit tests -- but a fake embedding would make band behavior
meaningless, and band behavior is most of what is under test.
"""

from __future__ import annotations

import math
import psycopg
import pytest

from engraphy.core import dedup

from bench.core.corpus import Haystack, Session, Turn
from engraphy.tests import bandvalues as bv
from bench.core.extract import ExtractResult, NodeDraft, VerbatimExtractor, window_sessions
from bench.core.ingest import (
    AlwaysDistinct,
    ConfirmDecision,
    IngestStats,
    _record_outcome,
    ingest_haystack,
)
from bench.core.space import (
    BENCH_PACK_PATH,
    IsolationError,
    assert_no_ambient_scopes,
    load_bench_pack,
    scope_id_for,
)
from bench.tests.conftest import DATABASE_URL


def _haystack(hid, sessions):
    return Haystack(
        haystack_id=hid,
        sessions=tuple(
            Session(sid, tuple(Turn(speaker=sp, text=tx, turn_id=f"{sid}:{i}")
                               for i, (sp, tx) in enumerate(turns)))
            for sid, turns in sessions
        ),
    )


# --------------------------------------------------------------------------
# The bench pack's load-bearing property
# --------------------------------------------------------------------------


def test_bench_pack_declares_no_ambient_scopes():
    """An ambient scope is unioned into every write's dedup candidate set, which
    would let one haystack's facts become merge candidates for another's. The
    key must be absent, not empty."""
    pack = load_bench_pack()
    assert "ambient_scopes" not in pack
    assert pack["pack"] == "bench"


def test_ambient_scopes_are_refused_if_reintroduced():
    with pytest.raises(IsolationError, match="ambient_scopes"):
        assert_no_ambient_scopes({"pack": "x", "ambient_scopes": ["personal"]})


def test_bench_pack_file_has_not_grown_an_ambient_key():
    """Guards the YAML itself, not just the loader -- the failure mode is a
    careless edit to the pack file."""
    text = BENCH_PACK_PATH.read_text(encoding="utf-8")
    live = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("ambient_scopes" in ln for ln in live)


# --------------------------------------------------------------------------
# The isolation gate
# --------------------------------------------------------------------------


async def test_record_outcome_counts_merged_linked_as_a_node(pool):
    """Phase B: a `merged_linked` outcome is a real member row, not a write error.
    The bench must count it and map the draft's local id to the MEMBER node id so
    the extractor's edges attach to the fact that was actually written. (The
    direct merge-link branch uses no pool -- a stub envelope is enough.)"""
    stats = IngestStats()
    draft = NodeDraft(local_id="d1", node_type="note", title="A promoted fact",
                      body="Priya was promoted to charge nurse in June.")
    envelope = {
        "v": 1, "outcome": "merged_linked",
        "node": {"id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301", "type": "note"},
        "canonical": {"id": "6ba7b810-9dad-41d1-80b4-00c04fd430c8"},
        "similarity": 0.96, "cluster_edge_added": True,
    }
    node_id = await _record_outcome(pool, None, envelope, draft, AlwaysDistinct(), stats)
    assert node_id == "3f2504e0-4f89-41d3-9a0c-0305e82c3301"  # the MEMBER, not canonical
    assert stats.merged_linked == 1
    assert stats.nodes_written == 1  # a member row counts like an insert
    assert stats.write_errors == {}, "merged_linked is a success, never a write error"


async def test_identical_facts_within_one_haystack_do_merge(pool, bench_space):
    """The positive control, and the half that makes the isolation gate mean
    something.

    Without this, the negative test ("two haystacks don't merge") would stay
    green if dedup silently stopped merging altogether -- it would be passing
    for the wrong reason. Together the pair pins scoping as the *cause*: same
    text in the same scope merges, the same text in a different scope does not.
    It also exercises the merge accounting that every dedup number depends on.
    """
    rs = bench_space(["conv-a"])
    fact = [("Caroline", "I adopted a greyhound named Pepper last spring.")]

    first = await ingest_haystack(pool, rs, _haystack("conv-a", [("s1", fact)]), VerbatimExtractor())
    second = await ingest_haystack(pool, rs, _haystack("conv-a", [("s2", fact)]), VerbatimExtractor())

    assert first.inserted == 1
    # Byte-identical text is similarity 1.0 -- comfortably above t_high (0.95).
    assert second.merged == 1, f"identical text in one scope did not merge: {second.as_dict()}"
    assert second.inserted == 0
    assert second.nodes_written == 0

    with psycopg.connect(DATABASE_URL) as c:
        cur = c.cursor()
        cur.execute(
            "SELECT count(*) FROM nodes WHERE space_id = %s AND status = 'active'",
            (rs.space_id,),
        )
        assert cur.fetchone()[0] == 1


async def test_identical_facts_in_two_haystacks_stay_separate(pool, bench_space):
    """THE acceptance gate (design/09 §Isolation). The same sentence ingested
    into two haystacks must produce two distinct active nodes -- if it merges,
    scoping is not containing dedup and every number the harness produces is
    contaminated."""
    rs = bench_space(["conv-a", "conv-b"])
    fact = [("Caroline", "I adopted a greyhound named Pepper last spring.")]

    stats_a = await ingest_haystack(pool, rs, _haystack("conv-a", [("s1", fact)]), VerbatimExtractor())
    stats_b = await ingest_haystack(pool, rs, _haystack("conv-b", [("s1", fact)]), VerbatimExtractor())

    assert stats_a.nodes_written == 1
    # Byte-identical text: without scope isolation this is a guaranteed merge at
    # similarity 1.0, so this assertion is the whole gate.
    assert stats_b.merged == 0, "identical fact merged across haystacks -- isolation is broken"
    assert stats_b.nodes_written == 1

    with psycopg.connect(DATABASE_URL) as c:
        cur = c.cursor()
        cur.execute(
            "SELECT scope_id, count(*) FROM nodes WHERE space_id = %s AND status = 'active' "
            "GROUP BY scope_id ORDER BY scope_id",
            (rs.space_id,),
        )
        rows = dict(cur.fetchall())
    assert rows == {scope_id_for("conv-a"): 1, scope_id_for("conv-b"): 1}


async def test_scope_id_collision_is_fatal_not_silent():
    """Two haystacks slugging onto one scope would silently merge two corpora."""
    import psycopg as _pg

    with _pg.connect(DATABASE_URL, autocommit=True) as c:
        with pytest.raises(IsolationError, match="lossy"):
            from bench.core.space import provision_run_space

            provision_run_space(
                c,
                run_id="collision_probe",
                # Differ only past the 48-char truncation point.
                haystack_ids=["x" * 60 + "aaa", "x" * 60 + "bbb"],
            )


# --------------------------------------------------------------------------
# The write path is the real one
# --------------------------------------------------------------------------


async def test_verbatim_ingest_writes_one_note_per_turn(pool, bench_space):
    rs = bench_space(["conv-a"])
    h = _haystack("conv-a", [("s1", [
        ("Caroline", "I finally adopted a greyhound, her name is Pepper."),
        ("Melanie", "That is wonderful, how is she settling in?"),
        ("Caroline", "She sleeps roughly twenty hours every single day."),
    ])])

    stats = await ingest_haystack(pool, rs, h, VerbatimExtractor())

    assert stats.drafts == 3
    assert stats.write_errors == {}
    with psycopg.connect(DATABASE_URL) as c:
        cur = c.cursor()
        cur.execute(
            "SELECT type, source_client, source_session FROM nodes "
            "WHERE space_id = %s AND status = 'active'",
            (rs.space_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == stats.nodes_written
    assert {r[0] for r in rows} == {"note"}
    assert {r[1] for r in rows} == {"engraphy-bench"}
    # Provenance survives into the store, so a surprising result can be traced
    # back to the session that produced it.
    assert {r[2] for r in rows} == {"s1"}


async def test_verbatim_extractor_emits_no_edges(pool, bench_space):
    """The floor's defining property: no edges, so traverse has nothing to walk
    and multi-hop should sit at the search-only baseline. That is the
    demonstration that graph structure must be written before it is traversed --
    if this ever passes with edges, the floor has stopped being a floor."""
    rs = bench_space(["conv-a"])
    h = _haystack("conv-a", [("s1", [("A", "The first fact about sailing."),
                                     ("B", "A completely unrelated fact about bread.")])])
    stats = await ingest_haystack(pool, rs, h, VerbatimExtractor())

    assert VerbatimExtractor().emits_edges is False
    assert stats.edges_requested == 0
    with psycopg.connect(DATABASE_URL) as c:
        cur = c.cursor()
        cur.execute("SELECT count(*) FROM edges WHERE space_id = %s", (rs.space_id,))
        assert cur.fetchone()[0] == 0


async def test_ingest_uses_the_real_bands_and_records_the_confirm_rate(pool, bench_space, monkeypatch):
    """The band distribution is reported, and the confirm band is resolved
    rather than dropped. Uses a stub embedder to place similarities exactly --
    the point under test is the harness's branching, not the model's geometry."""
    rs = bench_space(["conv-a"])

    # Three drafts: two effectively identical (merge band), one orthogonal.
    def fake_embed(text: str):
        vec = [0.0] * 384
        if "orthogonal" in text:
            vec[1] = 1.0
        elif "near duplicate" in text:
            # Squarely inside the active profile's confirm band, so the harness
            # branch under test is the one a real near-duplicate would take. The
            # second component is whatever makes the vector a unit vector.
            vec[0] = bv.PENDING
            vec[2] = math.sqrt(1.0 - bv.PENDING ** 2)
        else:
            vec[0] = 1.0
        return vec

    h = _haystack("conv-a", [("s1", [
        ("A", "base fact about the harbour"),
        ("A", "a near duplicate of the harbour fact"),
        ("A", "an orthogonal statement entirely"),
    ])])

    stats = await ingest_haystack(pool, rs, h, VerbatimExtractor(), embed_document=fake_embed)

    assert stats.drafts == 3
    assert stats.needs_confirmation == 1, stats.as_dict()
    assert 0.0 < stats.confirm_band_rate < 1.0
    assert stats.resolved_distinct == 1
    # AlwaysDistinct never destroys a fact: all three survive.
    assert stats.nodes_written == 3
    assert stats.write_errors == {}


async def test_confirm_band_pending_is_never_left_parked(pool, bench_space):
    """A `needs_confirmation` write stores nothing until resolved. If the
    harness ever left one parked, the fact would be silently missing from the
    store and would look like a retrieval failure downstream."""
    rs = bench_space(["conv-a"])

    def fake_embed(text: str):
        vec = [0.0] * 384
        if "second" in text:
            vec[0], vec[2] = 0.87, 0.493
        else:
            vec[0] = 1.0
        return vec

    h = _haystack("conv-a", [("s1", [("A", "the first statement"), ("A", "the second statement")])])
    await ingest_haystack(pool, rs, h, VerbatimExtractor(), embed_document=fake_embed)

    with psycopg.connect(DATABASE_URL) as c:
        cur = c.cursor()
        cur.execute("SELECT count(*) FROM pending_writes WHERE space_id = %s", (rs.space_id,))
        assert cur.fetchone()[0] == 0


async def test_ingest_never_review_queues(pool, bench_space, monkeypatch):
    """`review_queued` is the import_mode surface. Reaching it means someone
    reintroduced the bulk bypass, which drops confirm-band facts entirely."""
    rs = bench_space(["conv-a"])
    seen = {}

    real_write = dedup.write

    async def spy(*args, **kwargs):
        seen.update(kwargs)
        return await real_write(*args, **kwargs)

    monkeypatch.setattr(dedup, "write", spy)
    h = _haystack("conv-a", [("s1", [("A", "a fact worth keeping about tides")])])
    stats = await ingest_haystack(pool, rs, h, VerbatimExtractor())

    assert stats.write_errors == {}
    # The three forbidden knobs are never passed.
    assert "import_mode" not in seen
    assert "thresholds" not in seen
    assert "resonance_floor" not in seen


# --------------------------------------------------------------------------
# Policy and stats plumbing (no DB)
# --------------------------------------------------------------------------


def test_always_distinct_never_asks_for_a_merge():
    d = AlwaysDistinct().decide({"outcome": "needs_confirmation", "candidates": []},
                                NodeDraft("l1", "note", "A title here", "A body."))
    assert d.resolution == "distinct"
    assert d.merge_into is None


def test_merge_decision_requires_a_target():
    with pytest.raises(ValueError, match="merge_into"):
        ConfirmDecision(resolution="merge")


def test_always_merge_is_not_implemented():
    """Ruled 2026-07-21 (Devon): AlwaysMerge is not to exist, not even as a
    comparison arm -- it is the one policy that can destroy facts, and shipping
    it would leave it one config flag away from a published number."""
    import bench.core.ingest as ingest_mod

    names = [n for n in dir(ingest_mod) if "merge" in n.lower() and n[0].isupper()]
    assert names == [], f"a merge-all confirm policy has appeared: {names}"


def test_windowing_splits_long_sessions_with_overlap():
    turns = tuple(Turn(speaker="A", text=f"utterance number {i}") for i in range(100))
    s = Session("s1", turns)
    ws = window_sessions("h", s, max_turns=40, overlap=4)
    assert len(ws) == 3
    assert all(len(w.turns) <= 40 for w in ws)
    # The seam overlaps, so a fact and the turn that qualifies it are not split.
    assert ws[0].turns[-4:] == ws[1].turns[:4]


def test_short_session_is_one_window():
    s = Session("s1", (Turn(speaker="A", text="only one"),))
    assert len(window_sessions("h", s)) == 1


def test_extract_window_never_carries_prior_bodies():
    """An extractor sees prior titles only. Handing it prior bodies would make
    ingest cost quadratic and let late sessions rewrite early ones."""
    s = Session("s1", (Turn(speaker="A", text="hello there"),))
    (w,) = window_sessions("h", s, prior_titles=("an earlier title",))
    assert w.prior_titles == ("an earlier title",)
    assert not hasattr(w, "prior_bodies")


def test_verbatim_titles_stay_within_ddl_bounds():
    long_turn = Turn(speaker="A", text="word " * 500)
    (w,) = window_sessions("h", Session("s1", (long_turn,)))
    (draft,) = VerbatimExtractor().extract(w).nodes
    assert 3 <= len(draft.title) <= 200
    assert 1 <= len(draft.body) <= 8000


def test_ingest_stats_serialize_with_the_confirm_rate():
    from bench.core.ingest import IngestStats

    st = IngestStats(haystack_id="h", drafts=10, needs_confirmation=3)
    d = st.as_dict()
    assert d["confirm_band_rate"] == 0.3
    assert d["needs_confirmation"] == 3


def test_extract_result_carries_token_counts_for_the_manifest():
    r = ExtractResult(input_tokens=100, output_tokens=20)
    assert (r.input_tokens, r.output_tokens) == (100, 20)


async def test_unresolvable_supersede_is_counted_not_miscounted(pool, bench_space):
    """A supersede whose target cannot be resolved is downgraded to a plain
    write and counted as an extraction failure -- not as a generic write error.

    The distinction is the point: `supersede_target_unresolved` is the honest
    measure of how much of the knowledge-update result is extraction rather than
    engine, and it is worthless if a dangling reference lands in a different
    bucket. Exercised with a hand-built draft because VerbatimExtractor never
    emits supersede intents.
    """
    from bench.core.ingest import _write_draft

    rs = bench_space(["conv-a"])
    draft = NodeDraft(
        local_id="d1",
        node_type="note",
        title="A replacement fact about the harbour",
        body="The harbour closed for repairs in March.",
        supersedes_local_id="never-written-draft",
    )
    stats = __import__("bench.core.ingest", fromlist=["IngestStats"]).IngestStats()
    node_id = await _write_draft(
        pool, rs, rs.scope_for("conv-a"), draft, {}, {}, AlwaysDistinct(), stats,
        embed_document=lambda t: [1.0] + [0.0] * 383, session_id="s1",
    )

    assert stats.supersede_attempted == 1
    assert stats.supersede_target_unresolved == 1
    assert stats.write_errors == {}, "a dangling supersede ref must not read as a write error"
    # Downgraded to a plain write: the fact still lands.
    assert node_id is not None
    assert stats.inserted == 1


def test_extract_result_is_reexported_for_tests():
    assert ExtractResult().nodes == ()
