"""Never destroy a node on a typed-attr validation failure. The write path
quarantines an
invalid attr into the reserved `dropped` bucket, keeps the node, and surfaces
what it set aside -- rather than the whole memory being lost, which was a primary
driver of the LoCoMo pack gap. Runs against the real DB + pinned model like the
other E1 integration tests.
"""
import pytest
from psycopg.types.json import Jsonb

from engraphy.core import embedding as _emb
from engraphy.core.dedup import BandThresholds, write
from engraphy.core.get import get
from engraphy.core.search import search

_INSERT = BandThresholds(t_high=1.1, t_low=1.05)  # sim < t_low -> always insert


def _bootstrap(conn, space_id):
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'S')", (space_id,))
    cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p1', 'P')",
                (space_id,))
    # `memo`: a closed type with a REQUIRED date and an optional string -- the
    # shape that made conversational nodes die (event.occurred_on required).
    memo_spec = {"attrs": {"required": {"occurred_on": {"type": "date"}},
                           "optional": {"note": {"type": "string"}}, "closed": True}}
    cur.execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES (%s, 'memo', 'm', %s)",
        (space_id, Jsonb(memo_spec)),
    )
    cur.execute("INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
                "VALUES (%s, 'work', 'W', 'p1', 'private')", (space_id,))
    conn.commit()


def _cleanup(conn, space_id):
    cur = conn.cursor()
    for t in ("audit_log", "dedup_log", "edges", "nodes", "scopes", "config",
              "node_types", "principals"):
        cur.execute(f"DELETE FROM {t} WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


@pytest.fixture
def memo_space(conn, request):
    space_id = ("q-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap(conn, space_id)
    yield space_id
    _cleanup(conn, space_id)


async def _w(pool, space, title, body, attrs):
    vec = _emb.embed_document(_emb.searchable_text(title, body, ""))
    return await write(pool, space, "p1", "memo", "work", title, body, attrs, vec,
                       "pytest", thresholds=_INSERT)


async def test_invalid_required_date_is_quarantined_node_survives(pool, memo_space, conn):
    """The flagship case: event.occurred_on = an unparseable prose date. Before
    the fix the whole node was rejected; now it is stored with the bad date
    quarantined and the required-presence check satisfied by the bucket."""
    space = memo_space
    r = await _w(pool, space, "Banff trip",
                 "Evan went skiing at Banff zq-banff", {"occurred_on": "last month"})
    assert r["outcome"] == "inserted", "the node was kept, not rejected"
    nid = r["node"]["id"]

    # the write surfaced what it set aside
    assert r["dropped_attrs"] == [
        {"key": "occurred_on", "value": "last month",
         "error": "attrs.occurred_on must be a date"}
    ]

    # the offending value is preserved (visible, recoverable) in the bucket, and
    # the top-level attr is gone (so the row is trigger-valid).
    cur = conn.cursor()
    cur.execute("SELECT attrs FROM nodes WHERE id = %s", (nid,))
    attrs = cur.fetchone()[0]
    assert "occurred_on" not in attrs
    assert attrs["dropped"]["occurred_on"]["value"] == "last month"

    # and the FACT is findable -- its searchable content lives in title/body,
    # never depended on the date parsing.
    res = await search(pool, space, "p1", "work", "zq-banff", "pytest")
    assert nid in {x["node"]["id"] for x in res["results"]}


async def test_partial_date_is_stored_verbatim_not_dropped(pool, memo_space, conn):
    """A year-only date is now valid (PART 2): stored as-is, never quarantined."""
    space = memo_space
    r = await _w(pool, space, "Dance win",
                 "Jon's crew won first place zq-2022", {"occurred_on": "2022"})
    assert r["outcome"] == "inserted"
    assert "dropped_attrs" not in r, "a valid partial date must not be quarantined"
    cur = conn.cursor()
    cur.execute("SELECT attrs FROM nodes WHERE id = %s", (r["node"]["id"],))
    attrs = cur.fetchone()[0]
    assert attrs == {"occurred_on": "2022"}, "partial stored verbatim, no coercion"


async def test_invalid_optional_attr_quarantined_valid_required_kept(pool, memo_space, conn):
    """A bad OPTIONAL attr is quarantined while the valid required attr is kept."""
    space = memo_space
    r = await _w(pool, space, "Note", "body zq-opt",
                 {"occurred_on": "2023-06", "note": 123})
    assert r["outcome"] == "inserted"
    assert [d["key"] for d in r["dropped_attrs"]] == ["note"]
    cur = conn.cursor()
    cur.execute("SELECT attrs FROM nodes WHERE id = %s", (r["node"]["id"],))
    attrs = cur.fetchone()[0]
    assert attrs["occurred_on"] == "2023-06"   # valid partial kept top-level
    assert "note" not in attrs
    assert attrs["dropped"]["note"]["value"] == 123


async def test_wellformed_write_is_unchanged(pool, memo_space, conn):
    """No droppable attr -> attrs untouched, no dropped_attrs, no bucket."""
    space = memo_space
    r = await _w(pool, space, "Clean", "body zq-clean",
                 {"occurred_on": "2023-06-15", "note": "hi"})
    assert r["outcome"] == "inserted"
    assert "dropped_attrs" not in r
    cur = conn.cursor()
    cur.execute("SELECT attrs FROM nodes WHERE id = %s", (r["node"]["id"],))
    assert cur.fetchone()[0] == {"occurred_on": "2023-06-15", "note": "hi"}


async def test_update_quarantines_invalid_attr_instead_of_failing(pool, memo_space, conn):
    """The amend path is a write too: update() with a bad attr value must keep
    the node (quarantine), not raise a trigger CheckViolation and lose the edit."""
    from engraphy.core.update import update
    space = memo_space
    r = await _w(pool, space, "Trip", "body zq-upd", {"occurred_on": "2023-06-15"})
    nid = r["node"]["id"]
    # amend with an unparseable date -- must not destroy the node.
    res = await update(pool, space, "p1", nid, attrs={"occurred_on": "some time last year"})
    assert res["outcome"] == "updated"
    assert [d["key"] for d in res["dropped_attrs"]] == ["occurred_on"]
    cur = conn.cursor()
    cur.execute("SELECT attrs FROM nodes WHERE id = %s", (nid,))
    attrs = cur.fetchone()[0]
    assert "occurred_on" not in attrs
    assert attrs["dropped"]["occurred_on"]["value"] == "some time last year"


async def test_update_partial_date_stored_verbatim(pool, memo_space, conn):
    """An amend to a valid partial date is kept as-is, not quarantined."""
    from engraphy.core.update import update
    space = memo_space
    r = await _w(pool, space, "Trip", "body zq-upd2", {"occurred_on": "2023-06-15"})
    nid = r["node"]["id"]
    res = await update(pool, space, "p1", nid, attrs={"occurred_on": "2024"})
    assert res["outcome"] == "updated"
    assert "dropped_attrs" not in res
    cur = conn.cursor()
    cur.execute("SELECT attrs FROM nodes WHERE id = %s", (nid,))
    assert cur.fetchone()[0] == {"occurred_on": "2024"}


async def test_get_exposes_quarantined_value(pool, memo_space):
    """A caller/UI can recover the quarantined value via get (it is in attrs)."""
    space = memo_space
    r = await _w(pool, space, "Married", "Melanie married zq-married",
                 {"occurred_on": "for 5 years"})
    got = await get(pool, space, "p1", [r["node"]["id"]])
    node = got["nodes"][0]
    assert node["attrs"]["dropped"]["occurred_on"]["value"] == "for 5 years"
