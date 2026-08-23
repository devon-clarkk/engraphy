"""engraphy.admin.doctor -- design/04 s.Hygiene: stale pendings, attrs_nonconforming
(derived, not stored -- see doctor.py's module docstring), registry-vs-pack
drift, orphaned merges, canonical chains >3, nodes with >20 addenda.
"""
import datetime
import pathlib

import pytest
import yaml
from psycopg.types.json import Jsonb

from conftest import bootstrap_space, insert_node
from engraphy.admin import doctor
from engraphy.admin.packs import apply as pack_apply

REPO_ROOT = pathlib.Path(__file__).parents[2]


def _report_line_starting(report, prefix):
    return next((line for line in report if line.strip().startswith(prefix)), None)


def test_doctor_unknown_space_raises(conn):
    cur = conn.cursor()
    with pytest.raises(doctor.DoctorError, match="does not exist"):
        doctor.run(cur, "no-such-space")


def test_doctor_clean_space_reports_zero_everywhere(conn):
    ctx = bootstrap_space(conn, space_id="doc-clean")
    cur = conn.cursor()
    report = doctor.run(cur, ctx["space_id"])
    assert _report_line_starting(report, "stale pendings") == "stale pendings (expired, unresolved): 0"
    assert _report_line_starting(report, "attrs_nonconforming") == "attrs_nonconforming: 0"
    assert _report_line_starting(report, "orphaned merges") == "orphaned merges (canonical target not active): 0"
    assert _report_line_starting(report, "canonical chains") == "canonical chains >3 hops: 0"
    assert _report_line_starting(report, "nodes with >20 addenda") == "nodes with >20 addenda: 0"
    # no pack applied to a bootstrap_space -- drift check must skip, not fail
    assert "skipped (no pack applied" in _report_line_starting(report, "registry-vs-pack drift")


def test_doctor_stale_pendings_counts_only_expired(conn):
    ctx = bootstrap_space(conn, space_id="doc-pending")
    cur = conn.cursor()
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
    cur.execute(
        "INSERT INTO pending_writes (space_id, author_principal, payload, expires_at) "
        "VALUES (%s, %s, %s, %s)",
        (ctx["space_id"], ctx["principal_id"], Jsonb({}), past),
    )
    cur.execute(
        "INSERT INTO pending_writes (space_id, author_principal, payload, expires_at) "
        "VALUES (%s, %s, %s, %s)",
        (ctx["space_id"], ctx["principal_id"], Jsonb({}), future),
    )
    report = doctor.run(cur, ctx["space_id"])
    assert _report_line_starting(report, "stale pendings") == "stale pendings (expired, unresolved): 1"


def test_doctor_attrs_nonconforming_is_derived_from_current_spec(conn):
    """A row valid under the spec it was written against becomes nonconforming
    the moment node_types.attr_spec tightens underneath it (no write to the
    row itself) -- this is exactly the grandfathered-row scenario pack
    upgrade's tightening path produces, and doctor must catch it with no
    separate flag column, per the derivation decision in DECISIONS-DELTA.md."""
    ctx = bootstrap_space(conn, space_id="doc-nonconform")
    insert_node(conn, ctx["space_id"], ctx["scope_id"], attrs={"status": "open"})
    cur = conn.cursor()

    report_before = doctor.run(cur, ctx["space_id"])
    assert _report_line_starting(report_before, "attrs_nonconforming") == "attrs_nonconforming: 0"

    tightened = {
        "attrs": {
            "required": {"status": {"enum": ["open", "closed"]}, "owner": {"type": "string"}},
            "optional": {"note": {"type": "string"}},
            "closed": True,
        }
    }
    cur.execute(
        "UPDATE node_types SET attr_spec = %s WHERE space_id = %s AND name = 'widget'",
        (Jsonb(tightened), ctx["space_id"]),
    )
    report_after = doctor.run(cur, ctx["space_id"])
    assert _report_line_starting(report_after, "attrs_nonconforming") == "attrs_nonconforming: 1"
    assert any("type 'widget': 1" in line for line in report_after)


def test_doctor_orphaned_merge(conn):
    ctx = bootstrap_space(conn, space_id="doc-orphan")
    target = insert_node(conn, ctx["space_id"], ctx["scope_id"])
    merged = insert_node(conn, ctx["space_id"], ctx["scope_id"])
    cur = conn.cursor()
    cur.execute(
        "UPDATE nodes SET status = 'merged', canonical_id = %s WHERE id = %s", (target, merged))
    # the target itself later archived -- merged's canonical_id now points at
    # a non-active node, which is what "orphaned" means here.
    cur.execute("UPDATE nodes SET status = 'archived' WHERE id = %s", (target,))

    report = doctor.run(cur, ctx["space_id"])
    assert _report_line_starting(report, "orphaned merges") == "orphaned merges (canonical target not active): 1"


def test_doctor_long_canonical_chain_flagged_short_chain_not(conn):
    ctx = bootstrap_space(conn, space_id="doc-chain")
    cur = conn.cursor()

    # A -> B -> C -> D -> E(active): 4 hops from A, over the >3 threshold.
    e = insert_node(conn, ctx["space_id"], ctx["scope_id"])
    d = insert_node(conn, ctx["space_id"], ctx["scope_id"])
    c = insert_node(conn, ctx["space_id"], ctx["scope_id"])
    b = insert_node(conn, ctx["space_id"], ctx["scope_id"])
    a = insert_node(conn, ctx["space_id"], ctx["scope_id"])
    cur.execute("UPDATE nodes SET status = 'merged', canonical_id = %s WHERE id = %s", (e, d))
    cur.execute("UPDATE nodes SET status = 'merged', canonical_id = %s WHERE id = %s", (d, c))
    cur.execute("UPDATE nodes SET status = 'merged', canonical_id = %s WHERE id = %s", (c, b))
    cur.execute("UPDATE nodes SET status = 'merged', canonical_id = %s WHERE id = %s", (b, a))

    # X -> Y(active): a plain 1-hop merge, well under the threshold.
    y = insert_node(conn, ctx["space_id"], ctx["scope_id"])
    x = insert_node(conn, ctx["space_id"], ctx["scope_id"])
    cur.execute("UPDATE nodes SET status = 'merged', canonical_id = %s WHERE id = %s", (y, x))

    report = doctor.run(cur, ctx["space_id"])
    assert _report_line_starting(report, "canonical chains") == "canonical chains >3 hops: 1"
    # only A's chain (A->B->C->D->E) is 4 hops; B/C/D/x are each also merge
    # heads, but their own sub-chains are <=3 hops and must not be flagged.
    assert any(str(a) in line for line in report if "hops" in line)


def test_doctor_high_addenda_threshold_is_strictly_greater_than_20(conn):
    ctx = bootstrap_space(conn, space_id="doc-addenda")
    cur = conn.cursor()
    # closed:false, matching test_dedup.py's own merge-fixture convention --
    # NOT bootstrap_space's closed:true 'widget' type. Both shipped packs
    # declare every node type closed:true and neither declares "addenda" in
    # required/optional, so nodes_validate_attrs_fn's Phase-3 closed check
    # would reject the very first real merge onto any of their types with
    # CheckViolation "attrs.addenda is not allowed (closed spec)" -- a
    # pre-existing E0/E1 kernel bug this test tripped over and works around,
    # flagged separately (out of E3 scope; not doctor's or this test's to fix).
    cur.execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES (%s, %s, %s, %s)",
        (ctx["space_id"], "open_widget", "non-closed test type", Jsonb({"attrs": {"closed": False}})),
    )
    boundary = insert_node(conn, ctx["space_id"], ctx["scope_id"], node_type="open_widget", attrs={})
    over = insert_node(conn, ctx["space_id"], ctx["scope_id"], node_type="open_widget", attrs={})
    cur.execute(
        "UPDATE nodes SET attrs = jsonb_set(attrs, '{addenda}', %s::jsonb) WHERE id = %s",
        (Jsonb([{"body": "x"}] * 20), boundary),
    )
    cur.execute(
        "UPDATE nodes SET attrs = jsonb_set(attrs, '{addenda}', %s::jsonb) WHERE id = %s",
        (Jsonb([{"body": "x"}] * 21), over),
    )
    report = doctor.run(cur, ctx["space_id"])
    assert _report_line_starting(report, "nodes with >20 addenda") == "nodes with >20 addenda: 1"
    assert any(str(over) in line for line in report)
    assert not any(str(boundary) in line for line in report if "addenda:" in line)


def test_doctor_registry_drift_flags_hand_edited_extra_node_type(conn, tmp_path):
    pack = yaml.safe_load((REPO_ROOT / "packs" / "starter" / "pack.yaml").read_text(encoding="utf-8"))
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES ('doc-drift', 'Drift')")
    pack_apply(pack, "doc-drift", cur)

    # simulate a hand-edited registry row -- something that never went through
    # pack apply/upgrade (design/04: "hand-edited registry rows are
    # unsupported and flagged by doctor").
    cur.execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) "
        "VALUES ('doc-drift', 'rogue', 'hand-edited', '{}')")

    report = doctor.run(cur, "doc-drift", pack_file=REPO_ROOT / "packs" / "starter" / "pack.yaml")
    drift_line = _report_line_starting(report, "registry-vs-pack drift")
    assert drift_line is not None and not drift_line.endswith(": 0")
    assert any("rogue" in line and "hand-edited" in line for line in report)


def test_doctor_registry_drift_skips_when_pack_file_missing(conn, tmp_path):
    pack = yaml.safe_load((REPO_ROOT / "packs" / "starter" / "pack.yaml").read_text(encoding="utf-8"))
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES ('doc-nofile', 'NoFile')")
    pack_apply(pack, "doc-nofile", cur)

    report = doctor.run(cur, "doc-nofile", pack_file=tmp_path / "does-not-exist.yaml")
    assert "skipped (pack file not found" in _report_line_starting(report, "registry-vs-pack drift")
