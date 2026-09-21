"""engraphy.admin.export_ and engraphy.admin.space_import: move one space's
memory to a space on another engine.

The two "engines" here are two spaces in the test database: the export reads the
source space on the operator connection, and the import replays the bundle into
the destination space through the app-role pool, so every node and edge write
runs under row-level security as the destination principal. Embeddings are the
deterministic text-keyed vectors test_import.py uses, so re-importing a bundle
reproduces the same vectors.
"""

import csv
import json

import psycopg
import pytest
from psycopg.types.json import Jsonb

from engraphy.admin.export_ import ExportError, export_space, open_readonly
from engraphy.admin.space_import import SpaceImportError, run_space_import
from engraphy.core import embedding
from engraphy.tests import bandvalues as bv
from engraphy.tests.conftest import APP_DATABASE_URL, DATABASE_URL
from engraphy.tests.test_import import _controlled_vector, _Embedder, _unit_from_text

_OPEN = Jsonb({"attrs": {"closed": False}})


def _vec_literal(v):
    return "[" + ",".join(str(x) for x in v) + "]"


def _types_and_rules(cur, space_id, *, error_to_widget):
    cur.execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES "
        "(%s, 'widget', 'w', %s), (%s, 'error', 'e', %s)",
        (space_id, _OPEN, space_id, _OPEN))
    cur.execute(
        "INSERT INTO edge_types (space_id, name, description, bidirectional) VALUES "
        "(%s, 'relates_to', 'r', true), (%s, 'supersedes', 's', false), "
        "(%s, 'same_topic', 't', true)", (space_id, space_id, space_id))
    rules = [("relates_to", "widget", "widget"), ("relates_to", "error", "error"),
             ("supersedes", "widget", "widget"), ("same_topic", "widget", "widget")]
    if error_to_widget:
        rules.append(("relates_to", "error", "widget"))
    for t, s, d in rules:
        cur.execute("INSERT INTO edge_rules (space_id, type, src_type, dst_type) "
                    "VALUES (%s, %s, %s, %s)", (space_id, t, s, d))


def _node(cur, space_id, ntype, scope, title, body, *, status="active", attrs=None,
          canonical=None, author="alice", session=None):
    vec = _unit_from_text(embedding.searchable_text(title, body, ""))
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, status, canonical_id, "
        "embedding, embedding_model, source_client, author_principal, source_session) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, 'seed', %s, %s) RETURNING id",
        (space_id, ntype, scope, title, body, Jsonb(attrs or {}), status, canonical,
         _vec_literal(vec), embedding.MODEL_STAMP, author, session))
    return cur.fetchone()[0]


def _edge(cur, space_id, src, dst, etype):
    cur.execute("INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, %s)",
                (space_id, src, dst, etype))


def _drop_space(conn, space_id):
    cur = conn.cursor()
    for t in ("pending_writes", "audit_log", "dedup_log", "edges", "nodes", "scope_grants",
              "scopes", "config", "edge_rules", "edge_types", "node_types", "metrics_rollup",
              "principals"):
        cur.execute(f"DELETE FROM {t} WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


@pytest.fixture
def spaces(conn, request):
    """A populated source space and an empty destination space."""
    tag = request.node.name.replace("_", "-")[:40]
    src, dst = f"xs-{tag}", f"xd-{tag}"
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'Source'), (%s, 'Dest')", (src, dst))
    cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES "
                "(%s, 'alice', 'A'), (%s, 'bob', 'B'), (%s, 'devon', 'D')", (src, src, dst))
    _types_and_rules(cur, src, error_to_widget=True)
    _types_and_rules(cur, dst, error_to_widget=False)
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility, ambient, "
        "hints, description) VALUES "
        "(%s, 'personal-alice', 'Alice', 'alice', 'private', true, '{}', NULL), "
        "(%s, 'proj-x', 'Project X', 'alice', 'team-read', false, '{x,xray}', 'The X project'), "
        "(%s, 'proj-y', 'Project Y', 'alice', 'private', false, '{}', 'Old work')",
        (src, src, src))
    cur.execute("UPDATE scopes SET archived = true WHERE space_id = %s AND id = 'proj-y'", (src,))
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility, ambient) "
        "VALUES (%s, 'personal-devon', 'Devon', 'devon', 'private', true)", (dst,))

    ids = {}
    ids["alpha"] = _node(cur, src, "widget", "personal-alice", "Alpha", "Alpha body.",
                         attrs={"color": "red",
                                "addenda": [{"body": "Alpha said again.", "at": "2026-01-01"}]},
                         session="s-1")
    ids["beta"] = _node(cur, src, "widget", "proj-x", "Beta", "Beta body.", author="bob")
    ids["gamma_old"] = _node(cur, src, "widget", "proj-x", "Gamma old", "Gamma was one thing.",
                             status="superseded")
    ids["gamma_new"] = _node(cur, src, "widget", "proj-x", "Gamma new", "Gamma is another thing.")
    ids["delta"] = _node(cur, src, "error", "proj-y", "Delta", "Delta body.", status="archived")
    ids["beta_dup"] = _node(cur, src, "widget", "proj-x", "Beta restated", "Beta again.",
                            status="merged", canonical=ids["beta"])
    _edge(cur, src, ids["alpha"], ids["beta"], "relates_to")
    _edge(cur, src, ids["gamma_new"], ids["gamma_old"], "supersedes")
    _edge(cur, src, ids["beta"], ids["beta_dup"], "relates_to")
    _edge(cur, src, ids["delta"], ids["alpha"], "relates_to")
    conn.commit()
    yield src, dst, ids
    _drop_space(conn, src)
    _drop_space(conn, dst)


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _dst_nodes(conn, dst):
    cur = conn.cursor()
    cur.execute("SELECT title, scope_id, status, author_principal, attrs, source_session "
                "FROM nodes WHERE space_id = %s ORDER BY title", (dst,))
    return {r[0]: r[1:] for r in cur.fetchall()}


def _dst_edges(conn, dst):
    cur = conn.cursor()
    cur.execute("SELECT s.title, d.title, e.type FROM edges e JOIN nodes s ON s.id = e.src_id "
                "JOIN nodes d ON d.id = e.dst_id WHERE e.space_id = %s ORDER BY 1, 2", (dst,))
    return set(cur.fetchall())


async def _import(pool, dst, bundle, **kw):
    kw.setdefault("embed_document", _Embedder())
    return await run_space_import(pool, DATABASE_URL, dst, "devon", bundle, **kw)


# ---- export -------------------------------------------------------------------


def test_export_writes_every_scope_node_and_edge(spaces, tmp_path):
    src, _, _ = spaces
    bundle = tmp_path / "out.jsonl"
    counts = export_space(DATABASE_URL, src, bundle)
    assert counts == {"scopes": 3, "node_types": 2, "nodes": 6, "edges": 4}
    lines = _lines(bundle)
    assert lines[0]["kind"] == "header" and lines[0]["source_space"] == src
    scope = next(x for x in lines if x["kind"] == "scope" and x["id"] == "proj-x")
    assert scope["visibility"] == "team-read" and scope["hints"] == ["x", "xray"]
    assert scope["description"] == "The X project"
    statuses = {x["title"]: x["status"] for x in lines if x["kind"] == "node"}
    assert statuses["Gamma old"] == "superseded" and statuses["Delta"] == "archived"
    assert statuses["Beta restated"] == "merged"
    assert all("embedding" not in x for x in lines), "a bundle carries no vectors"


def test_export_connection_refuses_writes(spaces):
    src, _, _ = spaces
    with open_readonly(DATABASE_URL) as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("UPDATE scopes SET display_name = 'x' WHERE space_id = %s", (src,))


def test_export_refuses_a_role_that_does_not_bypass_rls(spaces, tmp_path):
    src, _, _ = spaces
    with pytest.raises(ExportError, match="row-level security"):
        export_space(APP_DATABASE_URL, src, tmp_path / "out.jsonl")


def test_export_by_scope_keeps_only_edges_inside_it(spaces, tmp_path):
    src, _, _ = spaces
    bundle = tmp_path / "x.jsonl"
    counts = export_space(DATABASE_URL, src, bundle, scopes=["proj-x"])
    assert counts["nodes"] == 4 and counts["edges"] == 2
    with pytest.raises(ExportError, match="nope"):
        export_space(DATABASE_URL, src, bundle, scopes=["nope"])


# ---- import -------------------------------------------------------------------


async def test_import_remaps_space_principal_scopes_statuses_and_edges(pool, conn, spaces, tmp_path):
    src, dst, _ = spaces
    bundle = tmp_path / "out.jsonl"
    export_space(DATABASE_URL, src, bundle)
    s = await _import(pool, dst, bundle)

    assert s.inserted == 5 and s.merged_status_skipped == 1 and s.left_for_review == 0
    assert s.scope_map == {"personal-alice": "personal-devon", "proj-x": "proj-x", "proj-y": "proj-y"}
    assert s.scopes_created == ["proj-x", "proj-y"]
    assert s.source_authors == {"alice": 5, "bob": 1}

    nodes = _dst_nodes(conn, dst)
    assert set(nodes) == {"Alpha", "Beta", "Gamma old", "Gamma new", "Delta"}
    assert {n[2] for n in nodes.values()} == {"devon"}, "every node authored by the destination principal"
    assert nodes["Alpha"][0] == "personal-devon"
    assert nodes["Gamma old"][1] == "superseded" and nodes["Delta"][1] == "archived"
    assert nodes["Gamma new"][1] == "active"
    assert nodes["Alpha"][3]["color"] == "red"
    assert nodes["Alpha"][3]["addenda"][0]["body"] == "Alpha said again."
    assert nodes["Alpha"][4] == "s-1"

    assert _dst_edges(conn, dst) == {("Alpha", "Beta", "relates_to"),
                                     ("Gamma new", "Gamma old", "supersedes")}
    assert s.edges_attached == 2
    assert s.edges_skipped_same_node == 1, "Beta -> its merged restatement collapses to one node"
    assert s.edges_skipped_no_rule == 1, "the destination declares no error -> widget relates_to"

    cur = conn.cursor()
    cur.execute("SELECT id, owner_principal, visibility, hints, description, archived FROM scopes "
                "WHERE space_id = %s AND id IN ('proj-x', 'proj-y') ORDER BY id", (dst,))
    assert cur.fetchall() == [("proj-x", "devon", "team-read", ["x", "xray"], "The X project", False),
                              ("proj-y", "devon", "private", [], "Old work", True)]


async def test_reimporting_the_same_bundle_writes_nothing(pool, conn, spaces, tmp_path):
    src, dst, _ = spaces
    bundle = tmp_path / "out.jsonl"
    export_space(DATABASE_URL, src, bundle)
    await _import(pool, dst, bundle)
    before_nodes, before_edges = _dst_nodes(conn, dst), _dst_edges(conn, dst)

    s = await _import(pool, dst, bundle)
    assert s.already_present == 5 and s.inserted == 0 and s.merged == 0
    assert s.edges_attached == 0 and s.edges_already_present == 2
    assert s.scopes_created == [] and s.statuses_restored == 0
    assert _dst_nodes(conn, dst) == before_nodes
    assert _dst_edges(conn, dst) == before_edges


async def test_scope_map_redirects_a_scope(pool, conn, spaces, tmp_path):
    src, dst, _ = spaces
    bundle = tmp_path / "out.jsonl"
    export_space(DATABASE_URL, src, bundle)
    s = await _import(pool, dst, bundle, scope_map=["proj-y=personal-devon"])
    assert "proj-y" not in s.scopes_created
    assert _dst_nodes(conn, dst)["Delta"][0] == "personal-devon"


async def test_missing_node_type_stops_the_import_before_any_write(pool, conn, spaces, tmp_path):
    src, dst, _ = spaces
    bundle = tmp_path / "out.jsonl"
    export_space(DATABASE_URL, src, bundle)
    cur = conn.cursor()
    cur.execute("DELETE FROM edge_rules WHERE space_id = %s AND (src_type = 'error' OR dst_type = 'error')", (dst,))
    cur.execute("DELETE FROM node_types WHERE space_id = %s AND name = 'error'", (dst,))
    conn.commit()
    with pytest.raises(SpaceImportError, match="error"):
        await _import(pool, dst, bundle)
    assert _dst_nodes(conn, dst) == {}
    cur.execute("SELECT count(*) FROM scopes WHERE space_id = %s", (dst,))
    assert cur.fetchone()[0] == 1, "no scope was created"


async def test_no_create_scopes_refuses_before_any_write(pool, conn, spaces, tmp_path):
    src, dst, _ = spaces
    bundle = tmp_path / "out.jsonl"
    export_space(DATABASE_URL, src, bundle)
    with pytest.raises(SpaceImportError, match="proj-x"):
        await _import(pool, dst, bundle, create_scopes=False)
    assert _dst_nodes(conn, dst) == {}


async def test_dry_run_writes_nothing(pool, conn, spaces, tmp_path):
    src, dst, _ = spaces
    bundle = tmp_path / "out.jsonl"
    export_space(DATABASE_URL, src, bundle)
    s = await _import(pool, dst, bundle, dry_run=True)
    assert s.dry_run and s.scopes_created == ["proj-x", "proj-y"]
    assert _dst_nodes(conn, dst) == {}
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM scopes WHERE space_id = %s", (dst,))
    assert cur.fetchone()[0] == 1


async def test_an_unknown_or_archived_principal_is_refused(pool, conn, spaces, tmp_path):
    src, dst, _ = spaces
    bundle = tmp_path / "out.jsonl"
    export_space(DATABASE_URL, src, bundle)
    with pytest.raises(SpaceImportError, match="not a member"):
        await run_space_import(pool, DATABASE_URL, dst, "nobody", bundle, embed_document=_Embedder())
    conn.cursor().execute("UPDATE principals SET archived = true WHERE space_id = %s AND id = 'devon'", (dst,))
    conn.commit()
    with pytest.raises(SpaceImportError, match="archived"):
        await _import(pool, dst, bundle)


async def test_near_duplicates_within_the_bundle_are_kept_distinct(pool, conn, spaces, tmp_path):
    """Two source nodes the source engine held side by side land in the confirm
    band against each other on import. The import resolves the second as
    distinct, because the source held both."""
    src, dst, _ = spaces
    cur = conn.cursor()
    _node(cur, src, "widget", "proj-x", "Kappa one", "Kappa first.")
    _node(cur, src, "widget", "proj-x", "Kappa two", "Kappa second.")
    conn.commit()
    bundle = tmp_path / "out.jsonl"
    export_space(DATABASE_URL, src, bundle, scopes=["proj-x"])
    one = _unit_from_text(embedding.searchable_text("Kappa one", "Kappa first.", ""))
    two = _controlled_vector(one, bv.PENDING, "kappa")
    embed = _Embedder({embedding.searchable_text("Kappa two", "Kappa second.", ""): two})

    s = await _import(pool, dst, bundle, embed_document=embed)
    assert s.resolved_distinct == 1 and s.left_for_review == 0
    assert {"Kappa one", "Kappa two"} <= set(_dst_nodes(conn, dst))


async def test_a_near_duplicate_of_existing_destination_content_is_left_for_review(
        pool, conn, spaces, tmp_path):
    """A node close to what the destination already held is parked for the
    principal to resolve, listed in the review CSV, and its edges wait."""
    src, dst, _ = spaces
    beta = _unit_from_text(embedding.searchable_text("Beta", "Beta body.", ""))
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
        "VALUES (%s, 'proj-x', 'X', 'devon', 'private')", (dst,))
    near = _controlled_vector(beta, bv.PENDING, "prior")
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, embedding, embedding_model, "
        "source_client, author_principal) VALUES (%s, 'widget', 'proj-x', 'Prior', 'Prior body.', "
        "%s::vector, %s, 'seed', 'devon')", (dst, _vec_literal(near), embedding.MODEL_STAMP))
    conn.commit()
    bundle = tmp_path / "out.jsonl"
    export_space(DATABASE_URL, src, bundle)

    s = await _import(pool, dst, bundle)
    assert s.left_for_review == 1
    assert "Beta" not in _dst_nodes(conn, dst)
    # Alpha -> Beta, and Beta -> its merged restatement (which maps to Beta), both
    # wait for Beta's resolution.
    assert s.edges_skipped_unmapped == 2
    rows = list(csv.reader(s.review_path.open(encoding="utf-8")))
    assert rows[0][0] == "pending_id" and rows[1][3] == "Beta"


async def test_an_archived_destination_scope_takes_no_new_writes(pool, conn, spaces, tmp_path):
    """A destination scope that is already archived takes no writes: a node
    bound for it is skipped and counted, and the rest of the bundle imports."""
    src, dst, _ = spaces
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility, archived) "
        "VALUES (%s, 'proj-y', 'Y', 'devon', 'private', true)", (dst,))
    conn.commit()
    bundle = tmp_path / "out.jsonl"
    export_space(DATABASE_URL, src, bundle)
    s = await _import(pool, dst, bundle)
    assert s.skipped_archived_scope == 1
    assert "Delta" not in _dst_nodes(conn, dst)
    assert s.inserted == 4


def test_a_bundle_without_a_header_is_refused(tmp_path):
    from engraphy.admin.space_import import read_bundle
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"kind": "node"}) + "\n", encoding="utf-8")
    with pytest.raises(SpaceImportError, match="not an Engraphy space export"):
        read_bundle(bad)
