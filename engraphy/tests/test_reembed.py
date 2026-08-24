"""`engraphy-admin reembed`: the one-time backfill onto a new vector space.

Driven with an injected embedder rather than a real model, for the same reason
test_surface_rebuild.py does: the mechanics under test are selection, resumability
and stamping, none of which need real vectors, and a real model would make the
suite slow for no added coverage.
"""
import pytest
from psycopg.types.json import Jsonb

from engraphy.admin.reembed import reembed_space
from engraphy.core import embedding as _emb
from engraphy.core.sentinel import (
    SENTINEL_EMBEDDING_MODEL,
    SENTINEL_NODE_TYPE,
    vector_literal as sentinel_vector_literal,
)

TARGET = "nomic-ai/nomic-embed-text-v1.5+onnx-int8"
LEGACY = "nomic-ai/nomic-embed-text-v1.5"


def _fake_embed(calls):
    def embed(text):
        calls.append(text)
        # deterministic unit vector; content-dependent so a swap is detectable
        seed = (sum(ord(c) for c in text) % 97) + 1
        vec = [0.0] * _emb.DIMS
        vec[seed % _emb.DIMS] = 1.0
        return vec
    return embed


def _bootstrap(conn, space_id):
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'S')", (space_id,))
    cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p1', 'P')",
                (space_id,))
    cur.execute("INSERT INTO node_types (space_id, name, description, attr_spec) VALUES "
                "(%s, 'note', 'n', %s), (%s, %s, 's', %s)",
                (space_id, Jsonb({"attrs": {}}), space_id, SENTINEL_NODE_TYPE,
                 Jsonb({"attrs": {}})))
    for sid in ("scope1", "scope2"):
        cur.execute("INSERT INTO scopes (space_id, id, display_name, owner_principal, "
                    "visibility) VALUES (%s, %s, 'S', 'p1', 'private')", (space_id, sid))
    # The sentinel, with its contract-constant vector and marker stamp.
    cur.execute(
        "INSERT INTO nodes (space_id, scope_id, type, title, body, attrs, embedding, "
        "embedding_model, source_client, author_principal, status) "
        "VALUES (%s,'scope1',%s,'sentinel','s',%s,%s::vector,%s,'pytest','p1','archived')",
        (space_id, SENTINEL_NODE_TYPE, Jsonb({}), sentinel_vector_literal(),
         SENTINEL_EMBEDDING_MODEL))
    conn.commit()


def _cleanup(conn, space_id):
    cur = conn.cursor()
    for t in ("audit_log", "dedup_log", "nodes", "scopes", "config", "node_types",
              "principals"):
        cur.execute(f"DELETE FROM {t} WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


def _seed(conn, space_id, scope_id, n, stamp):
    cur = conn.cursor()
    vec = "[" + ",".join(["0"] * (_emb.DIMS - 1) + ["1"]) + "]"
    for i in range(n):
        cur.execute(
            "INSERT INTO nodes (space_id, scope_id, type, title, body, attrs, "
            "embedding, embedding_model, source_client, author_principal) "
            "VALUES (%s,%s,'note',%s,%s,%s,%s::vector,%s,'pytest','p1')",
            (space_id, scope_id, f"title {i}", f"body {i}", Jsonb({}), vec, stamp))
    conn.commit()


@pytest.fixture
def seeded(conn):
    """A space with legacy-stamped rows across two scopes, plus the sentinel."""
    space_id = "reembed-t1"
    _cleanup(conn, space_id)
    _bootstrap(conn, space_id)
    _seed(conn, space_id, "scope1", 3, LEGACY)
    _seed(conn, space_id, "scope2", 2, LEGACY)
    yield conn, space_id, ("scope1", "scope2")
    _cleanup(conn, space_id)


def test_reembeds_every_row_not_already_in_the_target_space(seeded):
    conn, space_id, _ = seeded
    calls = []
    summary = reembed_space(conn, space_id, embed_document=_fake_embed(calls),
                            target_stamp=TARGET)
    assert summary.re_embedded == 5
    assert summary.already_current == 0
    assert len(calls) == 5

    cur = conn.cursor()
    cur.execute("SELECT DISTINCT embedding_model FROM nodes "
                "WHERE space_id = %s AND type <> %s", (space_id, SENTINEL_NODE_TYPE))
    assert [r[0] for r in cur.fetchall()] == [TARGET]


def test_a_second_run_finds_nothing(seeded):
    """Idempotent: this is what makes the backfill safe to re-run after a crash,
    and what makes a torch-to-fp32 move a no-op rather than a rewrite."""
    conn, space_id, _ = seeded
    calls = []
    reembed_space(conn, space_id, embed_document=_fake_embed(calls), target_stamp=TARGET)
    calls.clear()
    second = reembed_space(conn, space_id, embed_document=_fake_embed(calls),
                           target_stamp=TARGET)
    assert second.re_embedded == 0
    assert second.already_current == 5
    assert calls == []


def test_a_partial_run_resumes_exactly_the_remainder(seeded):
    """Selection is on the stamp, and the stamp is written with the vector, so a
    row is either converted or untouched. Simulated by converting a subset first."""
    conn, space_id, scopes = seeded
    calls = []
    reembed_space(conn, space_id, scope_id=scopes[0],
                  embed_document=_fake_embed(calls), target_stamp=TARGET)
    assert len(calls) == 3

    calls.clear()
    rest = reembed_space(conn, space_id, embed_document=_fake_embed(calls),
                         target_stamp=TARGET)
    assert rest.re_embedded == 2      # only the untouched scope
    assert rest.already_current == 3
    assert len(calls) == 2


def test_scope_filter_limits_the_work(seeded):
    conn, space_id, scopes = seeded
    calls = []
    summary = reembed_space(conn, space_id, scope_id=scopes[1],
                            embed_document=_fake_embed(calls), target_stamp=TARGET)
    assert summary.re_embedded == 2
    assert set(summary.per_scope) == {scopes[1]}


def test_dry_run_writes_nothing(seeded):
    conn, space_id, _ = seeded
    calls = []
    summary = reembed_space(conn, space_id, dry_run=True,
                            embed_document=_fake_embed(calls), target_stamp=TARGET)
    assert summary.re_embedded == 5
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT embedding_model FROM nodes "
                "WHERE space_id = %s AND type <> %s", (space_id, SENTINEL_NODE_TYPE))
    assert [r[0] for r in cur.fetchall()] == [LEGACY], "dry-run must not write"


def test_the_sentinel_is_never_re_embedded(seeded):
    """Its embedding is a constant by contract (design/04) and verify-restore
    compares exactly that vector. Sweeping it into a re-embed would break restore
    verification on every store that ran this command."""
    conn, space_id, _ = seeded
    cur = conn.cursor()
    cur.execute("SELECT embedding_model, embedding FROM nodes "
                "WHERE space_id = %s AND type = %s", (space_id, SENTINEL_NODE_TYPE))
    before = cur.fetchall()

    reembed_space(conn, space_id, embed_document=_fake_embed([]), target_stamp=TARGET)

    cur.execute("SELECT embedding_model, embedding FROM nodes "
                "WHERE space_id = %s AND type = %s", (space_id, SENTINEL_NODE_TYPE))
    assert cur.fetchall() == before
    for stamp, _vec in before:
        assert stamp == SENTINEL_EMBEDDING_MODEL


def test_updated_at_is_preserved(seeded):
    """Migration 0020 classifies an embedding-only change as a re-index, not a
    content edit. A backfill that bumped updated_at would make every node in the
    store look freshly edited."""
    conn, space_id, _ = seeded
    cur = conn.cursor()
    cur.execute("SELECT id, updated_at FROM nodes WHERE space_id = %s AND type <> %s "
                "ORDER BY id", (space_id, SENTINEL_NODE_TYPE))
    before = cur.fetchall()

    reembed_space(conn, space_id, embed_document=_fake_embed([]), target_stamp=TARGET)

    cur.execute("SELECT id, updated_at FROM nodes WHERE space_id = %s AND type <> %s "
                "ORDER BY id", (space_id, SENTINEL_NODE_TYPE))
    assert cur.fetchall() == before


def test_fp32_profiles_share_a_stamp_so_the_backfill_is_a_no_op(seeded):
    """The torch-to-ONNX-fp32 move must not rewrite a single row. Asserted through
    the real stamp function rather than a literal, so it stays true if the stamp
    scheme changes."""
    conn, space_id, _ = seeded
    calls = []
    summary = reembed_space(conn, space_id, embed_document=_fake_embed(calls),
                            target_stamp=_emb.model_stamp("onnx-fp32"))
    assert summary.re_embedded == 0
    assert summary.already_current == 5
    assert calls == []
