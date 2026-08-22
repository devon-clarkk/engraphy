"""engraphy.admin.packs.upgrade() -- design/04 s.Pack migrations: additive /
tightening / destructive change classes, diffed against the space's current
registry (not the pack that produced it -- upgrade() has no notion of "the
previous pack file", only "what's in the DB now").
"""
import pytest

from conftest import insert_node
from engraphy.admin.packs import PackUpgradeRefused, apply, current_registry, upgrade

BASE_PACK = {
    "pack": "test-pack",
    "version": 1,
    "node_types": {
        "widget": {"description": "A widget.", "attrs": {"required": {"status": {"enum": ["open", "closed"]}},
                                                           "closed": True}},
    },
    "edge_types": {
        "relates_to": {"description": "Generic association.", "bidirectional": True},
    },
    "edge_rules": [{"type": "relates_to", "src": "widget", "dst": "widget"}],
}


def _setup(conn, space_id, pack=None):
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, %s)", (space_id, "Test"))
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name) VALUES (%s, %s, %s)",
        (space_id, "p1", "P1"))
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, %s, %s, %s, %s)", (space_id, "scope1", "Scope", "p1", "private"))
    apply(pack or BASE_PACK, space_id, cur)
    return cur


def test_additive_new_node_type(conn):
    cur = _setup(conn, "up-additive-node")
    new_pack = {**BASE_PACK, "version": 2, "node_types": {
        **BASE_PACK["node_types"],
        "gadget": {"description": "A gadget.", "attrs": {"closed": False}},
    }}
    report = upgrade(new_pack, "up-additive-node", cur)
    assert any("additive: node type 'gadget' added" in line for line in report)
    node_types, _, _ = current_registry("up-additive-node", cur)
    assert "gadget" in node_types
    cur.execute("SELECT pack_version FROM spaces WHERE id = 'up-additive-node'")
    assert cur.fetchone()[0] == 2


def test_additive_new_edge_type_and_rule(conn):
    cur = _setup(conn, "up-additive-edge")
    new_pack = {**BASE_PACK, "version": 2,
                "edge_types": {**BASE_PACK["edge_types"],
                                "blocks": {"description": "Blocks.", "bidirectional": False}},
                "edge_rules": [*BASE_PACK["edge_rules"],
                                {"type": "blocks", "src": "widget", "dst": "widget"}]}
    report = upgrade(new_pack, "up-additive-edge", cur)
    assert any("additive: edge type 'blocks' added" in line for line in report)
    assert any("additive: edge rule 'blocks' widget -> widget added" in line for line in report)


def test_tightening_no_violators_applies_immediately(conn):
    cur = _setup(conn, "up-tighten-clean")
    insert_node(conn, "up-tighten-clean", "scope1", node_type="widget", attrs={"status": "open"})
    new_pack = {**BASE_PACK, "version": 2, "node_types": {
        "widget": {"description": "A widget.", "attrs": {
            "required": {"status": {"enum": ["open", "closed"]}}, "optional": {"note": {"type": "string"}},
            "closed": True}},
    }}
    report = upgrade(new_pack, "up-tighten-clean", cur)
    assert any("no existing rows affected" in line for line in report)
    node_types, _, _ = current_registry("up-tighten-clean", cur)
    assert node_types["widget"]["attrs"]["optional"] == {"note": {"type": "string"}}


def test_tightening_with_violators_refused_without_flag(conn):
    cur = _setup(conn, "up-tighten-refuse")
    node_id = insert_node(conn, "up-tighten-refuse", "scope1", node_type="widget", attrs={"status": "open"})
    new_pack = {**BASE_PACK, "version": 2, "node_types": {
        "widget": {"description": "A widget.", "attrs": {
            "required": {"status": {"enum": ["open", "closed"]}, "owner": {"type": "string"}},
            "closed": True}},
    }}
    with pytest.raises(PackUpgradeRefused) as excinfo:
        upgrade(new_pack, "up-tighten-refuse", cur)
    assert any(str(node_id) in line for line in excinfo.value.worklist)
    # nothing committed to node_types -- caller is expected to roll back, but
    # even mid-transaction the spec must be unchanged since upgrade() raises
    # before issuing the UPDATE for a refused type.
    node_types, _, _ = current_registry("up-tighten-refuse", cur)
    assert "owner" not in node_types["widget"]["attrs"].get("required", {})


def test_tightening_with_violators_applied_with_allow_nonconforming(conn):
    cur = _setup(conn, "up-tighten-allow")
    node_id = insert_node(conn, "up-tighten-allow", "scope1", node_type="widget", attrs={"status": "open"})
    new_pack = {**BASE_PACK, "version": 2, "node_types": {
        "widget": {"description": "A widget.", "attrs": {
            "required": {"status": {"enum": ["open", "closed"]}, "owner": {"type": "string"}},
            "closed": True}},
    }}
    report = upgrade(new_pack, "up-tighten-allow", cur, allow_nonconforming=True)
    assert any("1 nonconforming row" in line for line in report)
    node_types, _, _ = current_registry("up-tighten-allow", cur)
    assert "owner" in node_types["widget"]["attrs"]["required"]
    # the row itself is untouched (grandfathered) -- still readable, still
    # nonconforming under the new spec, exactly what doctor.py's derivation
    # is meant to surface.
    cur.execute("SELECT attrs FROM nodes WHERE id = %s", (node_id,))
    assert cur.fetchone()[0] == {"status": "open"}


def test_destructive_node_type_removal_refused_with_active_rows(conn):
    cur = _setup(conn, "up-destroy-node-active")
    insert_node(conn, "up-destroy-node-active", "scope1", node_type="widget")
    new_pack = {**BASE_PACK, "version": 2, "node_types": {}, "edge_rules": []}
    with pytest.raises(PackUpgradeRefused) as excinfo:
        upgrade(new_pack, "up-destroy-node-active", cur)
    assert any("widget" in line and "active node" in line for line in excinfo.value.worklist)
    node_types, _, _ = current_registry("up-destroy-node-active", cur)
    assert "widget" in node_types  # untouched


def test_destructive_node_type_removal_succeeds_with_no_active_rows(conn):
    cur = _setup(conn, "up-destroy-node-clean")
    new_pack = {**BASE_PACK, "version": 2, "node_types": {}, "edge_rules": []}
    # widget has no rows at all in this space -- removal must succeed cleanly.
    report = upgrade(new_pack, "up-destroy-node-clean", cur)
    assert any("destructive: node type 'widget' removed" in line for line in report)
    node_types, _, _ = current_registry("up-destroy-node-clean", cur)
    assert "widget" not in node_types


def test_destructive_edge_type_removal_refused_with_edges(conn):
    cur = _setup(conn, "up-destroy-edge-active")
    a = insert_node(conn, "up-destroy-edge-active", "scope1", node_type="widget")
    b = insert_node(conn, "up-destroy-edge-active", "scope1", node_type="widget")
    cur.execute(
        "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, %s)",
        ("up-destroy-edge-active", a, b, "relates_to"))
    new_pack = {**BASE_PACK, "version": 2, "edge_types": {}, "edge_rules": []}
    with pytest.raises(PackUpgradeRefused) as excinfo:
        upgrade(new_pack, "up-destroy-edge-active", cur)
    assert any("relates_to" in line and "edge(s)" in line for line in excinfo.value.worklist)


def test_destructive_edge_rule_removal_succeeds_with_no_matching_edges(conn):
    cur = _setup(conn, "up-destroy-rule-clean")
    new_pack = {**BASE_PACK, "version": 2, "edge_rules": []}
    report = upgrade(new_pack, "up-destroy-rule-clean", cur)
    assert any("destructive: edge rule 'relates_to' widget -> widget removed" in line for line in report)
    _, _, edge_rules = current_registry("up-destroy-rule-clean", cur)
    assert ("relates_to", "widget", "widget") not in edge_rules


def test_config_fragments_and_pack_version_updated_on_success(conn):
    cur = _setup(conn, "up-config")
    new_pack = {**BASE_PACK, "version": 7, "briefing": {"sections": []},
                "tool_aliases": {}, "tool_descriptions": {"write": "custom description"}}
    upgrade(new_pack, "up-config", cur)
    cur.execute("SELECT pack_name, pack_version FROM spaces WHERE id = 'up-config'")
    assert cur.fetchone() == ("test-pack", 7)
    cur.execute("SELECT value FROM config WHERE space_id = 'up-config' AND key = 'pack.tool_descriptions'")
    assert cur.fetchone()[0] == {"write": "custom description"}
