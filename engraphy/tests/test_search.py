"""engraphy.core.search.rrf_fuse — golden fixtures in fixtures/rrf_cases.yaml,
byte-exact per design/07 §Exact formulas (RRF).
"""
import pathlib

import pytest
import yaml
from psycopg.types.json import Jsonb

from engraphy.core.search import rrf_fuse

FIXTURES = yaml.safe_load(
    (pathlib.Path(__file__).parent / "fixtures" / "rrf_cases.yaml").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", FIXTURES, ids=[c["name"] for c in FIXTURES])
def test_rrf_fixture_case(case):
    result = rrf_fuse(
        case["vector_ranked"],
        case["lexical_ranked"],
        created_at=case.get("created_at"),
        limit=case.get("limit"),
    )

    if "expect_order" in case:
        assert [node_id for node_id, _ in result] == case["expect_order"]
        assert len(result) == case["expect_count"]
        scoremap = dict(result)
        for node_id, expected_score in case.get("spot_check_scores", {}).items():
            assert scoremap[node_id] == pytest.approx(expected_score, abs=1e-6)
    else:
        expected = [(e["id"], e["score"]) for e in case["expect"]]
        assert result == expected


# ---- hybrid search integration (design/02 §Search acceptance) ---------------
# These exercise the DB legs + fusion against the REAL pinned model: seeds are
# embedded with embed_document, queries with embed_query (the asymmetric
# convention). rrf_fuse's pure arithmetic is covered by rrf_cases.yaml above.

from engraphy.core import embedding as _emb  # noqa: E402
from engraphy.core.search import search  # noqa: E402


def _bootstrap_search_space(conn, space_id):
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'S')", (space_id,))
    cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p1', 'P')", (space_id,))
    cur.execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES "
        "(%s, 'note', 'n', %s), (%s, 'error', 'e', %s)",
        (space_id, Jsonb({"attrs": {"closed": False}}), space_id, Jsonb({"attrs": {"closed": False}})),
    )
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, 'scope1', 'S1', 'p1', 'private'), (%s, 'other', 'Oth', 'p1', 'private')",
        (space_id, space_id),
    )
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility, ambient) "
        "VALUES (%s, 'personal', 'Pers', 'p1', 'private', true)",
        (space_id,),
    )
    conn.commit()


def _cleanup_search_space(conn, space_id):
    cur = conn.cursor()
    for t in ("audit_log", "dedup_log", "edges", "nodes", "scopes", "node_types", "principals"):
        cur.execute(f"DELETE FROM {t} WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


@pytest.fixture
def search_space(conn, request):
    space_id = ("se-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap_search_space(conn, space_id)
    yield space_id
    _cleanup_search_space(conn, space_id)


def _seed(conn, space_id, node_type, scope, title, body):
    vec = _emb.embed_document(title + "\n" + body)
    cur = conn.cursor()
    lit = "[" + ",".join(str(x) for x in vec) + "]"
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
        "embedding_model, source_client, author_principal) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s, 'pytest', 'p1') RETURNING id",
        (space_id, node_type, scope, title, body, Jsonb({}), lit, _emb.MODEL_ID),
    )
    (nid,) = cur.fetchone()
    conn.commit()
    return str(nid)


def _ids(result):
    return [r["node"]["id"] for r in result["results"]]


async def test_search_envelope_shape(pool, search_space, conn):
    nid = _seed(conn, search_space, "note", "scope1", "Coffee machine descaling",
                "Descale the office coffee machine monthly or it fails.")
    result = await search(pool, search_space, "p1", "scope1", "how to descale the coffee machine", "pytest")

    assert result["v"] == 1
    assert result["detail"] == "full"
    assert result["truncated"] is False
    assert result["scopes_searched"] == ["personal", "scope1"]  # scope1 + readable ambient, sorted
    assert len(result["results"]) == 1
    item = result["results"][0]
    assert item["node"]["id"] == nid
    assert "body" in item["node"]              # full detail
    assert isinstance(item["score"], float)
    assert item["similarity"] == pytest.approx(item["similarity"], abs=0)  # present (vector-leg hit)
    assert item["edge_count"] == 0


async def test_search_merged_node_never_carries_attrs_addenda(pool, search_space, conn):
    """Q1: addenda never ships in `attrs` on the wire, from any tool -- merge
    history is surfaced exactly once, by `get`, as a top-level field. A node
    that has been merged into (attrs.addenda set directly, mirroring what
    dedup._do_merge writes) must not leak it through search's envelope."""
    nid = _seed(conn, search_space, "note", "scope1", "Coffee machine descaling",
                "Descale the office coffee machine monthly or it fails.")
    cur = conn.cursor()
    cur.execute(
        "UPDATE nodes SET attrs = %s WHERE id = %s",
        (Jsonb({"addenda": [{"body": "a reworded duplicate", "author_principal": "p1"}]}), nid),
    )
    conn.commit()

    result = await search(pool, search_space, "p1", "scope1", "how to descale the coffee machine", "pytest")

    assert len(result["results"]) == 1
    assert "addenda" not in result["results"][0]["node"]["attrs"]


async def test_paraphrase_found_by_vector_leg(pool, search_space, conn):
    """02 acceptance: paraphrase found by the vector leg alone -- a query that
    shares no content words with the node still retrieves it semantically."""
    nid = _seed(conn, search_space, "note", "scope1", "Deploy failed: migration not run",
                "The release broke because the backfill database change was never executed before switching.")
    # no shared content words with the node (no "deploy/migration/backfill/database/release/switch")
    result = await search(pool, search_space, "p1", "scope1", "the rollout collapsed since a schema update wasn't applied first", "pytest")

    ids = _ids(result)
    assert nid in ids, "semantic paraphrase must be retrieved"
    hit = next(r for r in result["results"] if r["node"]["id"] == nid)
    assert "similarity" in hit, "retrieved via the vector leg (similarity present)"


async def test_decoy_ranked_below_true_match_after_fusion(pool, search_space, conn):
    """02 acceptance: a shared-word decoy ranks below the true match after
    fusion -- the semantic leg keeps the real answer on top even when the decoy
    shares surface words with the query."""
    true_id = _seed(conn, search_space, "note", "scope1", "Descaling the office coffee machine",
                    "The office coffee machine must be descaled monthly or the heating element fails and the taste degrades.")
    decoy_id = _seed(conn, search_space, "note", "scope1", "Machine learning reading group",
                     "Our weekly machine learning group meets over coffee to discuss deep learning papers and models.")
    # fillers: semantically nearer the query than the decoy, no query-word overlap,
    # so the decoy sinks in the vector leg and loses after fusion.
    _seed(conn, search_space, "note", "scope1", "Mineral buildup ruins appliances",
          "Hard tap water leaves deposits that shorten the life of heating elements in kitchen devices.")
    _seed(conn, search_space, "note", "scope1", "Kettle maintenance tips",
          "Empty and rinse the electric kettle regularly to keep the break room tidy and working.")

    result = await search(pool, search_space, "p1", "scope1", "how do I descale the coffee machine", "pytest")
    ids = _ids(result)
    assert true_id in ids and decoy_id in ids
    assert ids.index(true_id) < ids.index(decoy_id), "true match must outrank the shared-word decoy"


async def test_space_isolation_no_cross_space_hit(pool, search_space, conn):
    """Identical text in another space is never returned."""
    other = "se-other-space-xyz"
    _cleanup_search_space(conn, other)
    _bootstrap_search_space(conn, other)
    try:
        mine = _seed(conn, search_space, "note", "scope1", "Shared title text",
                     "Exactly the same body text in both spaces.")
        _seed(conn, other, "note", "scope1", "Shared title text",
              "Exactly the same body text in both spaces.")

        result = await search(pool, search_space, "p1", "all", "shared title body text", "pytest")
        ids = _ids(result)
        assert ids == [mine], "only this space's node, never the other space's"
    finally:
        _cleanup_search_space(conn, other)


async def test_scope_set_specific_includes_ambient_excludes_others(pool, search_space, conn):
    in_scope1 = _seed(conn, search_space, "note", "scope1", "Note in scope one", "Content about widgets and gears.")
    in_ambient = _seed(conn, search_space, "note", "personal", "Note in personal", "Content about widgets and gears.")
    _seed(conn, search_space, "note", "other", "Note in other", "Content about widgets and gears.")

    result = await search(pool, search_space, "p1", "scope1", "widgets and gears content", "pytest")
    ids = set(_ids(result))
    assert in_scope1 in ids
    assert in_ambient in ids, "readable ambient scope is folded in"
    assert result["scopes_searched"] == ["personal", "scope1"]
    assert all(r["node"]["scope"] in ("scope1", "personal") for r in result["results"]), "the 'other' scope is excluded"


async def test_scope_all_searches_every_readable_scope_and_audits(pool, search_space, conn):
    a = _seed(conn, search_space, "note", "scope1", "Alpha widget note", "Content about widgets and gears.")
    b = _seed(conn, search_space, "note", "other", "Beta widget note", "Content about widgets and gears.")

    result = await search(pool, search_space, "p1", "all", "widgets and gears content", "pytest")
    assert {a, b} <= set(_ids(result))
    assert set(result["scopes_searched"]) == {"scope1", "other", "personal"}

    # scope='all' read is audited (03 exfiltration tripwire).
    cur = conn.cursor()
    cur.execute("SELECT action, detail FROM audit_log WHERE space_id = %s", (search_space,))
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "search"
    assert rows[0][1]["scope"] == "all"


async def test_type_filter(pool, search_space, conn):
    note_id = _seed(conn, search_space, "note", "scope1", "Widget note", "Content about widgets and gears.")
    _seed(conn, search_space, "error", "scope1", "Widget error", "Content about widgets and gears.")

    result = await search(pool, search_space, "p1", "scope1", "widgets and gears", "pytest", types=["note"])
    assert _ids(result) == [note_id]


async def test_include_inactive(pool, search_space, conn):
    active = _seed(conn, search_space, "note", "scope1", "Active widget note", "Content about widgets and gears.")
    archived = _seed(conn, search_space, "note", "scope1", "Archived widget note", "Content about widgets and gears.")
    cur = conn.cursor()
    cur.execute("UPDATE nodes SET status = 'archived' WHERE id = %s", (archived,))
    conn.commit()

    default = await search(pool, search_space, "p1", "scope1", "widgets and gears", "pytest")
    assert _ids(default) == [active]

    including = await search(pool, search_space, "p1", "scope1", "widgets and gears", "pytest", include_inactive=True)
    assert set(_ids(including)) == {active, archived}


async def test_summary_detail_omits_body(pool, search_space, conn):
    _seed(conn, search_space, "note", "scope1", "Coffee machine descaling", "Descale the coffee machine monthly.")
    result = await search(pool, search_space, "p1", "scope1", "descale coffee machine", "pytest", detail="summary")
    assert result["detail"] == "summary"
    assert "body" not in result["results"][0]["node"]
    assert "title" in result["results"][0]["node"]


async def test_search_bumps_recall_stats_without_touching_updated_at(pool, search_space, conn):
    """Recall stats batched on every read (02): a search bumps recall_count and
    last_recalled_at for the surfaced node, while migration 0012 keeps updated_at
    stable -- proving the read is recorded without pretending the node was edited."""
    nid = _seed(conn, search_space, "note", "scope1", "Coffee machine descaling",
                "Descale the office coffee machine monthly.")
    cur = conn.cursor()
    cur.execute("SELECT recall_count, last_recalled_at, updated_at FROM nodes WHERE id = %s", (nid,))
    rc0, lr0, ua0 = cur.fetchone()
    assert rc0 == 0 and lr0 is None
    conn.commit()

    result = await search(pool, search_space, "p1", "scope1", "descale the coffee machine", "pytest")
    assert nid in _ids(result)

    cur.execute("SELECT recall_count, last_recalled_at, updated_at FROM nodes WHERE id = %s", (nid,))
    rc1, lr1, ua1 = cur.fetchone()
    assert rc1 == 1, "a read bumped recall_count"
    assert lr1 is not None, "last_recalled_at set on read"
    assert ua1 == ua0, "updated_at untouched by the read"


async def test_search_recall_bump_only_surfaced_nodes(pool, search_space, conn):
    """Only the SURFACED results are recalled -- a node scanned but truncated
    below `limit` is not counted as recalled."""
    kept = _seed(conn, search_space, "note", "scope1", "Widget alpha", "Content about widgets and gears alpha.")
    dropped = _seed(conn, search_space, "note", "scope1", "Widget beta", "Content about widgets and gears beta.")
    conn.commit()

    result = await search(pool, search_space, "p1", "scope1", "widgets and gears", "pytest", limit=1)
    assert _ids(result) == [kept] or _ids(result) == [dropped]  # exactly one surfaced
    surfaced = _ids(result)[0]
    other = dropped if surfaced == kept else kept

    cur = conn.cursor()
    cur.execute("SELECT recall_count FROM nodes WHERE id = %s", (surfaced,))
    assert cur.fetchone()[0] == 1, "surfaced node recalled"
    cur.execute("SELECT recall_count FROM nodes WHERE id = %s", (other,))
    assert cur.fetchone()[0] == 0, "truncated node not recalled"


# ---- hybrid_fuse relevance floor (briefing's semantic floor mechanism) -------
# Synthetic unit vectors give controlled query<->doc cosines. The floor filters
# the VECTOR leg only (>= survives); lexical hits are never floor-dropped; and
# search() never passes a floor (parity guard below).

import math  # noqa: E402

from engraphy.core.search import hybrid_fuse  # noqa: E402
from engraphy.server.db import transaction  # noqa: E402


def _unit_vector_at_angle(theta):
    v = [0.0] * 384
    v[0] = math.cos(theta)
    v[1] = math.sin(theta)
    return v


def _seed_vec(conn, space_id, scope, title, body, vec):
    cur = conn.cursor()
    lit = "[" + ",".join(str(x) for x in vec) + "]"
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
        "embedding_model, source_client, author_principal) "
        "VALUES (%s, 'note', %s, %s, %s, %s, %s::vector, 'test-model', 'pytest', 'p1') RETURNING id",
        (space_id, scope, title, body, Jsonb({}), lit),
    )
    (nid,) = cur.fetchone()
    conn.commit()
    return str(nid)


async def _fuse(pool, space_id, query_vec, query_text, floor):
    async with transaction(pool, space_id, "p1") as c:
        top, _sim, _nm, _tr = await hybrid_fuse(
            c.cursor(), space_id, query_vec, query_text, {"scope1"}, None, False, 10,
            vector_floor=floor,
        )
    return [nid for nid, _ in top]


async def test_hybrid_fuse_floor_drops_below_floor_without_lexical_match(pool, search_space, conn):
    q = _unit_vector_at_angle(0)
    nid = _seed_vec(conn, search_space, "scope1", "Alpha node",
                    "beta gamma unrelated tokens", _unit_vector_at_angle(math.acos(0.40)))
    assert nid not in await _fuse(pool, search_space, q, "zzzzquerynomatch", 0.50)


async def test_hybrid_fuse_floor_kept_when_lexical_matches(pool, search_space, conn):
    # Below-floor by cosine, but the query word is in the body -> the lexical leg
    # keeps it (lexical hits are never floor-dropped).
    q = _unit_vector_at_angle(0)
    nid = _seed_vec(conn, search_space, "scope1", "Alpha node",
                    "distinctivetoken present here", _unit_vector_at_angle(math.acos(0.40)))
    assert nid in await _fuse(pool, search_space, q, "distinctivetoken", 0.50)


async def test_hybrid_fuse_floor_boundary_is_inclusive(pool, search_space, conn):
    # >= survives: floor == the node's own measured similarity keeps it; a hair
    # above drops it. No lexical match, so the vector leg is the only path.
    q = _unit_vector_at_angle(0)
    nid = _seed_vec(conn, search_space, "scope1", "Alpha node",
                    "no query words in this body", _unit_vector_at_angle(math.acos(0.60)))
    async with transaction(pool, search_space, "p1") as c:
        cur = c.cursor()
        await cur.execute(
            "SELECT 1 - (embedding <=> %s::vector) FROM nodes WHERE id = %s",
            ("[" + ",".join(str(x) for x in q) + "]", nid),
        )
        sim = float((await cur.fetchone())[0])
    assert nid in await _fuse(pool, search_space, q, "zzzzquerynomatch", sim)          # == floor survives
    assert nid not in await _fuse(pool, search_space, q, "zzzzquerynomatch", sim + 1e-6)  # above -> dropped


async def test_search_returns_sub_floor_node_no_floor_leak(pool, search_space, conn):
    """Parity guard: search() applies NO floor, so a node whose query<->doc
    cosine is below the briefing floor (0.50) is still returned -- proving the
    floor did not leak into search."""
    off = _seed(conn, search_space, "note", "scope1", "Aisle seat preference",
                "Prefers an aisle seat on short domestic flights.")
    result = await search(pool, search_space, "p1", "scope1",
                          "what do we know about descaling the office coffee machine", "pytest")
    hit = next((r for r in result["results"] if r["node"]["id"] == off), None)
    assert hit is not None, "search must not floor -- the sub-floor node is still returned"
    assert hit["similarity"] < 0.50, "confirms it is genuinely sub-floor (~0.43 baselined)"
