"""engraphy.core.traverse — golden fixtures in fixtures/traverse_cases.yaml against
the normative inlined recursive-CTE (design/02 §Traversal) + 07 envelope. The
harness seeds a labelled graph (explicit sequential ids), runs traverse() through
the real app-role pool (RLS live), maps ids back to labels, and compares node
depths, raw edges, truncated, resolved_from, and detail.
"""
import pathlib

import pytest
import yaml
from psycopg.types.json import Jsonb

from engraphy.core.traverse import traverse

CASES = yaml.safe_load(
    (pathlib.Path(__file__).parent / "fixtures" / "traverse_cases.yaml").read_text(encoding="utf-8")
)

_UNIT_VEC = "[" + ",".join(["1.0"] + ["0.0"] * 383) + "]"


def _label_id(i):
    return f"00000000-0000-4000-8000-{i:012d}"


def _cleanup(conn, space_id):
    cur = conn.cursor()
    for t in ("edges", "nodes", "edge_rules", "edge_types", "node_types", "scopes", "principals"):
        cur.execute(f"DELETE FROM {t} WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


def _seed(conn, space_id, seed):
    """Seed the graph; return (label->id, id->label). Nodes get explicit
    sequential ids so the deterministic ORDER BY is predictable; a canonical
    must precede the node merged into it (FK) -- the fixtures list it first."""
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'Traverse T')", (space_id,))
    cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p1', 'P1')", (space_id,))
    for p in seed.get("principals", []):
        cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES (%s, %s, %s)",
                    (space_id, p, p.upper()))

    cur.execute("INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
                "VALUES (%s, 'scope1', 'Scope1', 'p1', 'private')", (space_id,))
    for s in seed.get("scopes", []):
        cur.execute("INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (space_id, s["id"], s["id"].title(), s.get("owner", "p1"), s.get("visibility", "private")))

    nodes = seed.get("nodes", [])
    node_type_of = {n["label"]: n["type"] for n in nodes}
    for t in sorted({n["type"] for n in nodes}):
        cur.execute("INSERT INTO node_types (space_id, name, description, attr_spec) "
                    "VALUES (%s, %s, 'loose', %s)", (space_id, t, Jsonb({"attrs": {"closed": False}})))

    # edge types: those the seed marks (with bidirectional) + any used in edges.
    bidir = {et["name"]: et.get("bidirectional", False) for et in seed.get("edge_types", [])}
    for etype in sorted({e["type"] for e in seed.get("edges", [])} | set(bidir)):
        cur.execute("INSERT INTO edge_types (space_id, name, description, bidirectional) "
                    "VALUES (%s, %s, 'loose', %s)", (space_id, etype, bidir.get(etype, False)))
    rules = {(e["type"], node_type_of[e["src"]], node_type_of[e["dst"]]) for e in seed.get("edges", [])}
    for etype, st, dt in sorted(rules):
        cur.execute("INSERT INTO edge_rules (space_id, type, src_type, dst_type) VALUES (%s, %s, %s, %s)",
                    (space_id, etype, st, dt))

    label_to_id = {}
    for i, n in enumerate(nodes, start=1):
        label_to_id[n["label"]] = _label_id(i)
    for i, n in enumerate(nodes, start=1):
        canonical = label_to_id[n["merged_into"]] if n.get("merged_into") else None
        status = "merged" if canonical else n.get("status", "active")
        cur.execute(
            "INSERT INTO nodes (id, space_id, type, scope_id, title, body, attrs, status, canonical_id, "
            "embedding, embedding_model, source_client, author_principal) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, 'test-model', 'pytest', 'p1')",
            (_label_id(i), space_id, n["type"], n.get("scope", "scope1"),
             f"Node {n['label']}", f"Body of node {n['label']}.", Jsonb({}), status, canonical,
             _UNIT_VEC),
        )
    for e in seed.get("edges", []):
        cur.execute("INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, %s)",
                    (space_id, label_to_id[e["src"]], label_to_id[e["dst"]], e["type"]))
    conn.commit()
    return label_to_id, {v: k for k, v in label_to_id.items()}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
async def test_traverse_case(pool, conn, case):
    space_id = ("tv-" + case["name"].replace("_", "-"))[:60]
    _cleanup(conn, space_id)
    label_to_id, by_id = _seed(conn, space_id, case["seed"])
    t = case["traverse"]
    try:
        result = await traverse(
            pool, space_id, "p1", label_to_id[t["start"]], t["direction"],
            edge_types=t.get("edge_types"), max_depth=t.get("max_depth", 4),
            limit=t.get("limit", 50), detail=t.get("detail", "summary"),
        )
        exp = case["expect"]

        assert result["v"] == 1
        got_nodes = {by_id[n["id"]]: n["depth"] for n in result["nodes"]}
        assert got_nodes == exp["nodes"]

        got_edges = {(by_id[e["src"]], by_id[e["dst"]], e["type"]) for e in result["edges"]}
        assert got_edges == {tuple(e) for e in exp["edges"]}

        assert result["truncated"] == exp["truncated"]

        got_rf = {by_id[n["id"]]: by_id[n["resolved_from"]]
                  for n in result["nodes"] if "resolved_from" in n}
        assert got_rf == exp.get("resolved_from", {})

        if exp.get("detail") == "full":
            assert all("body" in n for n in result["nodes"])
        else:
            assert all("body" not in n for n in result["nodes"])  # summary default
    finally:
        _cleanup(conn, space_id)


async def test_traverse_merged_node_never_carries_attrs_addenda(pool, conn):
    """Q1: addenda never ships in `attrs` on the wire, from any tool -- traverse
    builds node dicts through search._node_envelope too, so a node with
    attrs.addenda set (mirroring dedup._do_merge's storage) must not leak it."""
    space_id = "tv-q1-addenda-strip"
    _cleanup(conn, space_id)
    try:
        seed = {
            "nodes": [
                {"label": "a", "type": "note"},
                {"label": "b", "type": "note"},
            ],
            "edges": [{"type": "relates_to", "src": "a", "dst": "b", "bidirectional": False}],
            "edge_types": [{"name": "relates_to"}],
        }
        label_to_id, _ = _seed(conn, space_id, seed)
        cur = conn.cursor()
        cur.execute(
            "UPDATE nodes SET attrs = %s WHERE id = %s",
            (Jsonb({"addenda": [{"body": "a reworded duplicate", "author_principal": "p1"}]}),
             label_to_id["a"]),
        )
        conn.commit()

        result = await traverse(
            pool, space_id, "p1", label_to_id["a"], "out", max_depth=1, limit=50, detail="full",
        )
        node_a = next(n for n in result["nodes"] if n["id"] == label_to_id["a"])
        assert "addenda" not in node_a["attrs"]
    finally:
        _cleanup(conn, space_id)
