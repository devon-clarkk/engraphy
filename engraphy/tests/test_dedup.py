"""engraphy.core.dedup — design/implementation/dedup-write-path-plan.md.
Band-selection boundary semantics first (pure, no model/DB dependency); the
full transactional write pipeline (candidate query, advisory lock, MERGE/
PENDING/INSERT branches, race/crash tests) is built incrementally on top.

Pipeline tests use SYNTHETIC seeded unit vectors at controlled cosine
similarity, never the real embedding model -- this exercises the mechanics
(candidate query, band branching, advisory lock) independently of
QUESTIONS.md "embedding-task-prefix", which only blocks *real-model*
baselining (fixtures/dedup_cases.yaml).
"""
import asyncio
import math

import psycopg
import pytest
from psycopg.types.json import Jsonb

from engraphy.core.dedup import (
    MERGED_INSTRUCTION,
    BandThresholds,
    ConfigError,
    NotFoundError,
    PendingExpiredError,
    ScopeUnknownError,
    SupersedeUnresolvedBandError,
    ValidationError,
    resolve_duplicate,
    select_band,
    supersede,
    write,
)
from engraphy.tests import bandvalues as bv
from engraphy.tests.conftest import DATABASE_URL


@pytest.mark.parametrize(
    "similarity,expected",
    [
        (1.00, "merge"),
        (0.96, "merge"),
        (0.95, "merge"),          # exact boundary: >= t_high -> merge
        (0.9499, "pending"),      # just under t_high
        (0.87, "pending"),
        (0.80, "pending"),        # exact boundary: >= t_low -> pending
        (0.7999, "insert"),       # just under t_low
        (0.5, "insert"),
        (0.0, "insert"),
    ],
)
def test_select_band_default_thresholds(similarity, expected):
    assert select_band(similarity, BandThresholds()) == expected


def test_select_band_respects_custom_thresholds():
    t = BandThresholds(t_high=0.90, t_low=0.70)
    assert select_band(0.90, t) == "merge"
    assert select_band(0.89, t) == "pending"
    assert select_band(0.70, t) == "pending"
    assert select_band(0.69, t) == "insert"


def _unit_vector_at_angle(theta: float) -> list[float]:
    """A 384-dim unit vector in the e1/e2 plane at angle theta from e1, so
    dot(_unit_vector_at_angle(0), _unit_vector_at_angle(theta)) == cos(theta)
    exactly -- a controlled, known cosine similarity independent of any
    embedding model."""
    vec = [0.0] * 384
    vec[0] = math.cos(theta)
    vec[1] = math.sin(theta)
    return vec


def _bootstrap_write_space(conn, space_id):
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, %s)", (space_id, "Write Space"))
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p1', 'P')", (space_id,)
    )
    cur.execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES "
        "(%s, 'widget', 'w', %s), (%s, 'error', 'e', %s)",
        (space_id, Jsonb({"attrs": {"closed": False}}), space_id, Jsonb({"attrs": {"closed": False}})),
    )
    # resolve_duplicate(distinct) auto-adds a relates_to edge to the nearest
    # candidate (02 §Deduplication write path), so the write space must declare
    # that edge type and its rule -- same shape as conftest's bootstrap_space.
    cur.execute(
        "INSERT INTO edge_types (space_id, name, description, bidirectional) VALUES "
        "(%s, 'relates_to', 'Generic association.', true), "
        "(%s, 'supersedes', 'Replacement; the old node''s status becomes superseded.', false), "
        "(%s, 'same_topic', 'Same topic, distinct content (Phase B merge-link).', true)",
        (space_id, space_id, space_id),
    )
    # supersedes is declared for 'widget' only, NOT 'error' -- deliberately
    # modelling the example pack, which restricts supersedes to
    # decision/pattern rather than declaring it wildcard like the starter pack
    # does. That asymmetry is what test_supersede_without_matching_edge_rule_
    # raises_edge_rule_violation exercises (QUESTIONS.md
    # "supersede-edge-type-pack-inconsistency", resolved: a pack may legitimately
    # decline a supersession, and the edges_validate trigger is what says so).
    cur.execute(
        "INSERT INTO edge_rules (space_id, type, src_type, dst_type) VALUES "
        "(%s, 'relates_to', 'widget', 'widget'), (%s, 'relates_to', 'error', 'error'), "
        "(%s, 'supersedes', 'widget', 'widget'), "
        "(%s, 'same_topic', 'widget', 'widget'), (%s, 'same_topic', 'error', 'error')",
        (space_id, space_id, space_id, space_id, space_id),
    )
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, 'scope1', 'S', 'p1', 'private')",
        (space_id,),
    )
    conn.commit()


def _cleanup_write_space(conn, space_id):
    cur = conn.cursor()
    cur.execute("DELETE FROM inbox WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM config WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM audit_log WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM dedup_log WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM pending_writes WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM edges WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM nodes WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM scope_grants WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM scopes WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM edge_rules WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM edge_types WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM node_types WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM principals WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


@pytest.fixture
def write_space(conn, request):
    space_id = ("wr-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap_write_space(conn, space_id)
    yield space_id
    _cleanup_write_space(conn, space_id)


def _seed_node(conn, space_id, node_type, title, body, attrs, embedding_vector):
    """Insert a pre-existing 'candidate' node directly (bypassing write()),
    committed so the async pool's separate connection can see it."""
    cur = conn.cursor()
    embedding_literal = "[" + ",".join(str(x) for x in embedding_vector) + "]"
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
        "embedding_model, source_client, author_principal) "
        "VALUES (%s, %s, 'scope1', %s, %s, %s, %s::vector, 'test-model', 'pytest', 'p1') "
        "RETURNING id",
        (space_id, node_type, title, body, Jsonb(attrs), embedding_literal),
    )
    (node_id,) = cur.fetchone()
    conn.commit()
    return node_id


async def test_write_insert_with_no_candidates(pool, write_space, conn):
    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "A brand new title", "A brand new body.", {},
        _unit_vector_at_angle(0), "pytest",
    )
    assert result["outcome"] == "inserted"
    assert result["node"]["title"] == "A brand new title"
    assert result["node"]["status"] == "active"

    # dedup_log has no SELECT policy (DECISIONS-DELTA: never read via an MCP
    # tool) -- verify via the superuser connection, not the app-role pool.
    cur = conn.cursor()
    cur.execute(
        "SELECT band, similarity, candidate_id, node_id FROM dedup_log WHERE space_id = %s",
        (write_space,),
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    band, similarity, candidate_id, node_id = rows[0]
    assert band == "insert"
    assert similarity is None
    assert candidate_id is None
    assert str(node_id) == result["node"]["id"]


async def test_write_insert_stores_source_session(pool, write_space, conn):
    """QUESTIONS.md 5.6: session_id (wire) -> nodes.source_session (storage),
    a thin passthrough; NULL when the caller sends nothing."""
    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "A brand new title", "A brand new body.", {},
        _unit_vector_at_angle(0), "pytest", source_session="sess-abc",
    )
    cur = conn.cursor()
    cur.execute("SELECT source_session FROM nodes WHERE id = %s", (result["node"]["id"],))
    assert cur.fetchone()[0] == "sess-abc"


async def test_write_insert_without_source_session_is_null(pool, write_space, conn):
    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "A brand new title", "A brand new body.", {},
        _unit_vector_at_angle(0), "pytest",
    )
    cur = conn.cursor()
    cur.execute("SELECT source_session FROM nodes WHERE id = %s", (result["node"]["id"],))
    assert cur.fetchone()[0] is None


async def test_write_insert_audit_log_action_defaults_to_write(pool, write_space, conn):
    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "A brand new title", "A brand new body.", {},
        _unit_vector_at_angle(0), "pytest",
    )
    cur = conn.cursor()
    cur.execute(
        "SELECT action FROM audit_log WHERE space_id = %s AND detail->>'node_id' = %s",
        (write_space, result["node"]["id"]),
    )
    assert cur.fetchone()[0] == "write"


async def test_write_insert_audit_log_action_carries_alias_identity(pool, write_space, conn):
    """design/03 §Tool aliases: "same audit identity (logged as `write via
    log_error`)" -- app.py passes aliases.resolve_alias_call's audit
    identity through as write()'s `action` override."""
    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "A brand new title", "A brand new body.", {},
        _unit_vector_at_angle(0), "pytest", action="write via log_error",
    )
    cur = conn.cursor()
    cur.execute(
        "SELECT action FROM audit_log WHERE space_id = %s AND detail->>'node_id' = %s",
        (write_space, result["node"]["id"]),
    )
    assert cur.fetchone()[0] == "write via log_error"


async def test_write_merge_source_session_lands_on_member(pool, write_space, conn):
    """Phase B: a novel merge-link inserts the incoming as its own member row, so
    its provenance (source_session, for purge-session's threat window) lives on
    that row -- not demoted into a get-only addendum as before."""
    _seed_node(
        conn, write_space, "widget", "Coffee maker needs descaling",
        "Descale monthly or it breaks.", {}, _unit_vector_at_angle(0),
    )
    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Coffee machine descaling", "A totally different sentence about something else entirely new.",
        {}, _unit_vector_at_angle(0), "pytest", source_session="sess-merge",
    )
    assert result["outcome"] == "merged_linked"
    cur = conn.cursor()
    cur.execute("SELECT source_session FROM nodes WHERE id = %s", (result["node"]["id"],))
    assert cur.fetchone()[0] == "sess-merge"


async def test_resolve_duplicate_distinct_preserves_source_session_across_park(pool, write_space, conn):
    """A parked write's provenance must survive the park (design/04's threat
    window), or purge-session can never reach a write resolved after the fact."""
    _seed_node(conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0))
    parked = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Similar-ish body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest", source_session="sess-parked",
    )
    assert parked["outcome"] == "needs_confirmation"

    resolved = await resolve_duplicate(pool, write_space, "p1", parked["pending_id"], "distinct")
    assert resolved["outcome"] == "inserted"

    cur = conn.cursor()
    cur.execute("SELECT source_session FROM nodes WHERE id = %s", (resolved["node"]["id"],))
    assert cur.fetchone()[0] == "sess-parked"


async def test_resolve_duplicate_merge_source_session_lands_on_member(pool, write_space, conn):
    """A parked write's provenance must survive park + an explicit novel merge:
    the merge-link member row carries the parked source_session."""
    candidate_id = _seed_node(
        conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0)
    )
    parked = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "A novel enough sentence to add as an addendum here.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest", source_session="sess-merge-parked",
    )
    assert parked["outcome"] == "needs_confirmation"

    resolved = await resolve_duplicate(
        pool, write_space, "p1", parked["pending_id"], "merge", merge_into=str(candidate_id)
    )
    assert resolved["outcome"] == "merged_linked"

    cur = conn.cursor()
    cur.execute("SELECT source_session FROM nodes WHERE id = %s", (resolved["node"]["id"],))
    assert cur.fetchone()[0] == "sess-merge-parked"


async def test_write_insert_with_dissimilar_candidate(pool, write_space, conn):
    # a pre-existing "candidate" node, orthogonal (similarity 0) to the incoming write.
    _seed_node(conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0))

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Totally different", "Totally different body.", {},
        _unit_vector_at_angle(math.pi / 2), "pytest",  # orthogonal -> similarity 0.0
    )
    assert result["outcome"] == "inserted"


async def test_write_nonexistent_scope_raises_scope_unknown(pool, write_space, conn):
    """QUESTIONS.md "write-scope-writable-precheck": write() itself now
    pre-checks writable_scopes_async, before the advisory lock or the
    candidate query -- no node, dedup_log, or audit_log row for a rejected
    write. errors.json's write/SCOPE_UNKNOWN case pins this exact text."""
    with pytest.raises(ScopeUnknownError, match="ENGRAPHY_SCOPE_UNKNOWN"):
        await write(
            pool, write_space, "p1", "widget", "nope",
            "T", "B", {}, _unit_vector_at_angle(0), "pytest",
        )
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s AND title = 'T'", (write_space,))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT count(*) FROM dedup_log WHERE space_id = %s", (write_space,))
    assert cur.fetchone()[0] == 0


async def test_write_scope_message_matches_wire_fixture_wording(pool, write_space):
    with pytest.raises(
        ScopeUnknownError,
        match=r"ENGRAPHY_SCOPE_UNKNOWN: scope 'nope' does not exist or is not writable",
    ):
        await write(
            pool, write_space, "p1", "widget", "nope",
            "T", "B", {}, _unit_vector_at_angle(0), "pytest",
        )


async def test_write_readable_but_unwritable_scope_raises_scope_unknown(pool, write_space, conn):
    """The same not-found collapse for a scope that IS readable (a teammate's
    private scope, p1 granted read-only) but not writable, not just a
    nonexistent id -- the plan's "nonexistent, unreadable, and readable-but-
    unwritable scopes all get the byte-identical message"."""
    cur = conn.cursor()
    cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p2', 'P2')", (write_space,))
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, 'p2-private', 'P2 Private', 'p2', 'private')",
        (write_space,),
    )
    cur.execute(
        "INSERT INTO scope_grants (space_id, scope_id, principal, level) VALUES (%s, 'p2-private', 'p1', 'read')",
        (write_space,),
    )
    conn.commit()

    with pytest.raises(ScopeUnknownError, match="ENGRAPHY_SCOPE_UNKNOWN"):
        await write(
            pool, write_space, "p1", "widget", "p2-private",
            "T", "B", {}, _unit_vector_at_angle(0), "pytest",
        )


async def test_write_pending_parks_no_node_row(pool, write_space, conn):
    candidate_id = _seed_node(
        conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0)
    )

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Similar-ish body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",  # a confirm-band similarity
    )
    assert result["outcome"] == "needs_confirmation"
    assert result["candidates"][0]["id"] == str(candidate_id)
    assert result["candidates"][0]["similarity"] == bv.PENDING
    assert "resolve_duplicate" in result["instruction"]
    assert result["expires_at"]

    # no node row was created for a PENDING write.
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s AND title = 'Similar-ish'", (write_space,))
    assert cur.fetchone()[0] == 0

    # pending_writes' SELECT policy is author-scoped (space + author_principal
    # = current principal), not deny-all like dedup_log -- but the app-role
    # pool's identity was reset when its transaction ended, so just verify via
    # the superuser connection, consistent with the other DB-state checks here.
    cur.execute(
        "SELECT author_principal, payload, expires_at FROM pending_writes WHERE space_id = %s",
        (write_space,),
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    author_principal, payload, expires_at = rows[0]
    assert author_principal == "p1"
    assert payload["title"] == "Similar-ish"
    assert payload["candidates"][0]["id"] == str(candidate_id)
    assert expires_at is not None


async def test_write_merge_novel_body_merge_links(pool, write_space, conn):
    """The measured leak, fixed (Phase B §2.1): a novel body at >= t_high is no
    longer demoted into a get-only addendum -- it inserts as its own embedded,
    searchable member row (`merged_linked`), keeping its own title/body/attrs and
    a `same_topic` edge to the canonical. This is I1 (non-destructive dedup)."""
    canonical_id = _seed_node(
        conn, write_space, "widget", "Coffee maker needs descaling",
        "Descale monthly or it breaks.", {}, _unit_vector_at_angle(0),
    )

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Coffee machine descaling", "A totally different sentence about something else entirely new.",
        {}, _unit_vector_at_angle(0), "pytest",  # identical embedding -> similarity 1.0 -> merge band
    )
    assert result["outcome"] == "merged_linked"
    assert result["similarity"] == 1.0
    assert result["cluster_edge_added"] is True
    assert result["canonical"]["id"] == str(canonical_id)
    # the member is its own embedded, active row carrying the incoming body...
    member_id = result["node"]["id"]
    assert result["node"]["body"] == "A totally different sentence about something else entirely new."
    cur = conn.cursor()
    cur.execute("SELECT status, author_principal FROM nodes WHERE id = %s", (member_id,))
    status, author = cur.fetchone()
    assert status == "active"
    assert author == "p1"
    # ...no addendum was written on the canonical (the old leak site)...
    cur.execute("SELECT attrs FROM nodes WHERE id = %s", (canonical_id,))
    assert cur.fetchone()[0].get("addenda", []) == []
    # ...and a same_topic edge links member -> canonical.
    cur.execute(
        "SELECT count(*) FROM edges WHERE space_id = %s AND type = 'same_topic' "
        "AND src_id = %s AND dst_id = %s", (write_space, member_id, str(canonical_id)),
    )
    assert cur.fetchone()[0] == 1
    # dedup_log records band merge_linked, node_id = member, candidate = canonical.
    cur.execute("SELECT node_id, candidate_id FROM dedup_log WHERE space_id = %s AND band = 'merge_linked'",
                (write_space,))
    node_id, candidate_id = cur.fetchone()
    assert str(node_id) == member_id
    assert str(candidate_id) == str(canonical_id)


async def test_write_merge_non_novel_body_no_addendum(pool, write_space, conn):
    _seed_node(
        conn, write_space, "widget", "Coffee maker needs descaling",
        "Descale monthly or it breaks the heating element.", {}, _unit_vector_at_angle(0),
    )

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Coffee machine descaling", "Descale monthly or it breaks the heating element.",
        {}, _unit_vector_at_angle(0), "pytest",  # same wording -> J=1.0 -> not novel
    )
    assert result["outcome"] == "merged"
    assert result["addendum_added"] is False
    assert "addenda" not in result["canonical"]["attrs"]


async def test_merge_link_graceful_skip_when_pack_lacks_rule(pool, write_space, conn):
    """§1.3: with no `same_topic` edge_rules row, a novel merge still inserts the
    member row (I1 holds) and logs band merge_linked -- but the edge is SKIPPED
    (pre-check, not a caught trigger violation), `cluster_edge_added` is false, and
    the member is still its own findable row. No fallback to relates_to."""
    # remove the rule this write space normally declares.
    cur = conn.cursor()
    cur.execute("DELETE FROM edge_rules WHERE space_id = %s AND type = 'same_topic'", (write_space,))
    conn.commit()
    _seed_node(
        conn, write_space, "widget", "Canonical", "Descale monthly or it breaks.", {}, _unit_vector_at_angle(0),
    )

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Member", "A totally different sentence about something else entirely new.",
        {}, _unit_vector_at_angle(0), "pytest",
    )
    assert result["outcome"] == "merged_linked"
    assert result["cluster_edge_added"] is False
    member_id = result["node"]["id"]
    # the member row exists and is active (the write did NOT abort)...
    cur.execute("SELECT status FROM nodes WHERE id = %s", (member_id,))
    assert cur.fetchone()[0] == "active"
    # ...no same_topic edge, and NO relates_to fallback either...
    cur.execute("SELECT count(*) FROM edges WHERE space_id = %s", (write_space,))
    assert cur.fetchone()[0] == 0
    # ...but the authoritative membership record survives in dedup_log.
    cur.execute("SELECT count(*) FROM dedup_log WHERE space_id = %s AND band = 'merge_linked'", (write_space,))
    assert cur.fetchone()[0] == 1


async def test_merge_link_novelty_corpus_includes_same_topic_peers(pool, write_space, conn):
    """§2.1 step 2: the novelty corpus includes existing same_topic PEER bodies,
    so a fact restated after being merge-linked once is judged non-novel against
    the cluster and absorbs, rather than spawning a duplicate member. The peer is
    load-bearing here: the restatement bands against the CANONICAL (nearer), whose
    own body alone would judge it novel -- only the peer's body in the corpus
    tips it to non-novel."""
    # canonical's tokens are a subset of the member's, so with the member (peer)
    # in the corpus the restatement is a token-subset (J -> 1.0), but against the
    # canonical body alone it is novel.
    _seed_node(
        conn, write_space, "widget", "Priya", "Priya works as a nurse.", {}, _unit_vector_at_angle(0),
    )
    member_body = "Priya works as a nurse in Leeds at the general paediatric teaching hospital."
    # first statement: novel vs the canonical -> merge-link a member (its
    # embedding sits inside the merge band but short of the canonical's own
    # angle, so a later write at that exact angle prefers the canonical as the
    # banded candidate).
    first = await write(
        pool, write_space, "p1", "widget", "scope1", "Priya detail", member_body,
        {}, _unit_vector_at_angle(math.acos(bv.MERGE)), "pytest",
    )
    assert first["outcome"] == "merged_linked"

    # restate the very same fact; it bands >= t_high against the canonical, and the
    # peer (member) body in the corpus makes it non-novel -> absorb, no new row.
    second = await write(
        pool, write_space, "p1", "widget", "scope1", "Priya detail again", member_body,
        {}, _unit_vector_at_angle(0), "pytest",
    )
    assert second["outcome"] == "merged", "the peer-inclusive corpus judged the restatement non-novel"

    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s", (write_space,))
    assert cur.fetchone()[0] == 2, "no duplicate member -- canonical + the one member only"


async def test_import_mode_merge_link_is_silent_but_keeps_the_member(pool, write_space, conn):
    """§2.2 import-mode: a novel import merge runs the merge-link mechanics in
    full (member row, same_topic edge, dedup_log) but collapses the envelope to
    just the outcome -- the member's text is kept (it is a row), so no
    dropped-addendum tell is needed."""
    _seed_node(
        conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0)
    )
    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Twin", "A wholly distinct body carrying its own new information.", {},
        _unit_vector_at_angle(0), "pytest", import_mode=True,
    )
    assert result == {"v": 1, "outcome": "merged_linked"}

    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s", (write_space,))
    assert cur.fetchone()[0] == 2, "the member row was kept"
    cur.execute("SELECT count(*) FROM edges WHERE space_id = %s AND type = 'same_topic'", (write_space,))
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT count(*) FROM dedup_log WHERE space_id = %s AND band = 'merge_linked'", (write_space,))
    assert cur.fetchone()[0] == 1


async def test_import_rerun_of_a_merge_link_creates_zero_new_rows(pool, write_space, conn):
    """§2.2 import idempotency, updated: a first novel import merge-links a member;
    re-importing the identical line bands >= t_high against its own prior row, is
    judged non-novel against the cluster corpus, and absorbs with no addendum --
    the second run creates zero rows (design/02's "re-running the same import is a
    no-op")."""
    _seed_node(
        conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0)
    )
    body = "A wholly distinct body carrying its own new information."
    first = await write(
        pool, write_space, "p1", "widget", "scope1", "Twin", body, {},
        _unit_vector_at_angle(0), "pytest", import_mode=True,
    )
    assert first == {"v": 1, "outcome": "merged_linked"}
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s", (write_space,))
    assert cur.fetchone()[0] == 2

    second = await write(
        pool, write_space, "p1", "widget", "scope1", "Twin", body, {},
        _unit_vector_at_angle(0), "pytest", import_mode=True,
    )
    assert second == {"v": 1, "outcome": "merged", "addendum_added": False}
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s", (write_space,))
    assert cur.fetchone()[0] == 2, "the re-import created zero new rows"


async def test_write_merge_error_reoccurrence_adds_addendum_even_when_not_novel(pool, write_space, conn):
    _seed_node(
        conn, write_space, "error", "Deploy failed", "Deploy failed because of a config error.",
        {"happened_at": "2026-01-01"}, _unit_vector_at_angle(0),
    )

    result = await write(
        pool, write_space, "p1", "error", "scope1",
        "Deploy failed", "Deploy failed because of a config error.",  # identical wording -> not novel
        {"happened_at": "2026-02-01"},  # but a distinct occurrence date
        _unit_vector_at_angle(0), "pytest",
    )
    assert result["outcome"] == "merged"
    assert result["addendum_added"] is True
    # Q1: addenda never ships in `attrs` on the wire, even freshly merged.
    assert "addenda" not in result["canonical"]["attrs"]
    cur = conn.cursor()
    cur.execute("SELECT attrs FROM nodes WHERE id = %s", (result["canonical"]["id"],))
    addenda = cur.fetchone()[0]["addenda"]
    assert len(addenda) == 1
    # QUESTIONS.md "error-reoccurrence-addendum-shape": the occurrence date
    # itself must survive the merge, or "a re-occurrence is data" is false.
    assert addenda[0]["happened_at"] == "2026-02-01"


async def test_write_merge_chases_canonical_through_already_merged_node(pool, write_space, conn):
    original_id = _seed_node(
        conn, write_space, "widget", "Original", "Original body text here.", {}, _unit_vector_at_angle(0),
    )
    canonical_id = _seed_node(
        conn, write_space, "widget", "Canonical", "Canonical body text here.", {}, _unit_vector_at_angle(0),
    )
    cur = conn.cursor()
    cur.execute(
        "UPDATE nodes SET status = 'merged', canonical_id = %s WHERE id = %s",
        (canonical_id, original_id),
    )
    conn.commit()

    # the candidate query only ever returns status='active' rows, so it will
    # find `canonical_id` (not the already-merged `original_id`) as the
    # nearest candidate -- this exercises _resolve_canonical as a no-op
    # defensive chase, per its own docstring on why this matters more for
    # resolve_duplicate's stale parked candidates than a live write().
    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "New", "Canonical body text here.", {},  # non-novel restatement -> absorb into the chased canonical
        _unit_vector_at_angle(0), "pytest",
    )
    assert result["outcome"] == "merged"
    assert result["canonical"]["id"] == str(canonical_id)


async def test_write_race_advisory_lock_serializes_concurrent_duplicate_writes(pool, write_space, conn):
    """The plan's lynchpin test (dedup-write-path-plan.md §Decision: why an
    advisory lock, §Test plan "Race test"): two connections write the same
    novel text, released together via asyncio.gather -- the pg_advisory_xact_
    lock (space, type) must serialize them so the second writer's candidate
    query sees the first writer's already-committed node, producing exactly
    one node and one 'merged' outcome. Without the lock (or under the wrong
    isolation level -- see DECISIONS-DELTA's READ COMMITTED pin) both writers
    would see zero candidates and both INSERT: a permanent duplicate, the
    exact failure dedup exists to prevent. Repeated 100x per the plan; each
    trial gets its own node_type so an earlier trial's node can never become
    an unintended candidate for a later one.
    """
    for i in range(100):
        node_type = f"race{i}"
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES (%s, %s, 'r', %s)",
            (write_space, node_type, Jsonb({"attrs": {"closed": False}})),
        )
        conn.commit()

        results = await asyncio.gather(
            write(
                pool, write_space, "p1", node_type, "scope1",
                "Race title", "Race body text, identical across both writers.", {},
                _unit_vector_at_angle(0), "pytest",
            ),
            write(
                pool, write_space, "p1", node_type, "scope1",
                "Race title", "Race body text, identical across both writers.", {},
                _unit_vector_at_angle(0), "pytest",
            ),
        )
        outcomes = sorted(r["outcome"] for r in results)
        assert outcomes == ["inserted", "merged"], f"trial {i}: {outcomes}"

        cur.execute(
            "SELECT count(*) FROM nodes WHERE space_id = %s AND type = %s AND status = 'active'",
            (write_space, node_type),
        )
        assert cur.fetchone()[0] == 1, f"trial {i}: expected exactly one active node"


# ---- resolve_duplicate ------------------------------------------------


async def test_resolve_duplicate_distinct_inserts_and_attaches_relates_to(pool, write_space, conn):
    candidate_id = _seed_node(
        conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0)
    )
    parked = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Similar-ish body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",  # a confirm-band similarity
    )
    assert parked["outcome"] == "needs_confirmation"

    result = await resolve_duplicate(pool, write_space, "p1", parked["pending_id"], "distinct")
    assert result["outcome"] == "inserted"
    assert result["relates_edge_added"] is True

    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FROM edges WHERE space_id = %s AND type = 'relates_to' "
        "AND src_id = %s AND dst_id = %s",
        (write_space, result["node"]["id"], str(candidate_id)),
    )
    assert cur.fetchone()[0] == 1

    # resolution deletes the pending_writes row.
    cur.execute("SELECT count(*) FROM pending_writes WHERE id = %s", (parked["pending_id"],))
    assert cur.fetchone()[0] == 0


async def test_resolve_duplicate_distinct_converts_to_merge_when_world_moved(pool, write_space, conn):
    _seed_node(conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0))
    parked = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Similar-ish body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",  # a confirm-band similarity
    )

    # world moved: a near-identical node landed while this write sat parked --
    # seeded at the PARKED PAYLOAD's own embedding angle (not the original
    # candidate's), so its similarity to the payload is 1.0, not another confirm-band value.
    _seed_node(
        conn, write_space, "widget", "Similar-ish exact",
        "Similar-ish body.", {}, _unit_vector_at_angle(math.acos(bv.PENDING)),
    )

    result = await resolve_duplicate(pool, write_space, "p1", parked["pending_id"], "distinct")
    assert result["outcome"] == "merged"
    assert "relates_edge_added" not in result

    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FROM nodes WHERE space_id = %s AND title = 'Similar-ish'", (write_space,)
    )
    assert cur.fetchone()[0] == 0  # never inserted -- converted straight to merge


async def test_resolve_duplicate_merge_into_specific_candidate(pool, write_space, conn):
    """Phase B §2.2: an explicit merge with NOVEL content returns `merged_linked`
    -- the caller's "these belong together" intent is honored AS A LINK (I1: no
    write reduces findable facts), not as a get-only addendum. The member row is
    inserted and `same_topic`-linked to the chosen candidate."""
    candidate_id = _seed_node(
        conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0)
    )
    parked = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "A genuinely novel addendum sentence not seen before.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",
    )

    result = await resolve_duplicate(
        pool, write_space, "p1", parked["pending_id"], "merge", merge_into=str(candidate_id),
    )
    assert result["outcome"] == "merged_linked"
    assert result["canonical"]["id"] == str(candidate_id)
    assert result["cluster_edge_added"] is True
    member_id = result["node"]["id"]

    cur = conn.cursor()
    # the member is its own embedded, active row (not an addendum)...
    cur.execute("SELECT status FROM nodes WHERE id = %s", (member_id,))
    assert cur.fetchone()[0] == "active"
    # ...linked to the candidate by a same_topic edge...
    cur.execute(
        "SELECT count(*) FROM edges WHERE space_id = %s AND type = 'same_topic' "
        "AND src_id = %s AND dst_id = %s",
        (write_space, member_id, str(candidate_id)),
    )
    assert cur.fetchone()[0] == 1
    # ...logged with band merge_linked, node_id = member (the parked write logged
    # its own 'pending' row earlier, so filter to the resolve outcome's row)...
    cur.execute("SELECT node_id FROM dedup_log WHERE space_id = %s AND band = 'merge_linked'",
                (write_space,))
    assert str(cur.fetchone()[0]) == member_id
    cur.execute("SELECT count(*) FROM pending_writes WHERE id = %s", (parked["pending_id"],))
    assert cur.fetchone()[0] == 0


async def test_resolve_duplicate_merge_non_novel_absorbs(pool, write_space, conn):
    """The other side of the §2.2 split: an explicit merge of a NON-novel
    restatement still absorbs (provenance merge), returning `merged`."""
    candidate_id = _seed_node(
        conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0)
    )
    parked = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Existing body.", {},  # non-novel
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",
    )
    result = await resolve_duplicate(
        pool, write_space, "p1", parked["pending_id"], "merge", merge_into=str(candidate_id),
    )
    assert result["outcome"] == "merged"
    assert result["canonical"]["id"] == str(candidate_id)
    cur = conn.cursor()
    # the resolve logged a 'merge' row (the parked write logged 'pending' earlier);
    # no merge_linked row exists.
    cur.execute("SELECT count(*) FROM dedup_log WHERE space_id = %s AND band = 'merge'", (write_space,))
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT count(*) FROM dedup_log WHERE space_id = %s AND band = 'merge_linked'", (write_space,))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s", (write_space,))
    assert cur.fetchone()[0] == 1, "absorb creates no member row"


async def test_resolve_duplicate_merge_chases_canonical_when_target_was_merged(pool, write_space, conn):
    candidate_id = _seed_node(
        conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0)
    )
    parked = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Real canonical body.", {},  # non-novel vs the chased canonical -> absorb
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",
    )

    # the candidate itself got merged into a third node while this sat parked.
    real_canonical_id = _seed_node(
        conn, write_space, "widget", "Real canonical", "Real canonical body.", {}, _unit_vector_at_angle(0),
    )
    cur = conn.cursor()
    cur.execute(
        "UPDATE nodes SET status = 'merged', canonical_id = %s WHERE id = %s",
        (real_canonical_id, candidate_id),
    )
    conn.commit()

    result = await resolve_duplicate(
        pool, write_space, "p1", parked["pending_id"], "merge", merge_into=str(candidate_id),
    )
    assert result["outcome"] == "merged"
    assert result["canonical"]["id"] == str(real_canonical_id)


async def test_resolve_duplicate_merge_target_archived_raises_pending_expired(pool, write_space, conn):
    candidate_id = _seed_node(
        conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0)
    )
    parked = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Similar-ish body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",
    )

    cur = conn.cursor()
    cur.execute("UPDATE nodes SET status = 'archived' WHERE id = %s", (candidate_id,))
    conn.commit()

    with pytest.raises(PendingExpiredError, match="ENGRAPHY_PENDING_EXPIRED"):
        await resolve_duplicate(
            pool, write_space, "p1", parked["pending_id"], "merge", merge_into=str(candidate_id),
        )


async def test_resolve_duplicate_ttl_expired_raises(pool, write_space, conn):
    _seed_node(conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0))
    parked = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Similar-ish body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",
    )

    cur = conn.cursor()
    cur.execute(
        "UPDATE pending_writes SET expires_at = now() - interval '1 hour' WHERE id = %s",
        (parked["pending_id"],),
    )
    conn.commit()

    with pytest.raises(PendingExpiredError, match="ENGRAPHY_PENDING_EXPIRED"):
        await resolve_duplicate(pool, write_space, "p1", parked["pending_id"], "distinct")


async def test_resolve_duplicate_scope_unwritable_raises(pool, write_space, conn):
    """World-change table: "Scope became unwritable for the author ->
    ENGRAPHY_SCOPE_UNKNOWN (not-found semantics)". The author is still p1 -- the
    scope itself moved out from under the parked write (archived scopes are
    excluded by engraphy_writable_scopes())."""
    _seed_node(conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0))
    parked = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Similar-ish body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",
    )

    cur = conn.cursor()
    cur.execute(
        "UPDATE scopes SET archived = true WHERE space_id = %s AND id = 'scope1'", (write_space,)
    )
    conn.commit()

    with pytest.raises(ScopeUnknownError, match="ENGRAPHY_SCOPE_UNKNOWN"):
        await resolve_duplicate(pool, write_space, "p1", parked["pending_id"], "distinct")


async def test_resolve_duplicate_by_non_author_raises_not_found(pool, write_space, conn):
    """pending_writes' RLS policy is author-scoped (visibility-and-rls-plan.md:
    "a parked write is visible only to its author -- it may quote private
    content"), so a non-author resolver gets the not-found collapse rather than
    any hint that the parked write exists at all."""
    _seed_node(conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0))
    parked = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Similar-ish body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",
    )

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p2', 'P2')", (write_space,)
    )
    conn.commit()

    with pytest.raises(NotFoundError, match="ENGRAPHY_NOT_FOUND"):
        await resolve_duplicate(pool, write_space, "p2", parked["pending_id"], "distinct")


async def test_resolve_duplicate_unknown_pending_id_raises_not_found(pool, write_space):
    with pytest.raises(NotFoundError, match="ENGRAPHY_NOT_FOUND"):
        await resolve_duplicate(pool, write_space, "p1", "00000000-0000-0000-0000-000000000000", "distinct")


async def test_resolve_duplicate_merge_without_merge_into_raises_value_error(pool, write_space, conn):
    _seed_node(conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0))
    parked = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Similar-ish body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",
    )

    with pytest.raises(ValueError, match="merge_into"):
        await resolve_duplicate(pool, write_space, "p1", parked["pending_id"], "merge")


# ---- supersede (plan step 7) -----------------------------------------
# Plan test row: "Supersede | Atomicity + self-exclusion + cross-type rejection".
# All of these are INSERT-band by construction: the MERGE/PENDING-against-a-
# THIRD-node branch is unspecified and fail-closed behind
# SupersedeUnresolvedBandError (QUESTIONS.md "supersede-nonclean-band"), so it
# is asserted as a guard below rather than given invented semantics here.


async def test_supersede_self_exclusion_replacement_does_not_band_against_old(pool, write_space, conn):
    """Trap #2: the replacement is *supposed* to be very similar to what it
    replaces. Without old_id excluded from the candidate set that similarity lands
    in the PENDING band and the supersede parks instead of writing -- so this test
    is the whole reason §Supersede atomicity says to exclude it."""
    old_id = _seed_node(
        conn, write_space, "widget", "Old node", "Old body.", {}, _unit_vector_at_angle(0)
    )

    result = await supersede(
        pool, write_space, "p1", str(old_id), "widget", "scope1",
        "New node", "New body, a revision of the old one.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING_NEAR_MERGE)), "pytest",  # vs old -> PENDING if not excluded
    )
    assert result["outcome"] == "inserted"
    assert result["superseded"] == str(old_id)

    cur = conn.cursor()
    cur.execute("SELECT status FROM nodes WHERE id = %s", (old_id,))
    assert cur.fetchone()[0] == "superseded"
    cur.execute(
        "SELECT count(*) FROM edges WHERE space_id = %s AND type = 'supersedes' "
        "AND src_id = %s AND dst_id = %s",
        (write_space, result["node"]["id"], str(old_id)),
    )
    assert cur.fetchone()[0] == 1, "supersedes edge runs replacement -> old"


async def test_supersede_cross_type_rejected(pool, write_space, conn):
    """"same type as replacement -- cross-type supersession is a modeling
    error, rejected". Rejected before the pipeline runs, so nothing is written."""
    old_id = _seed_node(
        conn, write_space, "widget", "Old node", "Old body.", {}, _unit_vector_at_angle(0)
    )

    with pytest.raises(ValidationError, match="ENGRAPHY_VALIDATION"):
        await supersede(
            pool, write_space, "p1", str(old_id), "error", "scope1",
            "New node", "New body.", {}, _unit_vector_at_angle(math.acos(bv.PENDING_NEAR_MERGE)), "pytest",
        )

    cur = conn.cursor()
    cur.execute("SELECT status FROM nodes WHERE id = %s", (old_id,))
    assert cur.fetchone()[0] == "active", "a rejected supersede leaves the old node untouched"
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s AND title = 'New node'", (write_space,))
    assert cur.fetchone()[0] == 0, "a rejected supersede writes no replacement"


async def test_supersede_non_active_old_node_rejected(pool, write_space, conn):
    old_id = _seed_node(
        conn, write_space, "widget", "Old node", "Old body.", {}, _unit_vector_at_angle(0)
    )
    cur = conn.cursor()
    cur.execute("UPDATE nodes SET status = 'archived' WHERE id = %s", (old_id,))
    conn.commit()

    with pytest.raises(ValidationError, match="ENGRAPHY_VALIDATION"):
        await supersede(
            pool, write_space, "p1", str(old_id), "widget", "scope1",
            "New node", "New body.", {}, _unit_vector_at_angle(math.acos(bv.PENDING_NEAR_MERGE)), "pytest",
        )


async def test_supersede_unknown_old_id_raises_not_found(pool, write_space):
    with pytest.raises(NotFoundError, match="ENGRAPHY_NOT_FOUND"):
        await supersede(
            pool, write_space, "p1", "00000000-0000-0000-0000-000000000000", "widget",
            "scope1", "New node", "New body.", {}, _unit_vector_at_angle(0), "pytest",
        )


def _assert_supersede_left_zero_state(conn, space_id, old_id, seeded_node_count):
    """The refusal rolls the whole call back, so nothing leaks across ANY of the
    five write-path tables (Fable, supersede-nonclean-band option (a)): no
    replacement node, no supersedes edge, no parked pending_writes row (even
    though the PENDING branch INSERTs one before the guard fires), no dedup_log
    row (deliberate -- re-logging out-of-band after rollback would breach the
    one-transaction shape), no audit_log row. The old node stays active."""
    cur = conn.cursor()
    cur.execute("SELECT status FROM nodes WHERE id = %s", (old_id,))
    assert cur.fetchone()[0] == "active", "old node survives a refused supersede"
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s", (space_id,))
    assert cur.fetchone()[0] == seeded_node_count, "no replacement node was written"
    for table in ("edges", "pending_writes", "dedup_log", "audit_log"):
        cur.execute(f"SELECT count(*) FROM {table} WHERE space_id = %s", (space_id,))
        assert cur.fetchone()[0] == 0, f"no {table} row leaked from a refused supersede"


async def test_supersede_merge_band_third_node_novel_completes_via_merge_link(pool, write_space, conn):
    """Q2 ruling (Phase B §2.3 item 5): a replacement that bands >= t_high against
    a THIRD node (not old_id) and whose content is NOVEL now proceeds as a
    merge-link -- it inserts as its own row, `same_topic`-linked to the similar
    third node, so the supersede pipeline's "insert supersedes edge; flip old"
    precondition holds and completes. The accepted consequence: a supersede can
    create a cluster with an unrelated-but-similar third node; the `same_topic`
    semantics ("banded same-topic, judged distinct") describe it truthfully."""
    old_id = _seed_node(
        conn, write_space, "widget", "Old node", "Old body.", {}, _unit_vector_at_angle(math.pi / 2)
    )
    # third node at the replacement's angle (e1) -> similarity 1.0 -> MERGE band,
    # but its body is lexically distinct from the replacement -> novel -> link.
    third = _seed_node(
        conn, write_space, "widget", "Twin of the replacement", "Twin body.", {},
        _unit_vector_at_angle(0),
    )

    result = await supersede(
        pool, write_space, "p1", str(old_id), "widget", "scope1",
        "New node", "A brand new distinct replacement body.", {},
        _unit_vector_at_angle(0), "pytest",
    )
    # supersede returns the replacement's write envelope, now `merged_linked`.
    assert result["outcome"] == "merged_linked"
    assert result["superseded"] == str(old_id)
    assert result["canonical"]["id"] == str(third)
    member_id = result["node"]["id"]

    cur = conn.cursor()
    cur.execute("SELECT status FROM nodes WHERE id = %s", (old_id,))
    assert cur.fetchone()[0] == "superseded", "the old node was flipped"
    # the replacement carries BOTH edges: same_topic -> third, supersedes -> old.
    cur.execute(
        "SELECT count(*) FROM edges WHERE space_id = %s AND type = 'same_topic' "
        "AND src_id = %s AND dst_id = %s", (write_space, member_id, str(third)),
    )
    assert cur.fetchone()[0] == 1
    cur.execute(
        "SELECT count(*) FROM edges WHERE space_id = %s AND type = 'supersedes' "
        "AND src_id = %s AND dst_id = %s", (write_space, member_id, str(old_id)),
    )
    assert cur.fetchone()[0] == 1


async def test_supersede_refuses_merge_band_third_node_non_novel(pool, write_space, conn):
    """The narrowed fail-closed case: a replacement that bands >= t_high against a
    third node but is NON-novel absorbs (no replacement row), so the supersede has
    nothing to flip old against and rolls the whole call back (the guard's absorb
    sub-case, QUESTIONS.md 'supersede-nonclean-band')."""
    old_id = _seed_node(
        conn, write_space, "widget", "Old node", "Old body.", {}, _unit_vector_at_angle(math.pi / 2)
    )
    # third node whose body the replacement restates verbatim -> non-novel -> absorb.
    _seed_node(
        conn, write_space, "widget", "Twin of the replacement", "Twin body.", {},
        _unit_vector_at_angle(0),
    )

    with pytest.raises(SupersedeUnresolvedBandError):
        await supersede(
            pool, write_space, "p1", str(old_id), "widget", "scope1",
            "New node", "Twin body.", {}, _unit_vector_at_angle(0), "pytest",
        )
    _assert_supersede_left_zero_state(conn, write_space, old_id, seeded_node_count=2)


async def test_supersede_refuses_pending_band_third_node_collision(pool, write_space, conn):
    """The PENDING-band case is the sharper one: the pipeline INSERTs a
    pending_writes row before the guard fires, and the refusal must roll THAT
    back too -- otherwise a parked write would outlive a supersede the caller
    never got."""
    old_id = _seed_node(
        conn, write_space, "widget", "Old node", "Old body.", {}, _unit_vector_at_angle(math.pi / 2)
    )
    # third node in the confirm band against the replacement (e1)
    _seed_node(
        conn, write_space, "widget", "Near neighbour", "Near body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)),
    )

    with pytest.raises(SupersedeUnresolvedBandError):
        await supersede(
            pool, write_space, "p1", str(old_id), "widget", "scope1",
            "New node", "New body.", {}, _unit_vector_at_angle(0), "pytest",
        )
    _assert_supersede_left_zero_state(conn, write_space, old_id, seeded_node_count=2)


async def test_supersede_without_matching_edge_rule_raises_edge_rule_violation(pool, write_space, conn):
    """A pack may legitimately decline a supersession: this write space declares
    supersedes for 'widget' only, modelling the example pack's
    decision/pattern-only rules. E0's edges_validate trigger is what refuses it
    (E2's tool layer renders that as ENGRAPHY_EDGE_RULE); the old node survives."""
    old_id = _seed_node(
        conn, write_space, "error", "Old error", "Old error body.", {}, _unit_vector_at_angle(0)
    )

    with pytest.raises(psycopg.errors.CheckViolation, match="edge_rules"):
        await supersede(
            pool, write_space, "p1", str(old_id), "error", "scope1",
            "New error", "New error body.", {},
            _unit_vector_at_angle(math.acos(bv.PENDING_NEAR_MERGE)), "pytest",
        )

    cur = conn.cursor()
    cur.execute("SELECT status FROM nodes WHERE id = %s", (old_id,))
    assert cur.fetchone()[0] == "active"


async def test_supersede_replacement_scope_unwritable_raises_scope_unknown(pool, write_space, conn):
    """FIX 2 (QUESTIONS.md "write-scope-writable-precheck", mirrored into
    supersede()): the OLD node's scope is already writable-checked above
    (old_scope), but scope_id -- where the REPLACEMENT lands -- is a separate,
    caller-supplied argument. A scope that's readable (a teammate's private
    scope, p1 granted read-only) but not writable must collapse into the same
    ENGRAPHY_SCOPE_UNKNOWN write() gives, not a raw RLS error, and the old node
    must survive untouched -- same not-found collapse, same atomicity guarantee."""
    old_id = _seed_node(
        conn, write_space, "widget", "Old node", "Old body.", {}, _unit_vector_at_angle(0)
    )
    cur = conn.cursor()
    cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p2', 'P2')", (write_space,))
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, 'p2-private', 'P2 Private', 'p2', 'private')",
        (write_space,),
    )
    cur.execute(
        "INSERT INTO scope_grants (space_id, scope_id, principal, level) VALUES (%s, 'p2-private', 'p1', 'read')",
        (write_space,),
    )
    conn.commit()

    with pytest.raises(
        ScopeUnknownError,
        match=r"ENGRAPHY_SCOPE_UNKNOWN: scope 'p2-private' does not exist or is not writable",
    ):
        await supersede(
            pool, write_space, "p1", str(old_id), "widget", "p2-private",
            "New node", "New body.", {}, _unit_vector_at_angle(math.acos(bv.PENDING_NEAR_MERGE)), "pytest",
        )

    cur.execute("SELECT status FROM nodes WHERE id = %s", (old_id,))
    assert cur.fetchone()[0] == "active", "a rejected supersede leaves the old node untouched"
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s AND title = 'New node'", (write_space,))
    assert cur.fetchone()[0] == 0, "a rejected supersede writes no replacement"


# ---- import mode (plan step 8, trap 6) --------------------------------
# "same pipeline, two flags: PENDING -> review_queue CSV row instead of parking;
# MERGE silent. No third code path -- the flags gate steps 6-PENDING and the
# envelope only." The load-bearing assertion is that everything OUTSIDE those
# two gates (band arithmetic, dedup_log, audit_log, merge mechanics) is
# unchanged -- that is what "no third code path" means in practice.


async def test_import_mode_pending_goes_to_review_queue_not_parked(pool, write_space, conn):
    candidate_id = _seed_node(
        conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0)
    )

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Similar-ish body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",  # confirm band
        import_mode=True,
    )

    # 02 §Bulk import names the report's columns: incoming, candidate, similarity.
    assert result["outcome"] == "review_queued"
    assert result["incoming"] == {"title": "Similar-ish", "body": "Similar-ish body."}
    assert result["candidate"]["id"] == str(candidate_id)
    assert result["similarity"] == bv.PENDING

    cur = conn.cursor()
    # the gate: NOT parked, and no node row either (it is still the PENDING band).
    cur.execute("SELECT count(*) FROM pending_writes WHERE space_id = %s", (write_space,))
    assert cur.fetchone()[0] == 0, "import mode must not park for interactive resolution"
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s AND title = 'Similar-ish'", (write_space,))
    assert cur.fetchone()[0] == 0, "a PENDING-band import item still creates no node"

    # ...but everything outside the gate is untouched: the encounter is still
    # logged, still as band 'pending' ("a dedup_log row for every encounter, all
    # bands" -- the threshold-tuning dataset does not get holes punched in it
    # just because the caller was an importer).
    cur.execute(
        "SELECT band, similarity, node_id, candidate_id FROM dedup_log WHERE space_id = %s",
        (write_space,),
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    band, similarity, node_id, logged_candidate = rows[0]
    assert band == "pending"
    assert round(similarity, 2) == bv.PENDING
    assert node_id is None
    assert str(logged_candidate) == str(candidate_id)


async def test_import_mode_merge_is_silent_but_absorbs_identically(pool, write_space, conn):
    """"AUTO-MERGE absorbs silently" suppresses the REPORT, never the merge.
    A NON-novel restatement absorbs (provenance-only, no addendum for a non-error
    type); the dedup_log row and the audit_log row must be byte-identical to a
    non-import merge -- a "silent" merge that skipped them would be silent data
    loss, not silence. (A NOVEL twin merge-links instead --
    test_import_mode_merge_link_is_silent.)"""
    canonical_id = _seed_node(
        conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0)
    )

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Twin", "Existing body.", {},  # non-novel restatement -> absorb
        _unit_vector_at_angle(0), "pytest",  # similarity 1.0 -> MERGE band
        import_mode=True,
    )

    # silent: no explicit merge report. `addendum_added` rides along from
    # 2026-07-21 -- internal plumbing for run_import's dropped-addendum count,
    # not wire (the report the caller would surface is still gone). False here:
    # a non-novel non-error restatement records no addendum.
    assert result == {"v": 1, "outcome": "merged", "addendum_added": False}

    cur = conn.cursor()
    # the absorption itself happened: no new row, no addendum, the canonical
    # unchanged.
    cur.execute("SELECT attrs FROM nodes WHERE id = %s", (canonical_id,))
    assert cur.fetchone()[0].get("addenda", []) == [], "non-novel absorb records no addendum"

    cur.execute("SELECT band, node_id FROM dedup_log WHERE space_id = %s", (write_space,))
    band, node_id = cur.fetchone()
    assert band == "merge"
    assert str(node_id) == str(canonical_id), "the encounter is logged like any other merge"

    cur.execute("SELECT count(*) FROM audit_log WHERE space_id = %s", (write_space,))
    assert cur.fetchone()[0] == 1, "a silent merge is still audited"

    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s", (write_space,))
    assert cur.fetchone()[0] == 1, "no second row -- the import absorbed it"


async def test_import_mode_insert_is_identical_to_a_normal_write(pool, write_space, conn):
    """The flags gate 6-PENDING and the envelope only, so an import that hits
    the INSERT band is indistinguishable from a normal write."""
    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "A brand new title", "A brand new body.", {},
        _unit_vector_at_angle(0), "pytest", import_mode=True,
    )
    assert result["outcome"] == "inserted"
    assert result["node"]["title"] == "A brand new title"
    assert result["node"]["status"] == "active"
    # Q2: import_mode skips the resonance report entirely -- the key itself
    # is absent, not an empty list (an import-mode INSERT would otherwise be
    # its own best resonance hit at similarity 1.0, a report nobody wants).
    assert "resonance" not in result

    cur = conn.cursor()
    cur.execute("SELECT band, node_id FROM dedup_log WHERE space_id = %s", (write_space,))
    band, node_id = cur.fetchone()
    assert band == "insert"
    assert str(node_id) == result["node"]["id"]


async def test_import_rerun_is_a_no_op(pool, write_space, conn):
    """02 §Bulk import: "re-running the same import is a no-op (everything hits
    >= 0.95)" -- the property that makes backfill safe. Full idempotency over a
    real file belongs to the import module's own tests; this asserts the
    pipeline-level guarantee those rest on."""
    first = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Imported item", "Imported item body.", {},
        _unit_vector_at_angle(0), "pytest", import_mode=True,
    )
    assert first["outcome"] == "inserted"

    second = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Imported item", "Imported item body.", {},
        _unit_vector_at_angle(0), "pytest", import_mode=True,
    )
    # A byte-identical re-import appends nothing, which is exactly the case
    # ImportSummary.merged_addendum_dropped counts.
    assert second == {"v": 1, "outcome": "merged", "addendum_added": False}

    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s", (write_space,))
    assert cur.fetchone()[0] == 1, "re-import creates zero rows"


# ---- resonance envelope (plan step 9) --------------------------------
# 07 §Exact formulas: "After a non-PENDING write: top-3 nodes by similarity
# >= 0.75 (config resonance.floor), any type, writer-readable scopes,
# status='active', excluding the node just written/merged-into."


async def test_resonance_excludes_the_node_just_written(pool, write_space, conn):
    """Trap 5, "resonance self-hit". The report runs after COMMIT, so the row
    just written is committed and would be its own top hit at similarity 1.0."""
    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "A brand new title", "A brand new body.", {},
        _unit_vector_at_angle(0), "pytest",
    )
    assert result["outcome"] == "inserted"
    assert result["resonance"] == [], "the only active node is the one just written"


async def test_resonance_excludes_the_canonical_just_merged_into(pool, write_space, conn):
    _seed_node(conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0))

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Twin", "Existing body.", {},  # non-novel restatement -> absorbs into canonical
        _unit_vector_at_angle(0), "pytest",  # 1.0 -> MERGE
    )
    assert result["outcome"] == "merged"
    assert result["resonance"] == [], "the canonical merged into is excluded"


async def test_resonance_is_any_type_and_respects_the_floor(pool, write_space, conn):
    """Widened vs the dedup candidate query: ANY type, not just same-type.
    And the floor is a floor: a node below it is not a resonance."""
    error_id = _seed_node(
        conn, write_space, "error", "A different type", "Other body.", {},
        _unit_vector_at_angle(math.acos(bv.RESONATES)),
    )
    _seed_node(
        conn, write_space, "widget", "Too far away", "Far body.", {},
        _unit_vector_at_angle(math.acos(bv.BELOW_RESONANCE)),
    )

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "A brand new title", "A brand new body.", {},
        _unit_vector_at_angle(0), "pytest",
    )
    assert result["outcome"] == "inserted"
    ids = [r["id"] for r in result["resonance"]]
    assert ids == [str(error_id)], "any-type hit above the floor; sub-floor node excluded"
    entry = result["resonance"][0]
    assert entry["type"] == "error"
    assert entry["scope"] == "scope1"
    assert entry["title"] == "A different type"
    assert entry["similarity"] == bv.RESONATES
    assert entry["links"] == []


async def test_resonance_floor_boundary_is_inclusive(pool, write_space, conn):
    """The comparison is ">=", so the floor value itself resonates."""
    at_floor = _seed_node(
        conn, write_space, "widget", "Exactly at the floor", "Floor body.", {},
        _unit_vector_at_angle(math.acos(bv.AT_RESONANCE_FLOOR)),
    )
    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "A brand new title", "A brand new body.", {},
        _unit_vector_at_angle(0), "pytest",
    )
    assert [r["id"] for r in result["resonance"]] == [str(at_floor)]


async def test_resonance_caps_at_top_3_ordered_by_similarity(pool, write_space, conn):
    seeded = []
    for i, sim in enumerate([0.99, 0.97, 0.96, 0.94, 0.93]):
        seeded.append((sim, _seed_node(
            conn, write_space, "error", "Resonant node " + str(i), "Body " + str(i) + ".", {},
            _unit_vector_at_angle(math.acos(sim)),
        )))

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "A brand new title", "A brand new body.", {},
        _unit_vector_at_angle(0), "pytest",
    )
    # top-3, most similar first (the seeded 'error' nodes never band against a
    # 'widget' write -- the candidate query is same-type, resonance is any-type).
    assert result["outcome"] == "inserted"
    assert [r["id"] for r in result["resonance"]] == [str(n) for _, n in seeded[:3]]
    assert [r["similarity"] for r in result["resonance"]] == [0.99, 0.97, 0.96]


async def test_resonance_carries_one_hop_link_summaries(pool, write_space, conn):
    # 'error'-typed so the resonant node resonates against a 'widget' write
    # without ever entering its same-type candidate query (a similarity this
    # high would otherwise be a dedup band, and a parked write gets no report).
    resonant = _seed_node(
        conn, write_space, "error", "Resonant node", "Resonant body.", {},
        _unit_vector_at_angle(math.acos(bv.RESONATES)),
    )
    # peers sit orthogonal (similarity 0), so they are links, not resonances.
    peer_out = _seed_node(
        conn, write_space, "error", "Peer out", "Peer out body.", {}, _unit_vector_at_angle(math.pi / 2)
    )
    peer_in = _seed_node(
        conn, write_space, "error", "Peer in", "Peer in body.", {}, _unit_vector_at_angle(math.pi / 2)
    )
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, 'relates_to')",
        (write_space, resonant, peer_out),
    )
    cur.execute(
        "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, 'relates_to')",
        (write_space, peer_in, resonant),
    )
    conn.commit()

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "A brand new title", "A brand new body.", {},
        _unit_vector_at_angle(0), "pytest",
    )
    (entry,) = [r for r in result["resonance"] if r["id"] == str(resonant)]
    # direction is relative to the resonant node: 'out' = it is the edge's src.
    assert entry["links"] == [
        {"type": "relates_to", "direction": "in", "peer_id": str(peer_in), "peer_title": "Peer in"},
        {"type": "relates_to", "direction": "out", "peer_id": str(peer_out), "peer_title": "Peer out"},
    ]


async def test_resonance_absent_from_pending_and_review_queue_envelopes(pool, write_space, conn):
    """"After a NON-PENDING write" -- a parked write created no node, so there
    is nothing to report resonance against."""
    _seed_node(conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0))
    parked = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Similar-ish body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",
    )
    assert parked["outcome"] == "needs_confirmation"
    assert "resonance" not in parked

    queued = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish two", "Similar-ish body two.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest", import_mode=True,
    )
    assert queued["outcome"] == "review_queued"
    assert "resonance" not in queued


async def test_resonance_never_leaks_an_unreadable_scope(pool, write_space, conn):
    """06: the readability bound is what makes a report unable to leak a
    teammate's private memory. A node in a scope p1 cannot read must never
    surface as a resonance, however similar it is."""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p2', 'P2')", (write_space,)
    )
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, 'p2private', 'S2', 'p2', 'private')",
        (write_space,),
    )
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
        "embedding_model, source_client, author_principal) "
        "VALUES (%s, 'widget', 'p2private', 'P2 secret', 'P2 secret body.', %s, %s::vector, "
        "'test-model', 'pytest', 'p2')",
        (write_space, Jsonb({}), "[" + ",".join(str(x) for x in _unit_vector_at_angle(0)) + "]"),
    )
    conn.commit()

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "A brand new title", "A brand new body.", {},
        _unit_vector_at_angle(0), "pytest",
    )
    # identical embedding (similarity 1.0) in p2's private scope -> still invisible.
    assert result["resonance"] == []


async def test_resonance_attaches_to_supersede(pool, write_space, conn):
    """supersede returns a write envelope, and its outcome is a non-PENDING
    write -- so it carries a report like any other. The node it replaced is
    flipped to 'superseded' and therefore drops out of the report's
    status='active' filter, without needing the self-exclusion."""
    resonant = _seed_node(
        conn, write_space, "error", "Resonant other type", "Resonant body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING_NEAR_MERGE)),
    )
    old_id = _seed_node(
        conn, write_space, "widget", "Old node", "Old body.", {}, _unit_vector_at_angle(0)
    )

    superseded = await supersede(
        pool, write_space, "p1", str(old_id), "widget", "scope1",
        "New node", "New body.", {}, _unit_vector_at_angle(math.acos(bv.PENDING_NEAR_MERGE)), "pytest",
    )
    assert superseded["outcome"] == "inserted"
    # the 'error' node sits at the replacement's own angle -> similarity 1.0.
    assert [r["id"] for r in superseded["resonance"]] == [str(resonant)]
    assert str(old_id) not in [r["id"] for r in superseded["resonance"]]


async def test_resonance_attaches_to_resolve_duplicate(pool, write_space, conn):
    """07: resolve_duplicate "returns the `write` envelope of the final
    outcome" -- both of its outcomes are non-PENDING writes."""
    candidate_id = _seed_node(
        conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0)
    )
    resonant = _seed_node(
        conn, write_space, "error", "Resonant other type", "Resonant body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)),
    )

    parked = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Similar-ish body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",  # vs the candidate -> PENDING
    )
    assert parked["outcome"] == "needs_confirmation"

    resolved = await resolve_duplicate(pool, write_space, "p1", parked["pending_id"], "distinct")
    assert resolved["outcome"] == "inserted"
    # the 'error' node at the parked payload's own angle (1.0), then the widget
    # candidate it was parked against -- both over the resonance floor, and
    # the newly inserted node itself excluded.
    assert [r["id"] for r in resolved["resonance"]] == [str(resonant), str(candidate_id)]
    assert [r["similarity"] for r in resolved["resonance"]] == [1.0, bv.PENDING]


# ---- per-space config reads (QUESTIONS.md per-space-config, 2026-07-16) ------
# 07 names dedup.t_high/t_low and resonance.floor as per-space `config` keys.
# Read once per write inside the transaction, no cache; precedence is
# caller-param > config row > code default; a malformed config value fails loud.


def _set_config(conn, space_id, key, value):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO config (space_id, key, value) VALUES (%s, %s, %s) "
        "ON CONFLICT (space_id, key) DO UPDATE SET value = excluded.value",
        (space_id, key, Jsonb(value)),
    )
    conn.commit()


async def test_config_lowers_t_high_so_a_pending_write_merges(pool, write_space, conn):
    """A confirm-band write is PENDING under the profile's default t_high, but
    MERGE once the space configures a lower one -- proving the config row is read
    and applied, with no caching (the row was set after bootstrap)."""
    _seed_node(conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0))
    _set_config(conn, write_space, "dedup.t_high", bv.CONFIG_T_HIGH_BELOW_PENDING)

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Existing body.", {},  # non-novel: absorbs, so the outcome
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",  # is 'merged', proving the band
    )
    assert result["outcome"] == "merged"


async def test_caller_thresholds_win_over_config(pool, write_space, conn):
    """Precedence: an explicit caller BandThresholds outranks the config row."""
    _seed_node(conn, write_space, "widget", "Existing node", "Existing body.", {}, _unit_vector_at_angle(0))
    _set_config(conn, write_space, "dedup.t_high", bv.CONFIG_T_HIGH_BELOW_PENDING)

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Similar-ish", "Similar-ish body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",
        # The profile's own calibrated pair, which puts a confirm-band
        # similarity back in the confirm band over the top of the config row.
        thresholds=BandThresholds.for_profile(),
    )
    assert result["outcome"] == "needs_confirmation"


async def test_config_resonance_floor_is_read(pool, write_space, conn):
    """A node that resonates under the profile's default floor stops resonating
    once the space configures a floor above it."""
    _seed_node(
        conn, write_space, "error", "A resonant node", "Resonant body.", {},
        _unit_vector_at_angle(math.acos(bv.RESONATES)),
    )
    _set_config(conn, write_space, "resonance.floor", bv.ABOVE_RESONATES)

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "A brand new title", "A brand new body.", {},
        _unit_vector_at_angle(0), "pytest",
    )
    assert result["outcome"] == "inserted"
    assert result["resonance"] == [], "0.90 < the configured 0.95 floor"


async def test_malformed_config_value_fails_loud(pool, write_space, conn):
    """config is not caller input: a non-numeric threshold fails the write with
    an ENGRAPHY_INTERNAL-class error, never a silent revert to defaults."""
    _set_config(conn, write_space, "dedup.t_high", "not a number")

    with pytest.raises(ConfigError, match="ENGRAPHY_INTERNAL"):
        await write(
            pool, write_space, "p1", "widget", "scope1",
            "A brand new title", "A brand new body.", {},
            _unit_vector_at_angle(0), "pytest",
        )


async def test_incoherent_config_band_ordering_fails_loud(pool, write_space, conn):
    """A partial config that inverts the bands (t_high below t_low) is caught by
    the 0 < t_low <= t_high <= 1 check, not silently used."""
    _set_config(conn, write_space, "dedup.t_high", 0.5)  # t_low stays the 0.80 default

    with pytest.raises(ConfigError, match="ENGRAPHY_INTERNAL"):
        await write(
            pool, write_space, "p1", "widget", "scope1",
            "A brand new title", "A brand new body.", {},
            _unit_vector_at_angle(0), "pytest",
        )


# ---- crash / atomicity (plan §Test plan "Crash test", Fable variant) --------
# The plan asked for "kill -9 between step 6 and COMMIT via a test hook". Fable
# accepted the forced-failure alternative, strengthened with one connection-kill
# variant: pg_terminate_backend from a second connection injected at the
# between-branch-and-commit seam exercises the SAME crash semantics as kill -9
# (Postgres cannot tell a SIGKILLed client from a dropped socket) without
# subprocess ceremony -- which also matters because the dev box is Windows.
# Required assertion: after the failure, zero rows across all five write-path
# tables for the attempted write.


def _assert_zero_write_path_state(conn, space_id):
    cur = conn.cursor()
    for table in ("nodes", "edges", "pending_writes", "dedup_log", "audit_log"):
        cur.execute(f"SELECT count(*) FROM {table} WHERE space_id = %s", (space_id,))
        assert cur.fetchone()[0] == 0, f"crash left a {table} row -- the write was not atomic"


async def test_crash_forced_failure_between_branch_and_commit_leaves_zero_state(pool, write_space, conn):
    """The node, dedup_log and audit_log rows are all written before the seam;
    a raise there rolls the whole transaction back as one unit."""
    async def boom(_cur):
        raise RuntimeError("injected crash between branch and COMMIT")

    with pytest.raises(RuntimeError, match="injected crash"):
        await write(
            pool, write_space, "p1", "widget", "scope1",
            "A brand new title", "A brand new body.", {},
            _unit_vector_at_angle(0), "pytest", _between_branch_and_commit=boom,
        )
    _assert_zero_write_path_state(conn, write_space)


async def test_crash_backend_terminated_between_branch_and_commit_leaves_zero_state(pool, write_space, conn):
    """The kill -9 stand-in: a second (superuser) connection terminates the
    write's backend mid-transaction. The COMMIT then cannot happen, so the
    server discards every uncommitted row -- the same guarantee kill -9 tests,
    reached through the drop-socket path Postgres treats identically."""
    async def kill(cur):
        await cur.execute("SELECT pg_backend_pid()")
        (pid,) = await cur.fetchone()
        killer = psycopg.connect(DATABASE_URL, autocommit=True)
        try:
            killer.cursor().execute("SELECT pg_terminate_backend(%s)", (pid,))
        finally:
            killer.close()

    with pytest.raises(psycopg.Error):
        await write(
            pool, write_space, "p1", "widget", "scope1",
            "A brand new title", "A brand new body.", {},
            _unit_vector_at_angle(0), "pytest", _between_branch_and_commit=kill,
        )
    _assert_zero_write_path_state(conn, write_space)


# ---- links (plan step 6 / links-wire-shape, Fable option c) -----------------
# write.links item = {type, src_id?|dst_id?}, exactly one endpoint, the omitted
# one being the node written. Error classes (advisor's split): malformed shape
# -> ValidationError (pre-transaction); unknown/unreadable endpoint ->
# NotFoundError; readable-but-no-rule -> the edges_validate trigger's
# CheckViolation.


def _edge_count(conn, space_id, src_id, dst_id, edge_type="relates_to"):
    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FROM edges WHERE space_id = %s AND src_id = %s AND dst_id = %s AND type = %s",
        (space_id, str(src_id), str(dst_id), edge_type),
    )
    return cur.fetchone()[0]


async def test_insert_attaches_link_with_dst_id(pool, write_space, conn):
    """dst_id present -> edge (new_node -> peer)."""
    peer = _seed_node(conn, write_space, "widget", "Peer", "Peer body.", {}, _unit_vector_at_angle(math.pi / 2))
    result = await write(
        pool, write_space, "p1", "widget", "scope1", "A brand new title", "A brand new body.", {},
        _unit_vector_at_angle(0), "pytest",
        links=[{"type": "relates_to", "dst_id": str(peer)}],
    )
    assert result["outcome"] == "inserted"
    assert _edge_count(conn, write_space, result["node"]["id"], peer) == 1


async def test_insert_attaches_link_with_src_id(pool, write_space, conn):
    """src_id present -> edge (peer -> new_node): the opposite direction, which
    the explicit-endpoint shape makes unambiguous."""
    peer = _seed_node(conn, write_space, "widget", "Peer", "Peer body.", {}, _unit_vector_at_angle(math.pi / 2))
    result = await write(
        pool, write_space, "p1", "widget", "scope1", "A brand new title", "A brand new body.", {},
        _unit_vector_at_angle(0), "pytest",
        links=[{"type": "relates_to", "src_id": str(peer)}],
    )
    assert _edge_count(conn, write_space, peer, result["node"]["id"]) == 1


async def test_merge_attaches_links_with_counts(pool, write_space, conn):
    """MERGE attaches to the canonical, ON CONFLICT DO NOTHING, and reports real
    attached vs skipped counts (plan step 6-MERGE item 3)."""
    canonical = _seed_node(conn, write_space, "widget", "Canonical", "Canonical body.", {}, _unit_vector_at_angle(0))
    peer1 = _seed_node(conn, write_space, "widget", "Peer one", "Peer one body.", {}, _unit_vector_at_angle(math.pi / 2))
    peer2 = _seed_node(conn, write_space, "widget", "Peer two", "Peer two body.", {}, _unit_vector_at_angle(math.pi / 2))
    # canonical already links to peer1 -> that link is a skip; peer2 is a new attach.
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, 'relates_to')",
        (write_space, str(canonical), str(peer1)),
    )
    conn.commit()

    result = await write(
        pool, write_space, "p1", "widget", "scope1", "Twin", "Canonical body.", {},
        _unit_vector_at_angle(0), "pytest",  # 1.0 + non-novel body -> absorb into canonical
        links=[{"type": "relates_to", "dst_id": str(peer1)}, {"type": "relates_to", "dst_id": str(peer2)}],
    )
    assert result["outcome"] == "merged"
    assert result["links_attached"] == 1
    assert result["links_skipped"] == 1
    assert _edge_count(conn, write_space, canonical, peer2) == 1


async def test_import_silent_merge_still_attaches_links(pool, write_space, conn):
    """"Silent" is report-only: a silent import merge drops the counts from the
    envelope but the edge is still attached."""
    canonical = _seed_node(conn, write_space, "widget", "Canonical", "Canonical body.", {}, _unit_vector_at_angle(0))
    peer = _seed_node(conn, write_space, "widget", "Peer", "Peer body.", {}, _unit_vector_at_angle(math.pi / 2))

    result = await write(
        pool, write_space, "p1", "widget", "scope1", "Twin", "Canonical body.", {},
        _unit_vector_at_angle(0), "pytest", import_mode=True,
        links=[{"type": "relates_to", "dst_id": str(peer)}],
    )
    # bare: no link counts, no resonance -- only the absorb verdict. ("Canonical
    # body." is a non-novel restatement of the canonical, so it absorbs with no
    # addendum for a non-error type; the point here is the counts' absence.)
    assert result == {"v": 1, "outcome": "merged", "addendum_added": False}
    assert _edge_count(conn, write_space, canonical, peer) == 1, "the attach still happened"


async def test_import_pending_review_queue_drops_links(pool, write_space, conn):
    """An import PENDING item goes to the review queue with nothing parked, so
    its links are dropped: no edge, no pending_writes row to hold them."""
    _seed_node(conn, write_space, "widget", "Existing", "Existing body.", {}, _unit_vector_at_angle(0))
    peer = _seed_node(conn, write_space, "widget", "Peer", "Peer body.", {}, _unit_vector_at_angle(math.pi / 2))

    result = await write(
        pool, write_space, "p1", "widget", "scope1", "Similar-ish", "Similar-ish body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest", import_mode=True,
        links=[{"type": "relates_to", "dst_id": str(peer)}],
    )
    assert result["outcome"] == "review_queued"
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM edges WHERE space_id = %s", (write_space,))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT count(*) FROM pending_writes WHERE space_id = %s", (write_space,))
    assert cur.fetchone()[0] == 0


async def test_pending_parks_links_and_distinct_applies_them(pool, write_space, conn):
    """Trap 8: links parked with the payload, applied and re-rule-checked when
    resolve_duplicate(distinct) inserts the node."""
    _seed_node(conn, write_space, "widget", "Existing", "Existing body.", {}, _unit_vector_at_angle(0))
    peer = _seed_node(conn, write_space, "widget", "Peer", "Peer body.", {}, _unit_vector_at_angle(math.pi / 2))

    parked = await write(
        pool, write_space, "p1", "widget", "scope1", "Similar-ish", "Similar-ish body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",
        links=[{"type": "relates_to", "dst_id": str(peer)}],
    )
    assert parked["outcome"] == "needs_confirmation"
    # links were parked, not applied yet.
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM edges WHERE space_id = %s", (write_space,))
    assert cur.fetchone()[0] == 0

    resolved = await resolve_duplicate(pool, write_space, "p1", parked["pending_id"], "distinct")
    assert resolved["outcome"] == "inserted"
    assert _edge_count(conn, write_space, resolved["node"]["id"], peer) == 1, "parked link applied on resolution"


async def test_resolve_duplicate_merge_attaches_parked_links(pool, write_space, conn):
    """resolve_duplicate(merge) attaches the parked links to the canonical."""
    candidate = _seed_node(conn, write_space, "widget", "Candidate", "Candidate body.", {}, _unit_vector_at_angle(0))
    peer = _seed_node(conn, write_space, "widget", "Peer", "Peer body.", {}, _unit_vector_at_angle(math.pi / 2))

    parked = await write(
        pool, write_space, "p1", "widget", "scope1", "Similar-ish", "Candidate body.", {},
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",  # non-novel -> absorbs on merge
        links=[{"type": "relates_to", "dst_id": str(peer)}],
    )
    resolved = await resolve_duplicate(
        pool, write_space, "p1", parked["pending_id"], "merge", merge_into=str(candidate)
    )
    assert resolved["outcome"] == "merged"
    assert resolved["links_attached"] == 1
    assert _edge_count(conn, write_space, candidate, peer) == 1


async def test_link_malformed_both_endpoints_raises_validation(pool, write_space, conn):
    peer = _seed_node(conn, write_space, "widget", "Peer", "Peer body.", {}, _unit_vector_at_angle(math.pi / 2))
    with pytest.raises(ValidationError, match="ENGRAPHY_VALIDATION"):
        await write(
            pool, write_space, "p1", "widget", "scope1", "T", "B", {}, _unit_vector_at_angle(0), "pytest",
            links=[{"type": "relates_to", "src_id": str(peer), "dst_id": str(peer)}],
        )
    # malformed shape is rejected before the transaction -- no node written.
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s AND title = 'T'", (write_space,))
    assert cur.fetchone()[0] == 0


async def test_link_malformed_neither_endpoint_raises_validation(pool, write_space):
    with pytest.raises(ValidationError, match="ENGRAPHY_VALIDATION"):
        await write(
            pool, write_space, "p1", "widget", "scope1", "T", "B", {}, _unit_vector_at_angle(0), "pytest",
            links=[{"type": "relates_to"}],
        )


async def test_link_missing_type_raises_validation(pool, write_space, conn):
    peer = _seed_node(conn, write_space, "widget", "Peer", "Peer body.", {}, _unit_vector_at_angle(math.pi / 2))
    with pytest.raises(ValidationError, match="ENGRAPHY_VALIDATION"):
        await write(
            pool, write_space, "p1", "widget", "scope1", "T", "B", {}, _unit_vector_at_angle(0), "pytest",
            links=[{"dst_id": str(peer)}],
        )


async def test_write_caller_supplied_attrs_addenda_raises_validation(pool, write_space, conn):
    """Q1: attrs.addenda is reserved for the merge-history append target --
    a caller-supplied value is rejected before the transaction opens, same
    as a malformed links shape."""
    with pytest.raises(ValidationError, match="ENGRAPHY_VALIDATION"):
        await write(
            pool, write_space, "p1", "widget", "scope1", "T", "B",
            {"addenda": [{"body": "spoofed merge history"}]},
            _unit_vector_at_angle(0), "pytest",
        )
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s AND title = 'T'", (write_space,))
    assert cur.fetchone()[0] == 0, "rejected before the transaction -- no node written"


async def test_link_unknown_endpoint_raises_not_found(pool, write_space):
    with pytest.raises(NotFoundError, match="ENGRAPHY_NOT_FOUND"):
        await write(
            pool, write_space, "p1", "widget", "scope1", "A brand new title", "A brand new body.", {}, _unit_vector_at_angle(0), "pytest",
            links=[{"type": "relates_to", "dst_id": "00000000-0000-0000-0000-000000000000"}],
        )


async def test_link_unreadable_endpoint_raises_not_found(pool, write_space, conn):
    """A node in another principal's private scope is unreadable, so a link to
    it collapses to NOT_FOUND (06: existence is information) -- never leaks."""
    cur = conn.cursor()
    cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p2', 'P2')", (write_space,))
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, 'p2private', 'S2', 'p2', 'private')",
        (write_space,),
    )
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
        "embedding_model, source_client, author_principal) "
        "VALUES (%s, 'widget', 'p2private', 'P2 node', 'P2 body.', %s, %s::vector, 'test-model', 'pytest', 'p2') "
        "RETURNING id",
        (write_space, Jsonb({}), "[" + ",".join(str(x) for x in _unit_vector_at_angle(math.pi / 2)) + "]"),
    )
    (p2_node,) = cur.fetchone()
    conn.commit()

    with pytest.raises(NotFoundError, match="ENGRAPHY_NOT_FOUND"):
        await write(
            pool, write_space, "p1", "widget", "scope1", "A brand new title", "A brand new body.", {}, _unit_vector_at_angle(0), "pytest",
            links=[{"type": "relates_to", "dst_id": str(p2_node)}],
        )


async def test_link_with_no_matching_rule_raises_check_violation(pool, write_space, conn):
    """A readable endpoint but an illegal (type, src, dst): relates_to has a
    widget->widget rule and an error->error rule, but no widget->error rule, so
    linking a widget write to an error node hits the edges_validate trigger."""
    error_peer = _seed_node(conn, write_space, "error", "Err peer", "Err body.", {}, _unit_vector_at_angle(math.pi / 2))
    with pytest.raises(psycopg.errors.CheckViolation, match="edge_rules"):
        await write(
            pool, write_space, "p1", "widget", "scope1", "A brand new title", "A brand new body.", {}, _unit_vector_at_angle(0), "pytest",
            links=[{"type": "relates_to", "dst_id": str(error_peer)}],
        )


async def test_link_unknown_edge_type_is_edge_rule_class_not_validation(pool, write_space, conn):
    """An UNREGISTERED edge type is a missing rule-matrix row (QUESTIONS.md
    links-wire-shape review, Fable): it must surface as the edges_validate
    trigger's CheckViolation ("edge_rules: no rule ..."), which the E2 tool layer
    maps to ENGRAPHY_EDGE_RULE -- NOT as a pre-transaction ENGRAPHY_VALIDATION. Shape
    validation deliberately does not pre-check the registry, so a well-formed
    item with an unknown type reaches the DB, where the BEFORE-INSERT trigger
    fires before the edges->edge_types FK is ever evaluated."""
    peer = _seed_node(conn, write_space, "widget", "Peer", "Peer body.", {}, _unit_vector_at_angle(math.pi / 2))
    with pytest.raises(psycopg.errors.CheckViolation, match="edge_rules"):
        await write(
            pool, write_space, "p1", "widget", "scope1", "A brand new title", "A brand new body.", {}, _unit_vector_at_angle(0), "pytest",
            links=[{"type": "no_such_edge_type", "dst_id": str(peer)}],
        )


# ---------------------------------------------------------------------------
# Regression: closed-spec node types must still be able to receive a merge
# addendum. DECISIONS-DELTA.md 2026-07-19 ("E0/E1 kernel bug found during E3
# work") + migration 0017. dedup.py's merge write is an
# `UPDATE nodes SET attrs = jsonb_set(attrs, '{addenda}', ...)`, which re-fires
# nodes_validate_attrs_fn(); before 0017 that trigger's Phase-3 closed-spec
# check had no exemption for the engine-reserved `addenda` key, so ANY node
# type declared `closed: true` raised
# `CheckViolation: attrs.addenda is not allowed (closed spec)` on its second
# dedup occurrence. Both shipped packs declare every node type `closed: true`,
# so this was production-breaking. The bug survived E1 only because
# `_bootstrap_write_space` above declares its types `closed: False` -- the
# merge path was never once exercised against a closed spec. These tests exist
# specifically to close that fixture gap, so they must keep `closed: True`.
# ---------------------------------------------------------------------------


def _bootstrap_closed_spec_space(conn, space_id):
    """Same shape as _bootstrap_write_space, but with a genuinely CLOSED attr
    spec (`closed: true` + a required enum), mirroring what both shipped packs
    actually declare."""
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, %s)", (space_id, "Closed Spec"))
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p1', 'P')", (space_id,)
    )
    closed_spec = {
        "attrs": {
            "required": {"status": {"enum": ["open", "closed"]}},
            "optional": {"note": {"type": "string"}},
            "closed": True,
        }
    }
    # A closed-spec `error` type too, so the addendum-on-closed-spec regression
    # (migration 0017) can be exercised via Phase B's only remaining addendum
    # writer -- the error-reoccurrence absorb (§2.1). `happened_at` is the field
    # a reoccurrence carries; the spec stays `closed: true`, which is the point.
    error_spec = {"attrs": {"optional": {"happened_at": {"type": "date"}}, "closed": True}}
    cur.execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) "
        "VALUES (%s, 'widget', 'w', %s), (%s, 'error', 'e', %s)",
        (space_id, Jsonb(closed_spec), space_id, Jsonb(error_spec)),
    )
    cur.execute(
        "INSERT INTO edge_types (space_id, name, description, bidirectional) VALUES "
        "(%s, 'relates_to', 'Generic association.', true)",
        (space_id,),
    )
    cur.execute(
        "INSERT INTO edge_rules (space_id, type, src_type, dst_type) VALUES "
        "(%s, 'relates_to', 'widget', 'widget')",
        (space_id,),
    )
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, 'scope1', 'S', 'p1', 'private')",
        (space_id,),
    )
    conn.commit()


@pytest.fixture
def closed_spec_space(conn, request):
    space_id = ("cs-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap_closed_spec_space(conn, space_id)
    yield space_id
    _cleanup_write_space(conn, space_id)


async def test_merge_addendum_succeeds_on_closed_spec_node_type(pool, closed_spec_space, conn):
    """The regression itself: before migration 0017 this raised
    `CheckViolation: attrs.addenda is not allowed (closed spec)`. Under Phase B
    the only path that writes an addendum is the error-reoccurrence absorb (a
    non-novel restatement of an `error` with a new `happened_at`); that path
    still exercises the jsonb_set(attrs, '{addenda}', ...) UPDATE the exemption
    protects."""
    _seed_node(
        conn, closed_spec_space, "error", "Deploy failed on the CI runner",
        "The Azure deploy failed because the backfill migration was never executed.",
        {"happened_at": "2026-01-01"}, _unit_vector_at_angle(0),
    )

    result = await write(
        pool, closed_spec_space, "p1", "error", "scope1",
        "Deploy failed on the CI runner",
        "The Azure deploy failed because the backfill migration was never executed.",
        {"happened_at": "2026-02-01"}, _unit_vector_at_angle(0), "pytest",  # reoccurrence
    )
    assert result["outcome"] == "merged"
    assert result["addendum_added"] is True
    assert "addenda" not in result["canonical"]["attrs"]

    cur = conn.cursor()
    cur.execute("SELECT attrs FROM nodes WHERE id = %s", (result["canonical"]["id"],))
    stored = cur.fetchone()[0]
    addenda = stored["addenda"]
    assert len(addenda) == 1
    assert addenda[0]["author_principal"] == "p1"
    assert addenda[0]["happened_at"] == "2026-02-01"
    # The declared attr must survive the addendum write untouched.
    assert stored["happened_at"] == "2026-01-01"


async def test_second_merge_appends_to_addenda_on_closed_spec(pool, closed_spec_space, conn):
    """The append path re-fires the trigger with a NON-empty existing addenda
    array -- a distinct trigger input from the first merge's empty one. Two error
    reoccurrences (non-novel wording, new dates) accumulate two addenda."""
    _seed_node(
        conn, closed_spec_space, "error", "Deploy failed on the CI runner",
        "The Azure deploy failed because the backfill migration was never executed.",
        {"happened_at": "2026-01-01"}, _unit_vector_at_angle(0),
    )
    for date in ("2026-02-01", "2026-03-01"):
        result = await write(
            pool, closed_spec_space, "p1", "error", "scope1",
            "Deploy failed on the CI runner",
            "The Azure deploy failed because the backfill migration was never executed.",
            {"happened_at": date}, _unit_vector_at_angle(0), "pytest",
        )
        assert result["outcome"] == "merged"

    cur = conn.cursor()
    cur.execute("SELECT attrs FROM nodes WHERE id = %s", (result["canonical"]["id"],))
    assert len(cur.fetchone()[0]["addenda"]) == 2


async def test_closed_spec_still_rejects_unknown_caller_attrs(pool, closed_spec_space, conn):
    """0017 exempts exactly `addenda` and nothing else -- an ordinary unknown
    key must still be refused, or the exemption widened the hole it patched."""
    with pytest.raises(psycopg.errors.CheckViolation, match="not allowed"):
        await write(
            pool, closed_spec_space, "p1", "widget", "scope1",
            "A brand new title", "A brand new body.",
            {"status": "open", "totally_unknown_key": "x"},
            _unit_vector_at_angle(0), "pytest",
        )


async def test_caller_supplied_addenda_still_rejected_on_closed_spec(pool, closed_spec_space, conn):
    """The DB exemption must not become a way for a caller to spoof merge
    history: the app-layer reserved-key guard still rejects it, and does so
    with ENGRAPHY_VALIDATION before the transaction opens -- not a CheckViolation."""
    with pytest.raises(ValidationError, match="reserved key"):
        await write(
            pool, closed_spec_space, "p1", "widget", "scope1",
            "A brand new title", "A brand new body.",
            {"status": "open", "addenda": [{"body": "spoofed history"}]},
            _unit_vector_at_angle(0), "pytest",
        )


# ---- The merged envelope's instruction, and the repair path it names ---------
#
# All three exist because of the dupstream contradiction finding (ruled
# 2026-07-21): 28 of 36 contradiction pairs auto-merged into the fact they
# overturn. The engine cannot detect that -- cosine is agreement-blind and an
# adjudicating LLM is forbidden -- so judgment stays the caller's and the
# envelope's job is to say what to check and name the repair verb.


async def test_merged_envelope_carries_the_static_instruction(pool, write_space, conn):
    """Always present on a merged envelope, never conditional on anything the
    engine thinks it detected -- the engine has no opinion to condition on."""
    _seed_node(conn, write_space, "widget", "Anchor", "The original body.", {},
               _unit_vector_at_angle(0))

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Restatement", "The original body.", {},  # non-novel restatement -> absorbs
        _unit_vector_at_angle(math.acos(bv.MERGE)), "pytest",
    )
    assert result["outcome"] == "merged"
    assert result["instruction"] == MERGED_INSTRUCTION
    assert "supersede" in result["instruction"]


async def test_the_instruction_is_present_even_when_the_body_was_dropped(pool, write_space, conn):
    """The case that most needs it: a near-verbatim negation trips the Jaccard
    novelty check, so `addendum_added` is False and the incoming text is gone
    entirely -- the envelope's instruction is the ONLY trace the caller gets."""
    _seed_node(conn, write_space, "widget", "Anchor", "Priya works as a paediatric nurse.", {},
               _unit_vector_at_angle(0))

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Correction", "Priya works as a paediatric nurse.", {},
        _unit_vector_at_angle(math.acos(bv.MERGE)), "pytest",
    )
    assert result["outcome"] == "merged"
    assert result["addendum_added"] is False, "precondition: this wording is judged a restatement"
    assert result["instruction"] == MERGED_INSTRUCTION


async def test_resolve_duplicate_merge_inherits_the_instruction(pool, write_space, conn):
    """07: resolve_duplicate returns the write envelope of the final outcome, so
    it carries the field verbatim rather than building its own. Asserted rather
    than assumed -- "inherits via the same function" is a claim about code that
    can stop being true."""
    anchor = _seed_node(conn, write_space, "widget", "Anchor", "The original body.", {},
                        _unit_vector_at_angle(0))
    parked = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Maybe a duplicate", "The original body.", {},  # non-novel -> merge absorbs
        _unit_vector_at_angle(math.acos(bv.PENDING)), "pytest",
    )
    assert parked["outcome"] == "needs_confirmation"

    resolved = await resolve_duplicate(
        pool, write_space, "p1", parked["pending_id"], "merge", merge_into=str(anchor),
    )
    assert resolved["outcome"] == "merged"
    assert resolved["instruction"] == MERGED_INSTRUCTION


async def test_supersede_repairs_a_contradiction_that_was_absorbed_by_auto_merge(
        pool, write_space, conn):
    """Obligation 2 of the ruling: the repair path the instruction now advertises
    must actually work, end to end, from the state auto-merge leaves behind.

    The full round trip: a correction gets absorbed into the fact it overturns
    (the canonical stays active and unchanged, which is the whole problem), then
    the caller does what the envelope told it to and supersedes the canonical
    with its version. Afterwards the correction is the active node, the stale
    fact is `superseded`, and the supersedes edge records the repair.

    This is also why supersede's self-exclusion (trap #2) is load-bearing here:
    the correction is by construction near-identical to what it replaces, so
    without old_id excluded from the candidate set the repair would itself park
    in the PENDING band instead of landing.

    Phase B note: a NOVEL correction now merge-links (a searchable member row)
    rather than being absorbed into a get-only addendum -- the dupstream leak is
    itself narrowed. This test exercises the residual case the instruction still
    exists for: a NEAR-VERBATIM update (J >= 0.8) that absorbs silently, leaving
    the stale fact active and served, until the caller supersedes it.
    """
    stale = _seed_node(conn, write_space, "widget", "Priya's job",
                       "Priya works as a paediatric nurse at Leeds General.", {},
                       _unit_vector_at_angle(0))

    # 1. The near-verbatim update is absorbed rather than stored as the current
    #    fact (J = 0.8 against the stale body: one word differs -> not novel).
    correction = "Priya works as a paediatric nurse at Leeds Central."
    absorbed = await write(
        pool, write_space, "p1", "widget", "scope1", "Priya's job", correction, {},
        _unit_vector_at_angle(math.acos(bv.MERGE)), "pytest",
    )
    assert absorbed["outcome"] == "merged"
    assert absorbed["canonical"]["id"] == str(stale)
    cur = conn.cursor()
    cur.execute("SELECT body, status FROM nodes WHERE id = %s", (stale,))
    body, status = cur.fetchone()
    assert body == "Priya works as a paediatric nurse at Leeds General.", \
        "the stale body is still what search serves"
    assert status == "active"

    # 2. The caller follows the instruction verbatim: supersede the canonical id.
    repaired = await supersede(
        pool, write_space, "p1", absorbed["canonical"]["id"], "widget", "scope1",
        "Priya's job", correction, {},
        _unit_vector_at_angle(math.acos(bv.MERGE)), "pytest",
    )
    assert repaired["outcome"] == "inserted"
    assert repaired["superseded"] == str(stale)

    # 3. The world is now correct: the correction is active, the stale fact is not.
    cur.execute("SELECT status FROM nodes WHERE id = %s", (stale,))
    assert cur.fetchone()[0] == "superseded"
    cur.execute("SELECT body, status FROM nodes WHERE id = %s", (repaired["node"]["id"],))
    new_body, new_status = cur.fetchone()
    assert new_body == correction
    assert new_status == "active"
    cur.execute(
        "SELECT count(*) FROM edges WHERE space_id = %s AND type = 'supersedes' "
        "AND src_id = %s AND dst_id = %s",
        (write_space, repaired["node"]["id"], str(stale)),
    )
    assert cur.fetchone()[0] == 1, "the repair is recorded as a supersedes edge"


async def test_import_mode_strips_the_report_but_keeps_addendum_added(pool, write_space, conn):
    """The stripped envelope is internal plumbing for the CLI importer, not wire:
    02's "silent is report-only" posture is unchanged, and no `instruction` is
    carried (import has nobody in the loop to act on one) -- but
    `addendum_added` survives so run_import can count total-loss absorptions."""
    _seed_node(conn, write_space, "widget", "Anchor", "The original body.", {},
               _unit_vector_at_angle(0))

    result = await write(
        pool, write_space, "p1", "widget", "scope1",
        "Restatement", "The original body.", {},
        _unit_vector_at_angle(math.acos(bv.MERGE)), "pytest", import_mode=True,
    )
    assert set(result) == {"v", "outcome", "addendum_added"}
    assert result["outcome"] == "merged"
    assert "instruction" not in result
    assert "resonance" not in result
