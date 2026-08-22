"""engraphy.admin.packs.apply() — design/01 acceptance: "both packs apply
cleanly [from empty]"; "the memory chain (Error->Pattern->Decision->Check)
inserts, links, and traverses under the example pack (standalone
CI, standalone, no external repo required)."
"""

import pathlib

import yaml

from conftest import insert_node
from engraphy.admin.packs import apply, validate

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "packs"
REPO_ROOT = pathlib.Path(__file__).parents[2]


def _load(path):
    return yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))


def test_starter_pack_applies_from_empty(conn):
    pack = _load(REPO_ROOT / "packs" / "starter" / "pack.yaml")
    assert validate(pack) == []

    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES ('starter-space', 'Starter')")
    apply(pack, "starter-space", cur)

    cur.execute("SELECT name FROM node_types WHERE space_id = 'starter-space' ORDER BY name")
    assert [r[0] for r in cur.fetchall()] == sorted(pack["node_types"].keys())

    cur.execute("SELECT name FROM edge_types WHERE space_id = 'starter-space' ORDER BY name")
    assert [r[0] for r in cur.fetchall()] == sorted(pack["edge_types"].keys())

    # involves: {src: "*", dst: person} -> one row per node type (5 types)
    cur.execute(
        "SELECT count(*) FROM edge_rules WHERE space_id = 'starter-space' AND type = 'involves'"
    )
    assert cur.fetchone()[0] == len(pack["node_types"])

    # relates_to: {src: "*", dst: "*"} -> full cross product
    cur.execute(
        "SELECT count(*) FROM edge_rules WHERE space_id = 'starter-space' AND type = 'relates_to'"
    )
    assert cur.fetchone()[0] == len(pack["node_types"]) ** 2

    cur.execute("SELECT pack_name, pack_version FROM spaces WHERE id = 'starter-space'")
    assert cur.fetchone() == (pack["pack"], pack["version"])

    # The briefing tool resolves the applied pack's briefing: fragment via
    # config['pack.briefing'] -- apply() is the only place that ever sees the
    # full pack file (E2-plan.md briefing pack-config-lookup resolution).
    cur.execute("SELECT value FROM config WHERE space_id = 'starter-space' AND key = 'pack.briefing'")
    assert cur.fetchone()[0] == pack["briefing"]

    # Same story for app.py's alias registration and per-space description
    # assembly: tool_aliases/tool_descriptions persist alongside briefing so
    # a running server can read them back without re-parsing the pack file.
    # The starter pack has no tool_aliases block -- {} is the resolved value.
    cur.execute(
        "SELECT value FROM config WHERE space_id = 'starter-space' AND key = 'pack.tool_aliases'"
    )
    assert cur.fetchone()[0] == {}
    cur.execute(
        "SELECT value FROM config WHERE space_id = 'starter-space' AND key = 'pack.tool_descriptions'"
    )
    assert cur.fetchone()[0] == pack["tool_descriptions"]


def test_example_pack_applies_from_empty(conn):
    pack = _load(FIXTURES_DIR / "example-pack.yaml")
    assert validate(pack) == []

    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES ('example-space', 'Example')")
    apply(pack, "example-space", cur)

    cur.execute("SELECT name FROM node_types WHERE space_id = 'example-space' ORDER BY name")
    assert [r[0] for r in cur.fetchall()] == sorted(pack["node_types"].keys())

    # involves: {src: "*", dst: person} -> one row per node type (10 types)
    cur.execute(
        "SELECT count(*) FROM edge_rules WHERE space_id = 'example-space' AND type = 'involves'"
    )
    assert cur.fetchone()[0] == len(pack["node_types"])

    # This pack HAS a tool_aliases block (log_error) -- confirm apply()
    # persists the real fragment, not just the starter pack's empty case.
    cur.execute("SELECT value FROM config WHERE space_id = 'example-space' AND key = 'pack.tool_aliases'")
    assert cur.fetchone()[0] == pack["tool_aliases"]
    cur.execute(
        "SELECT value FROM config WHERE space_id = 'example-space' AND key = 'pack.tool_descriptions'"
    )
    assert cur.fetchone()[0] == pack["tool_descriptions"]


def test_memory_chain_inserts_links_and_traverses(conn):
    """error --derived_from--> pattern --addresses--> decision --verified_by--> check,
    all under the example pack; a recursive walk from error reaches check."""
    pack = _load(FIXTURES_DIR / "example-pack.yaml")
    space_id = "memory-chain"
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'Example Chain')", (space_id,))
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'author', 'Author')", (space_id,)
    )
    apply(pack, space_id, cur)
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, 'life', 'Life', 'author', 'private')",
        (space_id,),
    )

    error_id = insert_node(
        conn, space_id, "life", node_type="error",
        attrs={"severity": "high", "happened_at": "2026-07-01"},
        author_principal="author", title="Deploy failed on push",
        body="The deploy pipeline failed after a config change.",
    )
    pattern_id = insert_node(
        conn, space_id, "life", node_type="pattern", attrs={"occurrences": 1},
        author_principal="author", title="Config changes need a staging run",
        body="Config changes deployed straight to prod without staging verification.",
    )
    decision_id = insert_node(
        conn, space_id, "life", node_type="decision", attrs={},
        author_principal="author", title="Require staging deploys before prod",
        body="All config changes must deploy to staging first.",
    )
    check_id = insert_node(
        conn, space_id, "life", node_type="check",
        attrs={"method": "manual"},
        author_principal="author", title="Confirm staging ran before prod deploy",
        body="Ask: did this change go through staging first?",
    )

    cur.execute(
        "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, 'derived_from')",
        (space_id, pattern_id, error_id),
    )
    cur.execute(
        "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, 'addresses')",
        (space_id, decision_id, pattern_id),
    )
    cur.execute(
        "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, 'verified_by')",
        (space_id, decision_id, check_id),
    )

    # Traverse: recursive walk (both directions) starting at error must reach check.
    cur.execute(
        """
        WITH RECURSIVE walk(id, depth) AS (
            SELECT %s::uuid, 0
          UNION
            SELECT CASE WHEN e.src_id = w.id THEN e.dst_id ELSE e.src_id END, w.depth + 1
            FROM edges e JOIN walk w ON e.src_id = w.id OR e.dst_id = w.id
            WHERE w.depth < 4
        )
        SELECT DISTINCT id FROM walk
        """,
        (error_id,),
    )
    reached = {r[0] for r in cur.fetchall()}
    assert {error_id, pattern_id, decision_id, check_id} <= reached
