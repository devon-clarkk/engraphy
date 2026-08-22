"""engraphy.core.update — E2-plan.md s.4's full build spec: pure content
replacement, never dedup-banded; re-embeds iff title/body supplied and the
resulting text actually changed; attrs.addenda is reserved and preserved
across any attrs replacement (Q1).
"""
import pytest

from engraphy.core.dedup import NotFoundError, ValidationError
from engraphy.core.update import update
from engraphy.tests.test_dedup import _seed_node, _unit_vector_at_angle, write_space  # noqa: F401

_VEC_A = _unit_vector_at_angle(0)
_VEC_B = _unit_vector_at_angle(1.0)


def _stub_embed(text: str) -> list[float]:
    return _VEC_B


async def test_update_attrs_only_does_not_reembed(pool, write_space, conn):
    nid = _seed_node(conn, write_space, "widget", "Original title", "Original body.", {}, _VEC_A)

    result = await update(
        pool, write_space, "p1", str(nid), attrs={"color": "blue"}, embed_document=_stub_embed,
    )
    assert result["outcome"] == "updated"
    assert result["node"]["title"] == "Original title"
    assert result["node"]["attrs"] == {"color": "blue"}

    cur = conn.cursor()
    cur.execute("SELECT embedding_model FROM nodes WHERE id = %s", (nid,))
    assert cur.fetchone()[0] == "test-model"  # unchanged from _seed_node's own stamp


async def test_update_title_change_reembeds(pool, write_space, conn):
    nid = _seed_node(conn, write_space, "widget", "Original title", "Original body.", {}, _VEC_A)

    result = await update(
        pool, write_space, "p1", str(nid), title="A new title", embed_document=_stub_embed,
    )
    assert result["node"]["title"] == "A new title"
    assert result["node"]["body"] == "Original body."

    cur = conn.cursor()
    cur.execute("SELECT embedding_model FROM nodes WHERE id = %s", (nid,))
    from engraphy.core import embedding as _emb
    assert cur.fetchone()[0] == _emb.MODEL_ID  # re-embedded -> pinned model id stamped


async def test_update_byte_identical_repeat_skips_reembed_and_updated_at(pool, write_space, conn):
    nid = _seed_node(conn, write_space, "widget", "Original title", "Original body.", {}, _VEC_A)
    cur = conn.cursor()
    cur.execute("SELECT updated_at, embedding_model FROM nodes WHERE id = %s", (nid,))
    before_updated_at, before_model = cur.fetchone()

    result = await update(
        pool, write_space, "p1", str(nid),
        title="Original title", body="Original body.", embed_document=_stub_embed,
    )
    assert result["outcome"] == "updated"

    cur.execute("SELECT updated_at, embedding_model FROM nodes WHERE id = %s", (nid,))
    after_updated_at, after_model = cur.fetchone()
    assert after_updated_at == before_updated_at, "byte-identical repeat must not bump updated_at"
    assert after_model == before_model, "byte-identical repeat must not re-embed"


async def test_update_preserves_stored_addenda_across_attrs_replacement(pool, write_space, conn):
    nid = _seed_node(
        conn, write_space, "widget", "Original title", "Original body.",
        {"addenda": [{"body": "a prior merge addendum", "author_principal": "p1"}]}, _VEC_A,
    )

    result = await update(
        pool, write_space, "p1", str(nid), attrs={"color": "blue"}, embed_document=_stub_embed,
    )
    assert result["outcome"] == "updated"
    # Q1: wire envelope never carries addenda.
    assert "addenda" not in result["node"]["attrs"]
    assert result["node"]["attrs"]["color"] == "blue"

    cur = conn.cursor()
    cur.execute("SELECT attrs FROM nodes WHERE id = %s", (nid,))
    stored = cur.fetchone()[0]
    assert stored["color"] == "blue"
    assert stored["addenda"] == [{"body": "a prior merge addendum", "author_principal": "p1"}]


async def test_update_caller_supplied_attrs_addenda_raises_validation(pool, write_space, conn):
    nid = _seed_node(conn, write_space, "widget", "Original title", "Original body.", {}, _VEC_A)
    with pytest.raises(ValidationError, match="ENGRAPHY_VALIDATION"):
        await update(
            pool, write_space, "p1", str(nid),
            attrs={"addenda": [{"body": "spoofed"}]}, embed_document=_stub_embed,
        )


async def test_update_unknown_id_raises_not_found(pool, write_space):
    with pytest.raises(NotFoundError, match="ENGRAPHY_NOT_FOUND"):
        await update(
            pool, write_space, "p1", "00000000-0000-4000-8000-000000000000",
            title="Whatever", embed_document=_stub_embed,
        )


async def test_update_no_fields_supplied_is_a_no_op(pool, write_space, conn):
    nid = _seed_node(conn, write_space, "widget", "Original title", "Original body.", {}, _VEC_A)
    result = await update(pool, write_space, "p1", str(nid), embed_document=_stub_embed)
    assert result["node"]["title"] == "Original title"
    assert result["node"]["body"] == "Original body."


# ---- Phase C: the amend-path re-embed hole (§2.4) ---------------------------
# update() now re-embeds iff the SEARCHABLE TEXT changes (title + body + rendered
# searchable attrs), not just title/body. A closed:false 'widget' declares no
# typed attrs, so none is searchable -- these tests need a type with a DECLARED
# searchable string attr and a non-searchable enum attr.

from psycopg.types.json import Jsonb  # noqa: E402

from engraphy.core import embedding as _emb  # noqa: E402


def _bootstrap_profile_space(conn, space_id):
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'S')", (space_id,))
    cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p1', 'P')", (space_id,))
    spec = {"attrs": {"optional": {"occupation": {"type": "string"},
                                   "strength": {"enum": ["hard", "soft"]}}, "closed": True}}
    cur.execute("INSERT INTO node_types (space_id, name, description, attr_spec) VALUES (%s,'profile','p',%s)",
                (space_id, Jsonb(spec)))
    cur.execute("INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
                "VALUES (%s, 'scope1', 'S', 'p1', 'private')", (space_id,))
    conn.commit()


@pytest.fixture
def profile_space(conn, request):
    space_id = ("up-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap_profile_space(conn, space_id)
    yield space_id
    cur = conn.cursor()
    for t in ("audit_log", "dedup_log", "nodes", "scopes", "node_types", "principals"):
        cur.execute(f"DELETE FROM {t} WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


def _seed_profile(conn, space_id, occupation=None, strength=None):
    attrs = {}
    if occupation is not None:
        attrs["occupation"] = occupation
    if strength is not None:
        attrs["strength"] = strength
    extra = _emb.render_attr_surface(attrs, {"occupation"})  # only occupation searchable
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
        "embedding_model, source_client, author_principal, extra_search) "
        "VALUES (%s,'profile','scope1','Lucy','Lucy is a person.',%s,%s::vector,'test-model','pytest','p1',%s) "
        "RETURNING id",
        (space_id, Jsonb(attrs), "[" + ",".join(["0"] * 384) + "]", extra),
    )
    nid = cur.fetchone()[0]
    conn.commit()
    return str(nid)


async def test_update_searchable_attr_change_reembeds(profile_space, pool, conn):
    """The hole this closes: an attrs-only update that changes a SEARCHABLE attr
    (occupation) re-embeds, because the searchable text changed -- even though
    title/body did not."""
    nid = _seed_profile(conn, profile_space, occupation="nurse")
    result = await update(pool, profile_space, "p1", nid,
                          attrs={"occupation": "teacher"}, embed_document=_stub_embed)
    assert result["node"]["attrs"]["occupation"] == "teacher"
    cur = conn.cursor()
    cur.execute("SELECT embedding_model, extra_search FROM nodes WHERE id = %s", (nid,))
    model, extra = cur.fetchone()
    assert model == _emb.MODEL_ID, "a searchable-attr change must re-embed"
    assert extra == "occupation: teacher", "extra_search tracks the new searchable attr"


async def test_update_non_searchable_attr_change_skips_reembed(profile_space, pool, conn):
    """The counterpart: changing only a NON-searchable attr (strength, an enum)
    leaves the searchable text unchanged, so no model call happens."""
    nid = _seed_profile(conn, profile_space, occupation="nurse", strength="hard")
    result = await update(pool, profile_space, "p1", nid,
                          attrs={"occupation": "nurse", "strength": "soft"}, embed_document=_stub_embed)
    assert result["node"]["attrs"]["strength"] == "soft"
    cur = conn.cursor()
    cur.execute("SELECT embedding_model, extra_search FROM nodes WHERE id = %s", (nid,))
    model, extra = cur.fetchone()
    assert model == "test-model", "non-searchable-attr change must NOT re-embed"
    assert extra == "occupation: nurse", "extra_search unchanged (occupation kept)"
