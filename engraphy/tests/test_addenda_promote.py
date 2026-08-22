"""engraphy.admin.addenda.promote_addenda -- Phase B §3 (recover facts buried as
get-only addenda in existing stores). Runs on the sync superuser-style `conn` the
other admin tests use; embeddings are injected (deterministic) so the tests do not
load the model.
"""
import pytest
from psycopg.types.json import Jsonb

from engraphy.admin.addenda import _derive_member_title, promote_addenda

_VEC = [1.0] + [0.0] * 383
_VEC_LIT = "[" + ",".join(str(x) for x in _VEC) + "]"


def _embed(_text):
    return list(_VEC)


def _bootstrap(conn, space_id, with_same_topic_rule=True):
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'S')", (space_id,))
    cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p1', 'P')",
                (space_id,))
    cur.execute("INSERT INTO node_types (space_id, name, description, attr_spec) "
                "VALUES (%s, 'note', 'n', %s)",
                (space_id, Jsonb({"attrs": {"closed": False}})))
    cur.execute("INSERT INTO edge_types (space_id, name, description, bidirectional) VALUES "
                "(%s, 'same_topic', 'Same topic, distinct content.', true)", (space_id,))
    if with_same_topic_rule:
        cur.execute("INSERT INTO edge_rules (space_id, type, src_type, dst_type) VALUES "
                    "(%s, 'same_topic', 'note', 'note')", (space_id,))
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


def _addendum(body, merged_at="2026-01-15T10:00:00+00:00", **extra):
    a = {
        "merged_at": merged_at,
        "source_client": "old-client",
        "source_session": "old-sess",
        "author_principal": "p1",
        "body": body,
    }
    a.update(extra)
    return a


def _seed_canonical(conn, space_id, body, addenda, title="Canonical", scope="scope1"):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
        "embedding_model, source_client, author_principal) "
        "VALUES (%s, 'note', %s, %s, %s, %s, %s::vector, 'test-model', 'pytest', 'p1') "
        "RETURNING id",
        (space_id, scope, title, body, Jsonb({"addenda": addenda}), _VEC_LIT),
    )
    (nid,) = cur.fetchone()
    conn.commit()
    return str(nid)


@pytest.fixture
def space(conn, request):
    space_id = ("ap-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap(conn, space_id)
    yield space_id
    _cleanup(conn, space_id)


# --- title derivation (pure) -------------------------------------------------

def test_derive_title_first_sentence():
    assert _derive_member_title("Priya moved to research. She left nursing.", "X") == \
        "Priya moved to research"


def test_derive_title_no_terminator_uses_whole_body():
    assert _derive_member_title("Priya works as a nurse", "X") == "Priya works as a nurse"


def test_derive_title_truncates_over_200_chars():
    long = "a" * 250
    got = _derive_member_title(long, "Canonical")
    assert len(got) == 200 and got.endswith("…")


def test_derive_title_degenerate_falls_back_to_canonical():
    got = _derive_member_title(".", "Canonical title for the fact")
    assert got == "Canonical title for the fact — addendum"


# --- promote mechanics -------------------------------------------------------

def test_novel_addendum_is_promoted_with_edge_marker_and_dedup_log(space, conn):
    novel = "A distinct new fact about an entirely separate matter worth keeping."
    canonical = _seed_canonical(conn, space, "The canonical fact about the topic.",
                                [_addendum(novel)])

    summary = promote_addenda(conn, space, embed_document=_embed)
    assert summary.promoted == 1
    assert summary.skipped_non_novel == 0
    assert summary.edges_skipped_missing_rule == 0

    cur = conn.cursor()
    # a member node exists carrying the addendum body, created_at = merged_at.
    cur.execute("SELECT id, body, created_at, scope_id, type FROM nodes "
                "WHERE space_id = %s AND id <> %s", (space, canonical))
    member_id, body, created_at, scope, ntype = cur.fetchone()
    assert body == novel
    assert scope == "scope1" and ntype == "note"
    assert created_at.isoformat() == "2026-01-15T10:00:00+00:00"
    # same_topic edge member -> canonical.
    cur.execute("SELECT count(*) FROM edges WHERE space_id = %s AND type = 'same_topic' "
                "AND src_id = %s AND dst_id = %s", (space, member_id, canonical))
    assert cur.fetchone()[0] == 1
    # dedup_log band merge_linked_promoted, similarity NULL.
    cur.execute("SELECT band, similarity, candidate_id FROM dedup_log WHERE space_id = %s", (space,))
    band, sim, cand = cur.fetchone()
    assert band == "merge_linked_promoted" and sim is None and str(cand) == canonical
    # the addendum is retained but marked (not deleted).
    cur.execute("SELECT attrs -> 'addenda' FROM nodes WHERE id = %s", (canonical,))
    addenda = cur.fetchone()[0]
    assert len(addenda) == 1
    assert addenda[0]["promoted_to"] == str(member_id)
    assert addenda[0]["body"] == novel  # body retained


def test_non_novel_addendum_is_left_untouched(space, conn):
    canonical = _seed_canonical(
        conn, space, "The canonical fact about the topic.",
        [_addendum("The canonical fact about the topic.")],  # identical -> non-novel
    )
    summary = promote_addenda(conn, space, embed_document=_embed)
    assert summary.promoted == 0
    assert summary.skipped_non_novel == 1

    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s", (space,))
    assert cur.fetchone()[0] == 1, "no member row"
    cur.execute("SELECT attrs -> 'addenda' -> 0 -> 'promoted_to' FROM nodes WHERE id = %s", (canonical,))
    assert cur.fetchone()[0] is None, "the addendum is not marked"


def test_rerun_is_a_no_op(space, conn):
    novel = "A distinct new fact about an entirely separate matter worth keeping."
    _seed_canonical(conn, space, "The canonical fact about the topic.", [_addendum(novel)])

    first = promote_addenda(conn, space, embed_document=_embed)
    assert first.promoted == 1
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s", (space,))
    after_first = cur.fetchone()[0]

    second = promote_addenda(conn, space, embed_document=_embed)
    assert second.promoted == 0
    assert second.skipped_already_marked == 1
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s", (space,))
    assert cur.fetchone()[0] == after_first, "re-run created zero new rows"


def test_rerun_completes_only_the_unmarked_remainder(space, conn):
    """The idempotency marker IS the crash-recovery guarantee: an addendum already
    carrying promoted_to (as if a prior run had committed it) is skipped, and only
    the unmarked remainder is promoted."""
    already = _addendum("Previously promoted distinct fact number one here today.")
    already["promoted_to"] = "00000000-0000-0000-0000-000000000001"
    remainder = _addendum("A second distinct fact never promoted before now today.")
    _seed_canonical(conn, space, "The canonical fact about the topic.", [already, remainder])

    summary = promote_addenda(conn, space, embed_document=_embed)
    assert summary.promoted == 1
    assert summary.skipped_already_marked == 1

    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s", (space,))
    assert cur.fetchone()[0] == 2, "only the remainder became a member"


def test_dry_run_writes_nothing(space, conn):
    novel = "A distinct new fact about an entirely separate matter worth keeping."
    _seed_canonical(conn, space, "The canonical fact about the topic.", [_addendum(novel)])

    summary = promote_addenda(conn, space, dry_run=True, embed_document=_embed)
    assert summary.dry_run is True
    assert summary.promoted == 1  # would-promote count

    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s", (space,))
    assert cur.fetchone()[0] == 1, "dry-run wrote no member"
    cur.execute("SELECT count(*) FROM dedup_log WHERE space_id = %s", (space,))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT attrs -> 'addenda' -> 0 -> 'promoted_to' FROM nodes WHERE space_id = %s", (space,))
    assert cur.fetchone()[0] is None


def test_created_at_equals_merged_at(space, conn):
    canonical = _seed_canonical(
        conn, space, "The canonical fact about the topic.",
        [_addendum("A distinct fact worth keeping around here.",
                   merged_at="2025-11-03T08:30:00+00:00")],
    )
    promote_addenda(conn, space, embed_document=_embed)
    cur = conn.cursor()
    cur.execute("SELECT created_at FROM nodes WHERE space_id = %s AND id <> %s", (space, canonical))
    assert cur.fetchone()[0].isoformat() == "2025-11-03T08:30:00+00:00"


def test_pack_without_rule_promotes_but_skips_edge(conn, request):
    space_id = ("ap-norule-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap(conn, space_id, with_same_topic_rule=False)
    try:
        novel = "A distinct new fact about an entirely separate matter worth keeping."
        canonical = _seed_canonical(conn, space_id, "The canonical fact about the topic.",
                                    [_addendum(novel)])
        summary = promote_addenda(conn, space_id, embed_document=_embed)
        assert summary.promoted == 1
        assert summary.edges_skipped_missing_rule == 1

        cur = conn.cursor()
        # the member row still exists (I1 holds)...
        cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s AND id <> %s",
                    (space_id, canonical))
        assert cur.fetchone()[0] == 1
        # ...but no edge, and the dedup_log record survives.
        cur.execute("SELECT count(*) FROM edges WHERE space_id = %s", (space_id,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM dedup_log WHERE space_id = %s "
                    "AND band = 'merge_linked_promoted'", (space_id,))
        assert cur.fetchone()[0] == 1
    finally:
        _cleanup(conn, space_id)
