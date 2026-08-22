"""Read-time near-duplicate collapse (engraphy/core/search.py).

Two layers:
 * `_collapse_by_pairs` -- the pure walk, tested exhaustively without a DB.
 * an end-to-end DB test that two near-verbatim active nodes collapse to one in
   a real search while a distinct node survives, at the shipped 0.95 threshold.

The threshold itself is NOT tuned here: it is the dedup merge band (`t_high`,
0.95), asserted to be the constant in use so a silent drift is caught.
"""
from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb

from engraphy.core import embedding as _emb
from engraphy.core.search import _READ_DEDUP_SIM, _collapse_by_pairs, search


# ---- pure collapse logic (no DB) -------------------------------------------

def test_threshold_is_the_merge_band():
    # read-time collapse reuses the write path's "same fact" bar, not a tuned one
    assert _READ_DEDUP_SIM == 0.95


def test_keeps_highest_ranked_of_a_near_dup_pair():
    fused = [("A", 3.0), ("B", 2.0), ("C", 1.0)]
    near = {frozenset({"A", "B"})}          # A and B are the same fact
    kept = _collapse_by_pairs(fused, near)
    assert [nid for nid, _ in kept] == ["A", "C"]   # B dropped, A (higher) kept


def test_no_pairs_keeps_everything():
    fused = [("A", 3.0), ("B", 2.0), ("C", 1.0)]
    assert _collapse_by_pairs(fused, set()) == fused


def test_distinct_nodes_all_survive():
    fused = [("A", 3.0), ("B", 2.0)]
    assert _collapse_by_pairs(fused, set()) == fused


def test_only_lower_ranked_member_is_dropped():
    # rank order B, A, C ; A~C are dups -> A kept (higher), C dropped
    fused = [("B", 3.0), ("A", 2.0), ("C", 1.0)]
    near = {frozenset({"A", "C"})}
    assert [n for n, _ in _collapse_by_pairs(fused, near)] == ["B", "A"]


def test_cluster_of_three_collapses_to_the_top_one():
    fused = [("A", 3.0), ("B", 2.0), ("C", 1.0)]
    near = {frozenset({"A", "B"}), frozenset({"B", "C"}), frozenset({"A", "C"})}
    assert [n for n, _ in _collapse_by_pairs(fused, near)] == ["A"]


def test_order_is_preserved():
    fused = [("A", 3.0), ("B", 2.9), ("C", 2.0), ("D", 1.0)]
    near = {frozenset({"B", "D"})}          # D is a dup of B
    assert [n for n, _ in _collapse_by_pairs(fused, near)] == ["A", "B", "C"]


# ---- end-to-end against a real store ---------------------------------------
# Self-contained space bootstrap + raw seeding (same pattern as test_search.py),
# so two ACTIVE duplicate rows exist WITHOUT the write path merging them -- which
# is exactly the situation read-time collapse targets (cross-scope writes, or a
# confirm-band `distinct` resolution, leave paraphrases of one fact co-active).

def _bootstrap(conn, space_id):
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'S')", (space_id,))
    cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p1', 'P')",
                (space_id,))
    cur.execute("INSERT INTO node_types (space_id, name, description, attr_spec) "
                "VALUES (%s, 'note', 'n', %s)",
                (space_id, Jsonb({"attrs": {"closed": False}})))
    # same_topic / relates_to so the Phase B collapse-exemption tests can attach a
    # declared-distinct edge between two note nodes (§4).
    cur.execute("INSERT INTO edge_types (space_id, name, description, bidirectional) VALUES "
                "(%s, 'same_topic', 'Same topic, distinct content.', true), "
                "(%s, 'relates_to', 'Generic association.', true)", (space_id, space_id))
    cur.execute("INSERT INTO edge_rules (space_id, type, src_type, dst_type) VALUES "
                "(%s, 'same_topic', 'note', 'note'), (%s, 'relates_to', 'note', 'note')",
                (space_id, space_id))
    cur.execute("INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
                "VALUES (%s, 'scope1', 'S1', 'p1', 'private')", (space_id,))
    conn.commit()


def _cleanup(conn, space_id):
    cur = conn.cursor()
    for t in ("audit_log", "dedup_log", "edges", "nodes", "scopes", "edge_rules",
              "edge_types", "node_types", "principals"):
        cur.execute(f"DELETE FROM {t} WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


def _link(conn, space_id, src_id, dst_id, edge_type):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, %s)",
        (space_id, src_id, dst_id, edge_type),
    )
    conn.commit()


def _seed(conn, space_id, title, body):
    vec = _emb.embed_document(title + "\n" + body)
    lit = "[" + ",".join(str(x) for x in vec) + "]"
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
        "embedding_model, source_client, author_principal) "
        "VALUES (%s, 'note', 'scope1', %s, %s, %s, %s::vector, %s, 'pytest', 'p1') RETURNING id",
        (space_id, title, body, Jsonb({}), lit, _emb.MODEL_ID),
    )
    (nid,) = cur.fetchone()
    conn.commit()
    return str(nid)


@pytest.fixture
def dedup_space(conn, request):
    space_id = ("rd-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap(conn, space_id)
    yield space_id
    _cleanup(conn, space_id)


async def test_duplicate_active_nodes_collapse_in_search(pool, dedup_space, conn):
    """Two active nodes with identical text (cosine 1.0 >= 0.95) collapse to one
    in a real search; a distinct node survives. Deterministic: it does not lean on
    a fragile near-cosine value, only on the >=/< sides of the threshold."""
    dup_a = _seed(conn, dedup_space, "Sam drinks tea", "Sam drinks tea every morning.")
    dup_b = _seed(conn, dedup_space, "Sam drinks tea", "Sam drinks tea every morning.")
    distinct = _seed(conn, dedup_space, "Priya is a nurse", "Priya works as a nurse in Leeds.")

    on = await search(pool, dedup_space, "p1", "scope1", "what does Sam drink",
                      "pytest", collapse_near_dupes=True)
    off = await search(pool, dedup_space, "p1", "scope1", "what does Sam drink",
                       "pytest", collapse_near_dupes=False)

    off_ids = {r["node"]["id"] for r in off["results"]}
    on_ids = {r["node"]["id"] for r in on["results"]}
    assert {dup_a, dup_b} <= off_ids          # both duplicates present without collapse
    assert len(on_ids & {dup_a, dup_b}) == 1  # exactly one survives the collapse
    assert distinct in on_ids                 # the distinct fact is never dropped
    assert len(on["results"]) == len(off["results"]) - 1


async def test_same_topic_pair_is_exempt_from_collapse(pool, dedup_space, conn):
    """Phase B §4: a `same_topic`-linked pair is >=0.95 similar by construction
    (a merge-link member and its canonical), so the read-time collapse WOULD drop
    one -- re-hiding exactly the fact merge-link preserved. The exemption keeps
    both. Same fixture shape as the plain-collapse test, only with the edge."""
    member = _seed(conn, dedup_space, "Sam drinks tea", "Sam drinks tea every morning.")
    canonical = _seed(conn, dedup_space, "Sam drinks tea", "Sam drinks tea every morning.")
    _link(conn, dedup_space, member, canonical, "same_topic")

    on = await search(pool, dedup_space, "p1", "scope1", "what does Sam drink",
                      "pytest", collapse_near_dupes=True)
    on_ids = {r["node"]["id"] for r in on["results"]}
    assert {member, canonical} <= on_ids, "the same_topic pair is exempt: BOTH survive"


async def test_relates_to_pair_is_exempt_from_collapse(pool, dedup_space, conn):
    """§4: `relates_to` is the same 'declared distinct' assertion (a human/agent
    `distinct` resolution, or a hand-drawn association), so a >=0.95 relates_to
    pair also survives the collapse."""
    a = _seed(conn, dedup_space, "Sam drinks tea", "Sam drinks tea every morning.")
    b = _seed(conn, dedup_space, "Sam drinks tea", "Sam drinks tea every morning.")
    _link(conn, dedup_space, a, b, "relates_to")

    on = await search(pool, dedup_space, "p1", "scope1", "what does Sam drink",
                      "pytest", collapse_near_dupes=True)
    on_ids = {r["node"]["id"] for r in on["results"]}
    assert {a, b} <= on_ids, "the relates_to pair is exempt: BOTH survive"
