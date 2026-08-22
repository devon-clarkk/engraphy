"""engraphy.admin.surface.rebuild_surface -- Phase C §4 (recompute extra_search +
re-embed existing rows). Sync superuser-style conn like the other admin tests;
embeddings injected (deterministic) so no model load.
"""

import pytest
from psycopg.types.json import Jsonb

from engraphy.admin.surface import rebuild_surface
from engraphy.core import embedding as _emb
from engraphy.core.sentinel import (
    SENTINEL_EMBEDDING_MODEL,
    SENTINEL_NODE_TYPE,
    vector_literal as sentinel_vector_literal,
)

_VEC = [1.0] + [0.0] * 383
_VEC_LIT = "[" + ",".join(str(x) for x in _VEC) + "]"


def _embed(_text):
    return list(_VEC)


def _bootstrap(conn, space_id, flag=None):
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'S')", (space_id,))
    cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p1', 'P')", (space_id,))
    spec = {"attrs": {"optional": {"occupation": {"type": "string"},
                                   "strength": {"enum": ["hard", "soft"]}}, "closed": True}}
    cur.execute("INSERT INTO node_types (space_id, name, description, attr_spec) VALUES "
                "(%s, 'profile', 'p', %s), (%s, %s, 's', %s)",
                (space_id, Jsonb(spec), space_id, SENTINEL_NODE_TYPE, Jsonb({"attrs": {}})))
    cur.execute("INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
                "VALUES (%s, 'scope1', 'S', 'p1', 'private')", (space_id,))
    if flag is not None:
        cur.execute("INSERT INTO config (space_id, key, value) VALUES (%s, 'write.attr_surface', %s)",
                    (space_id, Jsonb(flag)))
    conn.commit()


def _cleanup(conn, space_id):
    cur = conn.cursor()
    for t in ("audit_log", "dedup_log", "nodes", "scopes", "config", "node_types", "principals"):
        cur.execute(f"DELETE FROM {t} WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


def _seed(conn, space_id, title, body, attrs, extra_search="", node_type="profile",
          model="test-model", status="active", embedding_lit=None):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
        "embedding_model, source_client, author_principal, extra_search, status) "
        "VALUES (%s, %s, 'scope1', %s, %s, %s, %s::vector, %s, 'pytest', 'p1', %s, %s) RETURNING id",
        (space_id, node_type, title, body, Jsonb(attrs), embedding_lit or _VEC_LIT,
         model, extra_search, status),
    )
    nid = cur.fetchone()[0]
    conn.commit()
    return str(nid)


@pytest.fixture
def space(conn, request):
    space_id = ("sr-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap(conn, space_id)
    yield space_id
    _cleanup(conn, space_id)


def test_stranded_attr_row_is_reembedded_with_extra_search(space, conn):
    """A pre-C row (extra_search='') carrying a searchable attr the body does not
    restate gets its render recomputed and re-embedded."""
    nid = _seed(conn, space, "Lucy", "Lucy is a person.", {"occupation": "glassblower"})
    summary = rebuild_surface(conn, space, embed_document=_embed)
    assert summary.re_embedded == 1
    assert summary.skipped_equal == 0

    cur = conn.cursor()
    cur.execute("SELECT extra_search, embedding_model FROM nodes WHERE id = %s", (nid,))
    extra, model = cur.fetchone()
    assert extra == "occupation: glassblower"
    assert model == _emb.MODEL_ID  # re-embedded


def test_attr_less_and_already_correct_rows_are_skipped(space, conn):
    """Idempotency belt: a row whose render already matches extra_search (or is
    attr-less) is skipped -- no re-embed."""
    _seed(conn, space, "Bob", "Bob has no attrs.", {})  # extra='' == stored '' -> skip
    _seed(conn, space, "Ann", "Ann is here.", {"occupation": "nurse"},
          extra_search="occupation: nurse")  # already correct -> skip
    summary = rebuild_surface(conn, space, embed_document=_embed)
    assert summary.re_embedded == 0
    assert summary.skipped_equal == 2


def test_rerun_is_a_no_op(space, conn):
    _seed(conn, space, "Lucy", "Lucy is a person.", {"occupation": "glassblower"})
    first = rebuild_surface(conn, space, embed_document=_embed)
    assert first.re_embedded == 1
    second = rebuild_surface(conn, space, embed_document=_embed)
    assert second.re_embedded == 0 and second.skipped_equal == 1


def test_updated_at_is_preserved(space, conn):
    nid = _seed(conn, space, "Lucy", "Lucy is a person.", {"occupation": "glassblower"})
    cur = conn.cursor()
    # the value set by the seed INSERT (the nodes_touch trigger stamps it; a manual
    # UPDATE to updated_at would just be overwritten back by the trigger).
    cur.execute("SELECT updated_at FROM nodes WHERE id = %s", (nid,))
    before = cur.fetchone()[0]
    rebuild_surface(conn, space, embed_document=_embed)
    cur.execute("SELECT updated_at FROM nodes WHERE id = %s", (nid,))
    assert cur.fetchone()[0] == before, "a re-index must not bump updated_at (migration 0020)"


def test_dry_run_writes_nothing(space, conn):
    nid = _seed(conn, space, "Lucy", "Lucy is a person.", {"occupation": "glassblower"})
    summary = rebuild_surface(conn, space, dry_run=True, embed_document=_embed)
    assert summary.dry_run and summary.re_embedded == 1
    cur = conn.cursor()
    cur.execute("SELECT extra_search, embedding_model FROM nodes WHERE id = %s", (nid,))
    extra, model = cur.fetchone()
    assert extra == "" and model == "test-model", "dry-run wrote nothing"


def test_flag_off_renders_empty_surface(conn, request):
    space_id = ("sr-off-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap(conn, space_id, flag=False)  # write.attr_surface = false
    try:
        # seed a row that (wrongly) has a non-empty extra_search; a rebuild with the
        # flag off must reset it to '' (the operational-rollback path).
        nid = _seed(conn, space_id, "Lucy", "Lucy is a person.", {"occupation": "glassblower"},
                    extra_search="occupation: glassblower")
        summary = rebuild_surface(conn, space_id, embed_document=_embed)
        assert summary.re_embedded == 1
        cur = conn.cursor()
        cur.execute("SELECT extra_search FROM nodes WHERE id = %s", (nid,))
        assert cur.fetchone()[0] == "", "flag off -> extra_search reset to ''"
    finally:
        _cleanup(conn, space_id)


def test_inactive_rows_rebuilt_but_sentinel_excluded(space, conn):
    """All statuses are rebuilt (a superseded row too), EXCEPT the reserved
    engram_sentinel whose embedding is a constant by contract."""
    superseded = _seed(conn, space, "Old", "Old fact.", {"occupation": "nurse"}, status="superseded")
    sentinel = _seed(conn, space, "Sentinel", "sentinel body here", {},
                     node_type=SENTINEL_NODE_TYPE, model=SENTINEL_EMBEDDING_MODEL,
                     status="archived", embedding_lit=sentinel_vector_literal())
    summary = rebuild_surface(conn, space, embed_document=_embed)
    assert summary.re_embedded == 1  # only the superseded profile row
    cur = conn.cursor()
    cur.execute("SELECT extra_search, embedding_model FROM nodes WHERE id = %s", (superseded,))
    extra, model = cur.fetchone()
    assert extra == "occupation: nurse" and model == _emb.MODEL_ID
    # sentinel untouched: constant vector + marker model preserved.
    cur.execute("SELECT embedding_model FROM nodes WHERE id = %s", (sentinel,))
    assert cur.fetchone()[0] == SENTINEL_EMBEDDING_MODEL
