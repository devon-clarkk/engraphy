"""engraphy.core.link.link — the standalone edge-attach tool's core function.
Both endpoints are always explicit and required (unlike write's links,
which name exactly one, the other being the node being written).
"""
import psycopg
import pytest

from engraphy.core.dedup import NotFoundError, ValidationError
from engraphy.core.link import link
from engraphy.tests.test_dedup import _seed_node, _unit_vector_at_angle, write_space  # noqa: F401


async def test_link_attaches_edge_and_reports_count(pool, write_space, conn):
    a = _seed_node(conn, write_space, "widget", "Node A", "Body A.", {}, _unit_vector_at_angle(0))
    b = _seed_node(conn, write_space, "widget", "Node B", "Body B.", {}, _unit_vector_at_angle(1))

    result = await link(pool, write_space, "p1", [{"type": "relates_to", "src_id": str(a), "dst_id": str(b)}])
    assert result == {"v": 1, "attached": 1, "skipped": 0}

    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FROM edges WHERE space_id = %s AND src_id = %s AND dst_id = %s AND type = 'relates_to'",
        (write_space, a, b),
    )
    assert cur.fetchone()[0] == 1


async def test_link_already_present_edge_is_skipped_not_error(pool, write_space, conn):
    a = _seed_node(conn, write_space, "widget", "Node A", "Body A.", {}, _unit_vector_at_angle(0))
    b = _seed_node(conn, write_space, "widget", "Node B", "Body B.", {}, _unit_vector_at_angle(1))
    items = [{"type": "relates_to", "src_id": str(a), "dst_id": str(b)}]

    first = await link(pool, write_space, "p1", items)
    assert first == {"v": 1, "attached": 1, "skipped": 0}
    second = await link(pool, write_space, "p1", items)
    assert second == {"v": 1, "attached": 0, "skipped": 1}


async def test_link_multiple_items_mixed_attach_and_skip(pool, write_space, conn):
    a = _seed_node(conn, write_space, "widget", "Node A", "Body A.", {}, _unit_vector_at_angle(0))
    b = _seed_node(conn, write_space, "widget", "Node B", "Body B.", {}, _unit_vector_at_angle(1))
    c = _seed_node(conn, write_space, "widget", "Node C", "Body C.", {}, _unit_vector_at_angle(2))
    await link(pool, write_space, "p1", [{"type": "relates_to", "src_id": str(a), "dst_id": str(b)}])

    result = await link(pool, write_space, "p1", [
        {"type": "relates_to", "src_id": str(a), "dst_id": str(b)},  # already present -> skip
        {"type": "relates_to", "src_id": str(a), "dst_id": str(c)},  # new -> attach
    ])
    assert result == {"v": 1, "attached": 1, "skipped": 1}


async def test_link_unknown_endpoint_raises_not_found(pool, write_space, conn):
    a = _seed_node(conn, write_space, "widget", "Node A", "Body A.", {}, _unit_vector_at_angle(0))
    missing = "00000000-0000-4000-8000-000000000000"
    with pytest.raises(NotFoundError, match="ENGRAPHY_NOT_FOUND"):
        await link(pool, write_space, "p1", [{"type": "relates_to", "src_id": str(a), "dst_id": missing}])

    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM edges WHERE space_id = %s", (write_space,))
    assert cur.fetchone()[0] == 0, "a rejected item leaves the whole call's edges unattached"


async def test_link_missing_endpoint_raises_validation(pool, write_space, conn):
    a = _seed_node(conn, write_space, "widget", "Node A", "Body A.", {}, _unit_vector_at_angle(0))
    with pytest.raises(ValidationError, match="ENGRAPHY_VALIDATION"):
        await link(pool, write_space, "p1", [{"type": "relates_to", "src_id": str(a)}])  # no dst_id


async def test_link_unknown_field_raises_validation(pool, write_space, conn):
    a = _seed_node(conn, write_space, "widget", "Node A", "Body A.", {}, _unit_vector_at_angle(0))
    b = _seed_node(conn, write_space, "widget", "Node B", "Body B.", {}, _unit_vector_at_angle(1))
    with pytest.raises(ValidationError, match="ENGRAPHY_VALIDATION"):
        await link(pool, write_space, "p1", [
            {"type": "relates_to", "src_id": str(a), "dst_id": str(b), "extra": "nope"}
        ])


async def test_link_no_matching_edge_rule_raises_check_violation(pool, write_space, conn):
    """write_space only registers (relates_to, widget, widget) and
    (relates_to, error, error) -- a widget->error relates_to has no rule."""
    a = _seed_node(conn, write_space, "widget", "Node A", "Body A.", {}, _unit_vector_at_angle(0))
    b = _seed_node(conn, write_space, "error", "Node B", "Body B.", {}, _unit_vector_at_angle(1))
    with pytest.raises(psycopg.errors.CheckViolation):
        await link(pool, write_space, "p1", [{"type": "relates_to", "src_id": str(a), "dst_id": str(b)}])
