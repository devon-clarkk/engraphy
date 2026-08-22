"""engraphy.core.get — design/07 s.Canonical tool I/O `get`. Normative fixture:
fixtures/wire/get.json (byte-exact except server-minted volatile paths).

Reuses test_dedup's write_space bootstrap (types/scopes/edge_rules already
declared there) rather than duplicating it.
"""
import math

from psycopg.types.json import Jsonb

from engraphy.core.get import get
from engraphy.tests.test_dedup import _seed_node, _unit_vector_at_angle, write_space  # noqa: F401


async def test_get_found_and_missing_ids(pool, write_space, conn):
    nid = _seed_node(
        conn, write_space, "widget", "Coffee maker", "Descale monthly.", {}, _unit_vector_at_angle(0)
    )
    missing_id = "00000000-0000-4000-8000-000000000000"

    result = await get(pool, write_space, "p1", [str(nid), missing_id])

    assert result["v"] == 1
    assert len(result["nodes"]) == 1
    node = result["nodes"][0]
    assert node["id"] == str(nid)
    assert node["title"] == "Coffee maker"
    assert node["body"] == "Descale monthly."
    assert node["addenda"] == []
    assert node["edges"] == {"out": [], "in": []}
    assert result["missing"] == [missing_id]


async def test_get_unreadable_id_goes_to_missing_not_error(pool, write_space, conn):
    """06: existence is information -- an id in a different space is simply
    not found, never an error (the space_id filter in get()'s own SELECT,
    backed by RLS, makes cross-space rows invisible)."""
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES ('other-space-get', 'Other')")
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name) VALUES ('other-space-get', 'p1', 'P')"
    )
    cur.execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES "
        "('other-space-get', 'widget', 'w', %s)", (Jsonb({"attrs": {"closed": False}}),)
    )
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES ('other-space-get', 'scope1', 'S', 'p1', 'private')"
    )
    conn.commit()
    try:
        other_id = _seed_node(
            conn, "other-space-get", "widget", "Secret", "Body.", {}, _unit_vector_at_angle(0)
        )
        result = await get(pool, write_space, "p1", [str(other_id)])
        assert result["nodes"] == []
        assert result["missing"] == [str(other_id)]
    finally:
        cur.execute("DELETE FROM nodes WHERE space_id = 'other-space-get'")
        cur.execute("DELETE FROM scopes WHERE space_id = 'other-space-get'")
        cur.execute("DELETE FROM node_types WHERE space_id = 'other-space-get'")
        cur.execute("DELETE FROM principals WHERE space_id = 'other-space-get'")
        cur.execute("DELETE FROM spaces WHERE id = 'other-space-get'")
        conn.commit()


async def test_get_strips_attrs_addenda_and_surfaces_it_top_level(pool, write_space, conn):
    """Q1: no envelope, from any tool, ever ships addenda inside attrs. get
    is the ONE place merge history surfaces, as a top-level field."""
    nid = _seed_node(
        conn, write_space, "widget", "Coffee maker", "Descale monthly.",
        {"addenda": [{"body": "reworded duplicate", "author_principal": "p1"}]},
        _unit_vector_at_angle(0),
    )

    result = await get(pool, write_space, "p1", [str(nid)])

    node = result["nodes"][0]
    assert "addenda" not in node["attrs"]
    assert node["addenda"] == [{"body": "reworded duplicate", "author_principal": "p1"}]


async def test_get_edges_both_directions_capped_at_ten(pool, write_space, conn):
    center = _seed_node(
        conn, write_space, "widget", "Center", "Body.", {}, _unit_vector_at_angle(0)
    )
    cur = conn.cursor()
    out_peers, in_peers = [], []
    for i in range(12):
        peer = _seed_node(
            conn, write_space, "widget", f"Out {i}", "Body.", {},
            _unit_vector_at_angle(math.pi / 2),
        )
        cur.execute(
            "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, 'relates_to')",
            (write_space, center, peer),
        )
        out_peers.append(peer)
    for i in range(12):
        peer = _seed_node(
            conn, write_space, "widget", f"In {i}", "Body.", {},
            _unit_vector_at_angle(math.pi / 2),
        )
        cur.execute(
            "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, 'relates_to')",
            (write_space, peer, center),
        )
        in_peers.append(peer)
    conn.commit()

    result = await get(pool, write_space, "p1", [str(center)])

    node = result["nodes"][0]
    assert len(node["edges"]["out"]) == 10
    assert len(node["edges"]["in"]) == 10
    assert all(e["src"] == str(center) for e in node["edges"]["out"])
    assert all(e["dst"] == str(center) for e in node["edges"]["in"])


async def test_get_ids_clamped_to_twenty_five(pool, write_space, conn):
    ids = [f"00000000-0000-4000-8000-{i:012d}" for i in range(30)]
    result = await get(pool, write_space, "p1", ids)
    assert len(result["missing"]) == 25
