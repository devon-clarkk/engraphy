"""The verify-restore sentinel (design/04 s.Backup contract; DECISIONS-DELTA
2026-07-20 "verify-restore sentinel RESOLVED").

Two groups, deliberately separated:

- **Pure** tests (the vector's properties, `pack validate`'s reserved-name
  refusal) need no database and run anywhere.
- **Live-Postgres** tests cover the parts that only exist as SQL behavior:
  `space create` minting inside its transaction, `verify-restore` resolving the
  id from config, and the three exemptions (`doctor` drift, `pack upgrade`'s
  destructive phase, and the read paths) that stop the sentinel from either
  leaking to agents or breaking every pack upgrade.

The exemption tests matter more than they look: without them the sentinel type
is a type in the registry that no pack declares, which is exactly the shape
`doctor` reports as drift and `pack upgrade` tries to delete.
"""
import math

import pytest

from engraphy.admin import doctor, packs
from engraphy.admin.cli import _create_principal_with_personal_scope, _mint_sentinel
from engraphy.core import sentinel
from engraphy.core.dedup import ValidationError, _validate_not_reserved_type, supersede, write
from engraphy.core.embedding import DIMS
from engraphy.core.inbox import promote as core_promote

# --- Pure: the constant vector ---------------------------------------------


def test_constant_vector_is_a_unit_vector_of_the_right_width():
    """Unit norm is not cosmetic: nodes.embedding is a vector(384) compared with
    pgvector's cosine operator, and the rest of the corpus is L2-normalized
    (embedding.py re-normalizes after truncation). A non-unit sentinel would be
    the one row in the table with a different magnitude convention."""
    vec = sentinel.constant_unit_vector()
    assert len(vec) == DIMS == 384
    assert math.isclose(math.sqrt(sum(c * c for c in vec)), 1.0, rel_tol=1e-12)


def test_constant_vector_is_deterministic_across_calls():
    """The whole point: a restore is proven by the value coming back unchanged,
    which requires the expected value never to move -- not per process, not per
    machine, not per model version."""
    assert sentinel.constant_unit_vector() == sentinel.constant_unit_vector()


def test_vector_literal_round_trips_to_the_same_floats():
    """The literal is what actually reaches Postgres (space create has no
    embedding pipeline to hand it a list), so a formatting bug here would store
    a silently different vector than the one this module promises."""
    literal = sentinel.vector_literal()
    assert literal.startswith("[") and literal.endswith("]")
    parsed = [float(x) for x in literal[1:-1].split(",")]
    assert parsed == sentinel.constant_unit_vector()


def test_embedding_model_marker_is_not_the_real_model_id():
    """A model migration re-embeds rows keyed on embedding_model. If the sentinel
    carried MODEL_ID it would be swept into that re-embed and its constant vector
    -- the only thing verify-restore compares -- would be overwritten."""
    from engraphy.core.embedding import MODEL_ID

    assert sentinel.SENTINEL_EMBEDDING_MODEL != MODEL_ID


# --- Pure: pack validate refuses the reserved names -------------------------

_MINIMAL_PACK = {
    "pack": "reserved-test",
    "version": 1,
    "node_types": {"note": {"description": "A note.", "attrs": {}}},
    "edge_types": {},
    "edge_rules": [],
}


def test_pack_declaring_the_sentinel_type_is_refused():
    pack = {**_MINIMAL_PACK,
            "node_types": {**_MINIMAL_PACK["node_types"],
                           sentinel.SENTINEL_NODE_TYPE: {"description": "Mine now.", "attrs": {}}}}
    errors = packs.validate_reserved_names(pack)
    assert any(sentinel.SENTINEL_NODE_TYPE in e for e in errors)


def test_pack_declaring_a_reserved_attrs_key_is_refused():
    """`addenda` is engine-managed merge history (RESERVED_ATTR_KEYS, migration
    0017); a pack writing rules for it is describing a key the engine owns."""
    pack = {**_MINIMAL_PACK,
            "node_types": {"note": {"description": "A note.",
                                    "attrs": {"optional": {"addenda": {"type": "string"}}}}}}
    errors = packs.validate_reserved_names(pack)
    assert any("addenda" in e for e in errors)


def test_clean_pack_has_no_reserved_name_errors():
    assert packs.validate_reserved_names(_MINIMAL_PACK) == []


def test_the_pack_side_and_write_side_refusals_read_the_same_names():
    """The reason RESERVED_NODE_TYPES exists at all (ruled 2026-07-21): two
    refusals in two modules for one reservation is a drift waiting to happen.
    Anything added to the set has to be refused by both, so this asserts the
    write-path check covers the WHOLE set, not just the name it was written for."""
    for name in sentinel.RESERVED_NODE_TYPES:
        with pytest.raises(ValidationError):
            _validate_not_reserved_type(name)
        pack = {**_MINIMAL_PACK,
                "node_types": {**_MINIMAL_PACK["node_types"],
                               name: {"description": "Mine now.", "attrs": {}}}}
        assert any(name in e for e in packs.validate_reserved_names(pack))


def test_a_non_reserved_type_passes_the_write_path_check():
    _validate_not_reserved_type("widget")


def test_shipped_starter_pack_has_no_reserved_name_errors():
    """The regression that would hurt most: a reserved-name check that rejects
    our own shipped pack."""
    pack = packs.load_pack_file("packs/starter/pack.yaml")
    assert packs.validate_reserved_names(pack) == []


# --- Live Postgres ----------------------------------------------------------


def _space_with_sentinel(conn, space_id: str) -> tuple:
    """`space create`'s transaction body, minus typer: space + founding
    principal + personal scope + sentinel, exactly as the CLI sequences them."""
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, %s)", (space_id, "Sentinel Test"))
    _create_principal_with_personal_scope(cur, space_id, "p1", "P1", "space_admin")
    node_id = _mint_sentinel(cur, space_id, "p1")
    return cur, node_id


def test_space_create_mints_an_archived_sentinel_with_the_constant_vector(conn):
    cur, node_id = _space_with_sentinel(conn, "sent-mint")
    cur.execute(
        "SELECT type, scope_id, title, body, status, embedding_model, embedding "
        "FROM nodes WHERE id = %s", (node_id,))
    type_, scope_id, title, body, status, model, embedding = cur.fetchone()
    assert type_ == sentinel.SENTINEL_NODE_TYPE
    assert scope_id == "personal-p1"
    assert title == sentinel.SENTINEL_TITLE
    assert body == sentinel.SENTINEL_BODY
    assert status == "archived"          # never a search/briefing/dedup candidate
    assert model == sentinel.SENTINEL_EMBEDDING_MODEL
    # pgvector's `vector` stores float4, so the float64 constant cannot survive
    # the round trip exactly: 1/sqrt(384) comes back with ~3.6e-8 relative
    # error. 1e-6 is loose enough for single precision and still tight enough
    # to catch a genuinely wrong vector. The length assert matters too -- zip()
    # silently stops at the shorter sequence, so a truncated embedding would
    # otherwise pass.
    stored = [float(x) for x in str(embedding).strip("[]").split(",")]
    assert len(stored) == 384
    assert all(math.isclose(a, b, rel_tol=1e-6)
               for a, b in zip(stored, sentinel.constant_unit_vector()))


def test_space_create_records_the_sentinel_id_in_config(conn):
    """This key is what makes verify-restore self-locating."""
    cur, node_id = _space_with_sentinel(conn, "sent-config")
    cur.execute("SELECT value #>> '{}' FROM config WHERE space_id = %s AND key = %s",
                ("sent-config", sentinel.SENTINEL_CONFIG_KEY))
    assert cur.fetchone()[0] == node_id


def test_sentinel_type_is_registered_in_node_types(conn):
    cur, _ = _space_with_sentinel(conn, "sent-registry")
    cur.execute("SELECT attr_spec FROM node_types WHERE space_id = %s AND name = %s",
                ("sent-registry", sentinel.SENTINEL_NODE_TYPE))
    assert cur.fetchone()[0] == sentinel.SENTINEL_ATTR_SPEC


def test_sentinel_attrs_pass_the_validate_trigger(conn):
    """`{}` attrs against an empty, non-closed spec -- the mint would otherwise
    fail on the trigger, and it runs inside space create's transaction, so a
    failure here means no space can be created at all."""
    cur, node_id = _space_with_sentinel(conn, "sent-trigger")
    cur.execute("SELECT attrs FROM nodes WHERE id = %s", (node_id,))
    assert cur.fetchone()[0] == {}


# --- Live Postgres: the exemptions ------------------------------------------

_PACK = {
    "pack": "sentinel-exemption",
    "version": 1,
    "node_types": {"widget": {"description": "A widget.", "attrs": {}}},
    "edge_types": {},
    "edge_rules": [],
}


def test_doctor_does_not_report_the_sentinel_type_as_registry_drift(conn, tmp_path):
    """The sentinel is in the DB and in no pack file -- the literal shape of
    doctor's "in DB but not in pack file (hand-edited?)" finding. Reporting it
    would put an unfixable line in every healthy instance's output."""
    import yaml

    cur, _ = _space_with_sentinel(conn, "sent-doctor")
    packs.apply(_PACK, "sent-doctor", cur)
    pack_file = tmp_path / "pack.yaml"
    pack_file.write_text(yaml.safe_dump(_PACK), encoding="utf-8")

    lines = doctor._registry_drift(cur, "sent-doctor", pack_file)
    assert not any(sentinel.SENTINEL_NODE_TYPE in line for line in lines)
    assert any("drift" in line and ": 0" in line for line in lines)


def test_pack_upgrade_does_not_try_to_remove_the_sentinel_type(conn):
    """Without the exemption this raises PackUpgradeRefused on EVERY upgrade of
    EVERY space: the sentinel's node is archived, so the active-rows check
    passes and the DELETE then hits the FK from that archived row."""
    cur, _ = _space_with_sentinel(conn, "sent-upgrade")
    packs.apply(_PACK, "sent-upgrade", cur)

    report = packs.upgrade({**_PACK, "version": 2,
                            "node_types": {**_PACK["node_types"],
                                           "gadget": {"description": "A gadget.", "attrs": {}}}},
                           "sent-upgrade", cur)
    assert any("gadget" in line for line in report)
    assert not any(sentinel.SENTINEL_NODE_TYPE in line for line in report)
    cur.execute("SELECT count(*) FROM node_types WHERE space_id = %s AND name = %s",
                ("sent-upgrade", sentinel.SENTINEL_NODE_TYPE))
    assert cur.fetchone()[0] == 1, "the upgrade removed the engine's own sentinel type"


@pytest.fixture
def committed_sentinel_space(conn, request):
    """A committed (not rollback-scoped) space with a sentinel, for the async
    read-path tests: `search` runs on its own pool connection and cannot see an
    uncommitted transaction. Mirrors test_search.py's search_space fixture."""
    space_id = ("sn-" + request.node.name.replace("_", "-"))[:60]
    cur, node_id = _space_with_sentinel(conn, space_id)
    conn.commit()
    yield space_id, node_id
    for table in ("audit_log", "dedup_log", "edges", "nodes", "config",
                  "scopes", "node_types", "principals"):
        cur.execute(f"DELETE FROM {table} WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


@pytest.mark.asyncio
async def test_search_never_returns_the_sentinel_even_with_include_inactive(
        pool, committed_sentinel_space, conn):
    """status='archived' alone does NOT hide it here: `include_inactive` is an
    agent-callable search argument, so the exclusion has to be by type. The
    query is the sentinel's own title, which is the strongest possible lexical
    match -- if the type filter regressed, this test finds it immediately."""
    from engraphy.core.search import search

    space_id, node_id = committed_sentinel_space
    cur = conn.cursor()
    cur.execute("SELECT status FROM nodes WHERE id = %s", (node_id,))
    assert cur.fetchone()[0] == "archived", "precondition: the sentinel is in the DB"

    result = await search(pool, space_id, "p1", "personal-p1",
                          sentinel.SENTINEL_TITLE, "pytest", include_inactive=True)
    assert all(r["node"]["type"] != sentinel.SENTINEL_NODE_TYPE for r in result["results"])
    assert node_id not in [r["node"]["id"] for r in result["results"]]


# --- Live Postgres: the write-path refusal (ruled 2026-07-21) ---------------
#
# The premise these tests defend: `space create` REGISTERS `engraphy_sentinel` in
# node_types so the sentinel row can exist, and that registry row is exactly
# what the `nodes.(space_id, type)` FK checks -- so before this refusal, any
# caller who guessed the name got a valid insert. Every space below therefore
# has the type registered; a passing test means OUR check refused the write,
# not that the FK happened to. (design/07 §Pack file schema, Reserved names.)


def _unit_vector():
    """A 384-dim unit vector. These writes are all refused before any candidate
    query runs, so the direction carries no meaning -- only the width does."""
    vec = [0.0] * DIMS
    vec[0] = 1.0
    return vec


@pytest.fixture
def sentinel_write_space(conn, request):
    """A committed space carrying the sentinel (hence the registered type) and a
    writable scope, for the async write-path tests. Committed because write()
    runs on the pool's own connection and cannot see an uncommitted transaction."""
    space_id = ("sw-" + request.node.name.replace("_", "-"))[:60]
    cur, node_id = _space_with_sentinel(conn, space_id)
    conn.commit()
    yield space_id, node_id
    for table in ("inbox", "pending_writes", "audit_log", "dedup_log", "edges",
                  "nodes", "config", "scope_grants", "scopes", "edge_rules",
                  "edge_types", "node_types", "principals"):
        cur.execute(f"DELETE FROM {table} WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


def _node_count(conn, space_id, node_type):
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s AND type = %s",
                (space_id, node_type))
    return cur.fetchone()[0]


@pytest.mark.asyncio
async def test_write_refuses_the_reserved_sentinel_type(pool, sentinel_write_space, conn):
    """The hole this closes: the type is a legal FK target, so `write` naming it
    used to insert cleanly. Junk rows of an engine-owned type are permanent
    under no-hard-deletes, and doctor / pack upgrade / verify-restore / the
    read-path exclusions all assume rows of this type are engine-minted."""
    space_id, minted_id = sentinel_write_space
    with pytest.raises(ValidationError) as exc:
        await write(pool, space_id, "p1", sentinel.SENTINEL_NODE_TYPE, "personal-p1",
                    "Not the real sentinel", "Decoy body.", {}, _unit_vector(), "pytest")
    assert "ENGRAPHY_VALIDATION" in str(exc.value)
    assert sentinel.SENTINEL_NODE_TYPE in str(exc.value)
    assert "reserved for the engine" in str(exc.value)
    # Only the engine's own mint survives -- no decoy landed beside it.
    assert _node_count(conn, space_id, sentinel.SENTINEL_NODE_TYPE) == 1
    cur = conn.cursor()
    cur.execute("SELECT id FROM nodes WHERE space_id = %s AND type = %s",
                (space_id, sentinel.SENTINEL_NODE_TYPE))
    assert str(cur.fetchone()[0]) == minted_id


@pytest.mark.asyncio
async def test_supersede_refuses_a_reserved_type_replacement(pool, sentinel_write_space, conn):
    """supersede does not funnel through write(), so it needs its own call site
    (Fable's correction 1). The `old_id` here is deliberately a uuid that does
    not exist: getting VALIDATION rather than NOT_FOUND proves the check runs
    pre-transaction, before the old-node lookup and before the advisory lock."""
    space_id, _ = sentinel_write_space
    with pytest.raises(ValidationError) as exc:
        await supersede(pool, space_id, "p1", "00000000-0000-4000-8000-000000000000",
                        sentinel.SENTINEL_NODE_TYPE, "personal-p1",
                        "Replacement sentinel", "Decoy body.", {}, _unit_vector(), "pytest")
    assert sentinel.SENTINEL_NODE_TYPE in str(exc.value)
    assert "reserved for the engine" in str(exc.value)


@pytest.mark.asyncio
async def test_the_minted_sentinel_is_unsupersedable_through_the_tool_surface(
        pool, sentinel_write_space, conn):
    """The side door this closes for free (ruled 2026-07-21). Superseding the
    real sentinel needs a replacement of the SAME type -- cross-type supersession
    is already rejected -- and that type is now refused, so there is no argument
    combination that reaches the flip. Both halves asserted here, and the
    sentinel is still active-shaped and unflipped afterwards."""
    space_id, minted_id = sentinel_write_space

    # Same type as the target: refused by the new check.
    with pytest.raises(ValidationError):
        await supersede(pool, space_id, "p1", minted_id, sentinel.SENTINEL_NODE_TYPE,
                        "personal-p1", "Replacement sentinel", "Decoy body.", {},
                        _unit_vector(), "pytest")
    # Any other type: refused by the pre-existing cross-type rule.
    with pytest.raises(ValidationError):
        await supersede(pool, space_id, "p1", minted_id, "note", "personal-p1",
                        "Replacement note", "Decoy body.", {}, _unit_vector(), "pytest")

    cur = conn.cursor()
    cur.execute("SELECT status FROM nodes WHERE id = %s", (minted_id,))
    assert cur.fetchone()[0] == "archived", "the sentinel's status was flipped"
    cur.execute("SELECT count(*) FROM edges WHERE space_id = %s AND type = 'supersedes'",
                (space_id,))
    assert cur.fetchone()[0] == 0


@pytest.mark.asyncio
async def test_inbox_promote_refuses_the_reserved_type(pool, sentinel_write_space, conn):
    """promote inherits the refusal through write() -- no tool-layer change.
    The inbox row must survive: a refused promotion is not a consumed one."""
    from psycopg.types.json import Jsonb

    space_id, _ = sentinel_write_space
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inbox (space_id, scope_id, kind, payload, status) "
        "VALUES (%s, 'personal-p1', 'note', %s, 'pending') RETURNING id",
        (space_id, Jsonb({"t": 1})),
    )
    inbox_id = cur.fetchone()[0]
    conn.commit()

    with pytest.raises(ValidationError) as exc:
        await core_promote(pool, space_id, "p1", inbox_id, sentinel.SENTINEL_NODE_TYPE,
                           "personal-p1", "Promoted decoy", "Decoy body.", {}, "pytest",
                           embedding_vector=_unit_vector())
    assert sentinel.SENTINEL_NODE_TYPE in str(exc.value)
    assert _node_count(conn, space_id, sentinel.SENTINEL_NODE_TYPE) == 1
    cur.execute("SELECT status FROM inbox WHERE id = %s", (inbox_id,))
    assert cur.fetchone()[0] == "pending", "a refused promotion consumed the inbox row"
