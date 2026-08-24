"""Invariant I4 (dual-index consistency) as a regression wall -- the test whose
absence let the addenda leak ship (design/analysis/fact-searchability-model.md
§4; implementation fact-searchability-phase-b.md §6). "Can I find what I just
stored?"

Runs against the real DB + the pinned embedding model like the other E1
integration tests. Every write branch is exercised with NONCE-bearing bodies (a
unique `zq7xk-<n>` token per fact) so the lexical leg of `search` makes
findability deterministic and reader-independent. Bands are forced with synthetic
`thresholds` so the outcome is controlled while the embedding is a REAL model
vector (which is what assertion (a) checks).

The ontology is hand-bootstrapped to mirror a shipped pack's write-path surface:
closed:false `note`/`error` types plus `same_topic`/`relates_to`/`supersedes`
rules (the with-rule space) or those minus `same_topic` (the rule-less space,
standing in for a pack like the Example one that predates the edge).
That the shipped packs themselves DECLARE `same_topic` (and Example does not) is
asserted in test_pack_format.py::test_shipped_packs_declare_same_topic.
"""
import pytest
from psycopg.types.json import Jsonb

from engraphy.admin.addenda import promote_addenda
from engraphy.admin.surface import rebuild_surface
from engraphy.core import embedding as _emb
from engraphy.core.dedup import (
    ATTR_SURFACE_KEY,
    BandThresholds,
    embed_with_attr_surface,
    resolve_duplicate,
    supersede,
    write,
)
from engraphy.core.jaccard import is_novel
from engraphy.core.search import search
from engraphy.core.traverse import traverse
from engraphy.core.update import update

# Synthetic thresholds force the band regardless of the real similarity:
_INSERT = BandThresholds(t_high=1.1, t_low=1.05)   # sim < t_low -> insert
_MERGE = BandThresholds(t_high=0.0, t_low=0.0)     # sim >= t_high -> merge (needs a candidate)
_PENDING = BandThresholds(t_high=2.0, t_low=0.0)   # t_low <= sim < t_high -> pending


class _Nonce:
    def __init__(self):
        self.i = 0

    def next(self) -> str:
        self.i += 1
        return f"zq7xk-{self.i:03d}"


def _bootstrap(conn, space_id, with_rule=True):
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'S')", (space_id,))
    cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p1', 'P')",
                (space_id,))
    person_spec = {"attrs": {"optional": {
        "occupation": {"type": "string"}, "location": {"type": "string"},
        "strength": {"enum": ["hard", "soft"]}}, "closed": True}}
    cur.execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES "
        "(%s, 'note', 'n', %s), (%s, 'error', 'e', %s), (%s, 'person', 'p', %s)",
        (space_id, Jsonb({"attrs": {"closed": False}}),
         space_id, Jsonb({"attrs": {"closed": False}}),
         space_id, Jsonb(person_spec)),
    )
    edge_types = [("relates_to", True), ("supersedes", False)]
    if with_rule:
        edge_types.append(("same_topic", True))
    cur.executemany(
        "INSERT INTO edge_types (space_id, name, description, bidirectional) VALUES (%s, %s, 'd', %s)",
        [(space_id, name, bidir) for name, bidir in edge_types],
    )
    rules = []
    for etype in ("relates_to", "supersedes") + (("same_topic",) if with_rule else ()):
        for src in ("note", "error"):
            for dst in ("note", "error"):
                rules.append((space_id, etype, src, dst))
    cur.executemany(
        "INSERT INTO edge_rules (space_id, type, src_type, dst_type) VALUES (%s, %s, %s, %s)", rules,
    )
    cur.execute("INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
                "VALUES (%s, 'work', 'W', 'p1', 'private')", (space_id,))
    conn.commit()


def _cleanup(conn, space_id):
    cur = conn.cursor()
    for t in ("audit_log", "dedup_log", "edges", "nodes", "scopes", "edge_rules",
              "edge_types", "config", "node_types", "principals"):
        cur.execute(f"DELETE FROM {t} WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


@pytest.fixture
def rule_space(conn, request):
    space_id = ("di-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap(conn, space_id, with_rule=True)
    yield space_id
    _cleanup(conn, space_id)


@pytest.fixture
def norule_space(conn, request):
    space_id = ("dn-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap(conn, space_id, with_rule=False)
    yield space_id
    _cleanup(conn, space_id)


async def _w(pool, space, ntype, title, body, thresholds, import_mode=False):
    vec = _emb.embed_document(title + "\n" + body)
    return await write(pool, space, "p1", ntype, "work", title, body, {}, vec, "pytest",
                       thresholds=thresholds, import_mode=import_mode)


# --- assertions reused across spaces -----------------------------------------

def _assert_embedding_integrity(conn, space):
    """(a-extended, Phase C §6): every active node's stored embedding equals
    embed_document(searchable_text(title, body, extra_search)). For an attr-less
    node extra_search='' so this reduces to the Phase B title+body identity."""
    cur = conn.cursor()
    cur.execute("SELECT id, title, body, extra_search FROM nodes WHERE space_id = %s AND status = 'active'",
                (space,))
    rows = cur.fetchall()
    assert rows, "workload wrote at least one active node"
    for node_id, title, body, extra in rows:
        vec = _emb.embed_document(_emb.searchable_text(title, body, extra))
        lit = "[" + ",".join(str(x) for x in vec) + "]"
        cur.execute("SELECT 1 - (embedding <=> %s::vector) FROM nodes WHERE id = %s", (lit, node_id))
        cosine = cur.fetchone()[0]
        assert cosine >= 0.9999, f"node {node_id} embedding drifted from its text (cos={cosine})"


async def _assert_findable(pool, space, nonce, expected_id):
    """(d): the nonce token surfaces its carrying node (or canonical) in search."""
    res = await search(pool, space, "p1", "work", nonce, "pytest")
    ids = {r["node"]["id"] for r in res["results"]}
    assert expected_id in ids, f"nonce {nonce} did not surface node {expected_id}: got {ids}"


def _assert_no_fact_only_in_addendum(conn, space):
    """(b): every attrs.addenda entry is either non-novel w.r.t. its node's
    body + prior corpus, or carries promoted_to."""
    cur = conn.cursor()
    cur.execute("SELECT id, body, attrs FROM nodes WHERE space_id = %s "
                "AND jsonb_typeof(attrs -> 'addenda') = 'array'", (space,))
    for _node_id, body, attrs in cur.fetchall():
        corpus = [body]
        for a in attrs["addenda"]:
            a_body = a.get("body", "")
            if a.get("promoted_to") is not None:
                pass  # its content is a member row
            else:
                assert not is_novel(a_body, " ".join(corpus)), (
                    "a novel fact survives ONLY as a get-only addendum -- the leak is back"
                )
            corpus.append(a_body)


def _merge_linked_rows(conn, space):
    cur = conn.cursor()
    cur.execute("SELECT node_id, candidate_id FROM dedup_log WHERE space_id = %s "
                "AND band IN ('merge_linked', 'merge_linked_promoted')", (space,))
    return cur.fetchall()


# --- the comprehensive workload (with-rule space) ----------------------------

async def test_dual_index_invariant_full_workload(pool, rule_space, conn):
    space = rule_space
    n = _Nonce()
    findable: dict[str, str] = {}

    def body(text, nonce):
        return f"{text} ({nonce})"

    # 1. plain insert
    n1 = n.next()
    r = await _w(pool, space, "note", "Insert", body("Quarterly planning review notes", n1), _INSERT)
    assert r["outcome"] == "inserted"
    findable[n1] = r["node"]["id"]

    # an anchor candidate for the merge/pending branches
    na = n.next()
    ra = await _w(pool, space, "note", "Anchor",
                  body("Priya is a paediatric nurse in Leeds", na), _INSERT)
    anchor = ra["node"]["id"]
    findable[na] = anchor

    # 2. absorb (non-novel twin, forced merge) -- no distinct row, not required findable
    r = await _w(pool, space, "note", "Anchor",
                 body("Priya is a paediatric nurse in Leeds", na), _MERGE)
    assert r["outcome"] == "merged", "a non-novel twin absorbs"

    # 3. merge-link (novel twin, forced merge) -- REGRESSION PIN: this nonce is the
    #    exact query shape that failed silently before Phase B.
    n3 = n.next()
    r = await _w(pool, space, "note", "MergeLink",
                 body("Priya was promoted to charge nurse on the ward", n3), _MERGE)
    assert r["outcome"] == "merged_linked" and r["cluster_edge_added"] is True
    findable[n3] = r["node"]["id"]
    regression_pin = (n3, r["node"]["id"])

    # 4. pending -> distinct
    n4 = n.next()
    parked = await _w(pool, space, "note", "PendDistinct",
                      body("Priya volunteers at the community clinic", n4), _PENDING)
    assert parked["outcome"] == "needs_confirmation"
    r = await resolve_duplicate(pool, space, "p1", parked["pending_id"], "distinct")
    assert r["outcome"] == "inserted"
    findable[n4] = r["node"]["id"]

    # 5. pending -> merge (novel) -> merged_linked
    n5 = n.next()
    parked = await _w(pool, space, "note", "PendMergeNovel",
                      body("Priya earned a leadership award this spring", n5), _PENDING)
    r = await resolve_duplicate(pool, space, "p1", parked["pending_id"], "merge", merge_into=anchor)
    assert r["outcome"] == "merged_linked"
    findable[n5] = r["node"]["id"]

    # 6. pending -> merge (non-novel) -> merged (absorb, no distinct row). Uses a
    #    FRESH peer-free canonical: the anchor above has by now accumulated
    #    same_topic members, so a restatement of its body alone reads as novel
    #    against the enriched cluster corpus (correct, but not the case under test).
    nc = n.next()
    rc2 = await _w(pool, space, "note", "CleanAnchor",
                   body("The staff kitchen is on the third floor", nc), _INSERT)
    findable[nc] = rc2["node"]["id"]
    parked = await _w(pool, space, "note", "PendMergeDup",
                      body("The staff kitchen is on the third floor", nc), _PENDING)
    r = await resolve_duplicate(pool, space, "p1", parked["pending_id"], "merge",
                                merge_into=rc2["node"]["id"])
    assert r["outcome"] == "merged"

    # 7. supersede clean (old_id excluded -> inserted replacement)
    n7old = n.next()
    old = await _w(pool, space, "note", "SupersedeOld",
                   body("The office WiFi password is oldsecret", n7old), _INSERT)
    n7 = n.next()
    r = await supersede(pool, space, "p1", old["node"]["id"], "note", "work",
                        "SupersedeNew", body("The office WiFi password is newsecret", n7),
                        {}, _emb.embed_document("SupersedeNew\n" +
                                                body("The office WiFi password is newsecret", n7)),
                        "pytest", thresholds=_INSERT)
    assert r["outcome"] == "inserted" and r["superseded"] == old["node"]["id"]
    findable[n7] = r["node"]["id"]

    # 8. supersede into the merge band (novel vs a third node) -> merged_linked
    n8old = n.next()
    old8 = await _w(pool, space, "note", "SupOld2",
                    body("The deploy runbook lives in the wiki", n8old), _INSERT)
    n8 = n.next()
    r = await supersede(pool, space, "p1", old8["node"]["id"], "note", "work",
                        "SupNew2", body("The deploy runbook moved to the new handbook", n8),
                        {}, _emb.embed_document("SupNew2\n" +
                                                body("The deploy runbook moved to the new handbook", n8)),
                        "pytest", thresholds=_MERGE)
    assert r["outcome"] == "merged_linked" and r["superseded"] == old8["node"]["id"]
    findable[n8] = r["node"]["id"]

    # 9. update text change -> re-embed, findable by the new nonce
    n9 = n.next()
    base = await _w(pool, space, "note", "UpdateText",
                    body("Draft agenda for the offsite", n.next()), _INSERT)
    r = await update(pool, space, "p1", base["node"]["id"],
                     body=body("Final agenda for the offsite", n9))
    findable[n9] = base["node"]["id"]

    # 10. update attrs-only (body unchanged) -- embedding integrity must still hold
    await update(pool, space, "p1", base["node"]["id"], attrs={"tag": "offsite"})

    # 11. import-mode merge-link (silent) -> member kept, findable
    n11 = n.next()
    r = await _w(pool, space, "note", "ImportLink",
                 body("Priya mentors two junior nurses now", n11), _MERGE, import_mode=True)
    assert r == {"v": 1, "outcome": "merged_linked"}
    cur = conn.cursor()
    cur.execute("SELECT id FROM nodes WHERE space_id = %s AND body LIKE %s", (space, f"%{n11}%"))
    findable[n11] = str(cur.fetchone()[0])

    # 12. seed a legacy buried addendum, then run the promote migration in-test.
    legacy_nonce = n.next()
    legacy_addendum = {
        "merged_at": "2026-01-10T09:00:00+00:00", "source_client": "old",
        "source_session": None, "author_principal": "p1",
        "body": body("Priya trained as a midwife before nursing", legacy_nonce),
    }
    legacy_body = body("Priya is a paediatric nurse", n.next())
    legacy_vec = _emb.embed_document("Legacy\n" + legacy_body)
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
        "embedding_model, source_client, author_principal) "
        "VALUES (%s, 'note', 'work', 'Legacy', %s, %s, %s::vector, %s, 'pytest', 'p1') RETURNING id",
        (space, legacy_body, Jsonb({"addenda": [legacy_addendum]}),
         "[" + ",".join(str(x) for x in legacy_vec) + "]", _emb.MODEL_STAMP),
    )
    conn.commit()
    promote_addenda(conn, space, embed_document=_emb.embed_document)
    cur.execute("SELECT id FROM nodes WHERE space_id = %s AND body LIKE %s", (space, f"%{legacy_nonce}%"))
    row = cur.fetchone()
    assert row is not None, "the promote migration created the member row"
    findable[legacy_nonce] = str(row[0])

    # ---- ASSERTIONS ---------------------------------------------------------

    # (a) embedding integrity across every active node written above.
    _assert_embedding_integrity(conn, space)

    # (d) findability -- every fact the workload stored, including the merge-link
    #     member (the regression pin).
    for nonce, node_id in findable.items():
        await _assert_findable(pool, space, nonce, node_id)
    pin_nonce, pin_id = regression_pin
    await _assert_findable(pool, space, pin_nonce, pin_id)

    # (c) graph side: every merge_linked dedup_log row has a matching same_topic
    #     edge, and depth-1 traverse reaches the member from the canonical (and back).
    for node_id, candidate_id in _merge_linked_rows(conn, space):
        cur.execute("SELECT count(*) FROM edges WHERE space_id = %s AND type = 'same_topic' "
                    "AND src_id = %s AND dst_id = %s", (space, node_id, candidate_id))
        assert cur.fetchone()[0] == 1, f"merge_linked row {node_id} lacks its same_topic edge"
        walk = await traverse(pool, space, "p1", str(candidate_id), "both",
                              edge_types=["same_topic"], max_depth=1)
        reached = {item["id"] for item in walk["nodes"]}
        assert str(node_id) in reached, "traverse from canonical did not reach the member"

    # (b) no fact-only-in-addendum anywhere in the final state.
    _assert_no_fact_only_in_addendum(conn, space)


# --- the rule-less arm -------------------------------------------------------

async def test_dual_index_invariant_ruleless_pack(pool, norule_space, conn):
    """A pack without the same_topic rule: a novel merge still keeps the member
    row (I1) and logs merge_linked, but the edge is skipped (cluster_edge_added
    False) -- and the member is STILL findable through search."""
    space = norule_space
    n = _Nonce()

    na = n.next()
    await _w(pool, space, "note", "Anchor", f"Anchor fact about the topic ({na})", _INSERT)

    n2 = n.next()
    r = await _w(pool, space, "note", "MergeLink",
                 f"A distinct new statement on the same topic ({n2})", _MERGE)
    assert r["outcome"] == "merged_linked"
    assert r["cluster_edge_added"] is False, "no rule -> edge skipped"
    member_id = r["node"]["id"]

    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM edges WHERE space_id = %s AND type = 'same_topic'", (space,))
    assert cur.fetchone()[0] == 0, "no same_topic edge was attached"
    cur.execute("SELECT count(*) FROM dedup_log WHERE space_id = %s AND band = 'merge_linked'", (space,))
    assert cur.fetchone()[0] == 1, "the authoritative membership record survives in dedup_log"

    # the headline: the member is still findable despite the missing edge.
    await _assert_findable(pool, space, n2, member_id)
    _assert_embedding_integrity(conn, space)


# --- Phase C: can I find the fact stored in an attribute? (§6) ----------------

async def _w_person(pool, space, title, body, attrs):
    """Write a person node through the real Phase C tool-layer path: resolve the
    attr surface, embed searchable_text, store extra_search."""
    vector, extra = await embed_with_attr_surface(pool, space, "person", title, body, attrs)
    vec_ok = BandThresholds(t_high=1.1, t_low=1.05)  # force insert (no banding here)
    return await write(pool, space, "p1", "person", "work", title, body, attrs, vector,
                       "pytest", thresholds=vec_ok, extra_search=extra)


def _lexically_indexed(conn, nid, term):
    """Does the node's `search` tsvector (title A + body B + extra_search C +
    addenda D) match `term`? The deterministic weight-C findability the spec's
    (d-extended) assertion means -- unlike search(), which also runs a vector leg
    that returns the nearest node even when nothing lexically matches (so a bare
    'not in search results' can't prove a nonce is absent from the surface)."""
    cur = conn.cursor()
    cur.execute("SELECT search @@ plainto_tsquery('english', %s) FROM nodes WHERE id = %s", (term, nid))
    return cur.fetchone()[0]


async def test_dual_index_attr_findability(pool, rule_space, conn):
    """A fact that lives ONLY in a searchable attribute is findable via search
    (the anti-Phase-A-guidance case: the body does NOT restate the attr, so the
    engine must not depend on extractor discipline)."""
    space = rule_space
    # occupation carries a nonce the body never mentions.
    r = await _w_person(pool, space, "Lucy",
                        "Lucy is a friend from the pottery class.",
                        {"occupation": "glassblower zq7xk-77", "location": "Bristol"})
    person_id = r["node"]["id"]

    # (d-extended) the nonce (lexical leg, weight C) and the plain word both surface it.
    for query in ("zq7xk-77", "glassblower"):
        res = await search(pool, space, "p1", "work", query, "pytest")
        ids = {x["node"]["id"] for x in res["results"]}
        assert person_id in ids, f"attr fact not findable by {query!r}: {ids}"

    # extra_search stores the render; embedding integrity holds with it.
    cur = conn.cursor()
    cur.execute("SELECT extra_search FROM nodes WHERE id = %s", (person_id,))
    assert cur.fetchone()[0] == "location: Bristol\noccupation: glassblower zq7xk-77"
    _assert_embedding_integrity(conn, space)


async def test_dual_index_non_searchable_enum_excluded_from_surface(pool, rule_space, conn):
    """Two person nodes differing ONLY in a non-searchable enum (strength) have
    IDENTICAL embedded text -- the homogenization guard (§6): enum flags never
    enter the surface, so they cannot push same-type nodes together."""
    space = rule_space
    a = await _w_person(pool, space, "Ann", "Ann is here.", {"strength": "hard"})
    b = await _w_person(pool, space, "Ann", "Ann is here.", {"strength": "soft"})
    cur = conn.cursor()
    cur.execute("SELECT extra_search FROM nodes WHERE id IN (%s, %s)",
                (a["node"]["id"], b["node"]["id"]))
    extras = [row[0] for row in cur.fetchall()]
    assert extras == ["", ""], "a non-searchable enum must not render into the surface"


async def test_dual_index_update_adds_searchable_attr_makes_findable(pool, rule_space, conn):
    """An update that adds a searchable attr to an attr-less node re-embeds and
    makes the new attr fact findable (the amend-path arm of I4)."""
    space = rule_space
    r = await _w_person(pool, space, "Mia", "Mia is a neighbour.", {})
    nid = r["node"]["id"]
    # before: the nonce is not in the node's surface (lexical index).
    assert not _lexically_indexed(conn, nid, "zq7xk-88"), "nonce absent before the attr is added"
    # add a searchable attr.
    await update(pool, space, "p1", nid, attrs={"occupation": "luthier zq7xk-88"})
    assert _lexically_indexed(conn, nid, "zq7xk-88"), "added searchable attr not in the surface"
    res = await search(pool, space, "p1", "work", "zq7xk-88", "pytest")
    assert nid in {x["node"]["id"] for x in res["results"]}, "added searchable attr not findable"
    _assert_embedding_integrity(conn, space)


async def test_dual_index_legacy_row_findable_only_after_surface_rebuild(pool, rule_space, conn):
    """The pinned pre-C failure and its fix: a legacy row seeded with
    extra_search='' (attr not in the surface) is NOT findable by its attr nonce;
    after `surface rebuild` it is."""
    space = rule_space
    vec = _emb.embed_document(_emb.searchable_text("Old", "Old contact.", ""))
    lit = "[" + ",".join(str(x) for x in vec) + "]"
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
        "embedding_model, source_client, author_principal, extra_search) "
        "VALUES (%s,'person','work','Old','Old contact.',%s,%s::vector,%s,'pytest','p1','') RETURNING id",
        (space, Jsonb({"occupation": "cooper zq7xk-99"}), lit, _emb.MODEL_STAMP))
    nid = str(cur.fetchone()[0])
    conn.commit()

    # before rebuild: the attr nonce is NOT in the surface (the pinned pre-C failure).
    assert not _lexically_indexed(conn, nid, "zq7xk-99"), "precondition: legacy attr not in the surface"

    # rebuild the surface (in-test, on the same connection).
    rebuild_surface(conn, space, embed_document=_emb.embed_document)

    assert _lexically_indexed(conn, nid, "zq7xk-99"), "attr in the surface after rebuild"
    res = await search(pool, space, "p1", "work", "zq7xk-99", "pytest")
    assert nid in {x["node"]["id"] for x in res["results"]}, "legacy row findable after rebuild"
    _assert_embedding_integrity(conn, space)


async def test_dual_index_flag_off_writes_empty_surface(pool, rule_space, conn):
    """The control arm's precondition: with write.attr_surface=false the write path
    stores extra_search='' and behaves byte-identically to Phase B."""
    space = rule_space
    cur = conn.cursor()
    cur.execute("INSERT INTO config (space_id, key, value) VALUES (%s, %s, %s)",
                (space, ATTR_SURFACE_KEY, Jsonb(False)))
    conn.commit()
    r = await _w_person(pool, space, "Zoe", "Zoe is a colleague.",
                        {"occupation": "glassblower zq7xk-77"})
    cur.execute("SELECT extra_search FROM nodes WHERE id = %s", (r["node"]["id"],))
    assert cur.fetchone()[0] == "", "flag off -> extra_search=''"
    # the attr nonce never entered the surface (weight-C index empty of it).
    assert not _lexically_indexed(conn, r["node"]["id"], "zq7xk-77"), \
        "flag off -> attr never entered the surface"
