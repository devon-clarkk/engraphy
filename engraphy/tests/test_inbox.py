"""engraphy.core.inbox -- golden fixtures in fixtures/inbox_cases.yaml (the
capture/list/promote/discard lifecycle) plus procedural RLS/space-isolation traps
and the PENDING-then-resolve terminal flow. The harness seeds via the superuser
conn, runs the engine function through the real app-role pool (RLS live), and
asserts inbox-row state + the returned envelope.

Band forcing is test-surface, mirroring test_dedup: promote outcomes come from
candidate presence + a caller-passed `thresholds` + a synthetic unit
embedding_vector, never the real embedding model.
"""
import pathlib

import psycopg
import pytest
import yaml
from psycopg.types.json import Jsonb

from engraphy.core.dedup import (
    BandThresholds,
    NotFoundError,
    ScopeUnknownError,
    ValidationError,
    resolve_duplicate,
)
from engraphy.core.inbox import capture, discard, list_pending, promote

CASES = yaml.safe_load(
    (pathlib.Path(__file__).parent / "fixtures" / "inbox_cases.yaml").read_text(encoding="utf-8")
)

_UNIT_VEC = [1.0] + [0.0] * 383
_UNIT_VEC_LIT = "[" + ",".join(str(x) for x in _UNIT_VEC) + "]"


def _label_id(i):
    return f"00000000-0000-4000-8000-{i:012d}"


def _cleanup(conn, space_id):
    conn.rollback()  # clear any half-open read txn before deleting
    cur = conn.cursor()
    for t in ("inbox", "audit_log", "dedup_log", "pending_writes", "edges", "nodes",
              "edge_rules", "edge_types", "node_types", "config", "scope_grants",
              "scopes", "principals"):
        cur.execute(f"DELETE FROM {t} WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


def _seed(conn, space_id, seed):
    """Seed a fixture case's world: space + p1 + private scope1, any extra
    scopes, loose node types (candidates + the promote target), candidate nodes
    (unit vector), and inbox rows. Returns label -> id. Inbox ids are offset from
    node ids so the two label spaces never collide."""
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'Inbox T')", (space_id,))
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

    node_types = {n["type"] for n in seed.get("nodes", [])} | set(seed.get("node_types", []))
    for t in sorted(node_types):
        cur.execute("INSERT INTO node_types (space_id, name, description, attr_spec) "
                    "VALUES (%s, %s, 'loose', %s)", (space_id, t, Jsonb({"attrs": {"closed": False}})))

    label_to_id = {}
    for i, n in enumerate(seed.get("nodes", []), start=1):
        nid = _label_id(i)
        label_to_id[n["label"]] = nid
        cur.execute(
            "INSERT INTO nodes (id, space_id, type, scope_id, title, body, attrs, status, "
            "embedding, embedding_model, source_client, author_principal) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s::vector, 'test-model', 'pytest', 'p1')",
            (nid, space_id, n["type"], n.get("scope", "scope1"), f"Node {n['label']}",
             f"Body of node {n['label']}.", Jsonb({}), _UNIT_VEC_LIT),
        )
    for j, r in enumerate(seed.get("inbox", []), start=1):
        iid = _label_id(1000 + j)
        label_to_id[r["label"]] = iid
        cur.execute(
            "INSERT INTO inbox (id, space_id, scope_id, kind, payload, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (iid, space_id, r.get("scope", "scope1"), r["kind"], Jsonb(r["payload"]),
             r.get("status", "pending")),
        )
    conn.commit()
    return label_to_id


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
async def test_inbox_case(pool, conn, case):
    space_id = ("ib-" + case["name"].replace("_", "-"))[:60]
    action, exp = case["action"], case["expect"]
    op = action["op"]
    seed = dict(case.get("seed", {}))
    if op == "promote":  # the reviewer's target type must be a registered node type
        seed = {**seed, "node_types": [*seed.get("node_types", []), action["type"]]}
    _cleanup(conn, space_id)
    label_to_id = _seed(conn, space_id, seed)
    by_id = {v: k for k, v in label_to_id.items()}
    try:
        if op == "capture":
            res = await capture(pool, space_id, "p1", action["kind"], action["payload"],
                                scope_id=action.get("scope"))
            eb = exp["inbox"]
            assert res["v"] == 1 and res["status"] == "pending"
            cur = conn.cursor()
            cur.execute("SELECT kind, scope_id, status, payload FROM inbox WHERE id = %s", (res["id"],))
            kind, scope_id, status, payload = cur.fetchone()
            assert (status, kind, scope_id, payload) == (eb["status"], eb["kind"], eb["scope"], eb["payload"])

        elif op == "list":
            res = await list_pending(pool, space_id, "p1")
            got = {by_id[item["id"]] for item in res["items"]}
            assert got == set(exp["items"])

        elif op == "promote":
            thresholds = BandThresholds(**action["thresholds"]) if "thresholds" in action else None
            res = await promote(
                pool, space_id, "p1", label_to_id[action["item"]], action["type"], action["scope"],
                action["title"], action["body"], action.get("attrs", {}), "pytest",
                thresholds=thresholds, embedding_vector=_UNIT_VEC,
            )
            assert res["outcome"] == exp["outcome"]
            cur = conn.cursor()
            cur.execute("SELECT status FROM inbox WHERE id = %s", (label_to_id[action["item"]],))
            assert cur.fetchone()[0] == exp["item_status"]
            if exp.get("node") == "created":
                assert res["node"]["type"] == exp["node_type"]
            elif exp.get("node") == "none":
                cur.execute("SELECT count(*) FROM pending_writes WHERE space_id = %s", (space_id,))
                assert cur.fetchone()[0] == 1  # PENDING parked, no node committed

        elif op == "discard":
            res = await discard(pool, space_id, "p1", label_to_id[action["item"]])
            assert res["outcome"] == "discarded"
            cur = conn.cursor()
            cur.execute("SELECT status FROM inbox WHERE id = %s", (label_to_id[action["item"]],))
            assert cur.fetchone()[0] == exp["item_status"]
        else:
            raise AssertionError(f"unknown op {op!r}")
    finally:
        _cleanup(conn, space_id)


# --- procedural traps: RLS, space isolation, lifecycle guards ------------------

def _bootstrap(conn, space_id):
    """A two-principal world: p1 (the actor) + p2, a p1-private scope1, a
    p2-owned team-read scope (readable by p1, NOT writable), a p2-private scope
    (invisible to p1), and a loose 'widget' type with a relates_to rule (the
    distinct-resolution path needs it). Scope ids are hyphenated (the
    scopes_id_check pattern forbids underscores)."""
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'Inbox P')", (space_id,))
    cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p1', 'P1'), (%s, 'p2', 'P2')",
                (space_id, space_id))
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) VALUES "
        "(%s, 'scope1', 'S1', 'p1', 'private'), "
        "(%s, 'scope-tr', 'STR', 'p2', 'team-read'), "
        "(%s, 'scope-p2', 'SP2', 'p2', 'private')",
        (space_id, space_id, space_id),
    )
    cur.execute("INSERT INTO node_types (space_id, name, description, attr_spec) "
                "VALUES (%s, 'widget', 'w', %s)", (space_id, Jsonb({"attrs": {"closed": False}})))
    cur.execute("INSERT INTO edge_types (space_id, name, description, bidirectional) "
                "VALUES (%s, 'relates_to', 'assoc', true)", (space_id,))
    cur.execute("INSERT INTO edge_rules (space_id, type, src_type, dst_type) "
                "VALUES (%s, 'relates_to', 'widget', 'widget')", (space_id,))
    conn.commit()


def _seed_inbox_row(conn, space_id, scope_id, status="pending"):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inbox (space_id, scope_id, kind, payload, status) "
        "VALUES (%s, %s, 'note', %s, %s) RETURNING id",
        (space_id, scope_id, Jsonb({"t": 1}), status),
    )
    iid = cur.fetchone()[0]
    conn.commit()
    return iid


async def test_capture_into_unwritable_scope_denied(pool, conn):
    space_id = "ib-cap-deny"
    _cleanup(conn, space_id)
    _bootstrap(conn, space_id)
    try:
        # p1 captures into p2's team-read scope: readable but not writable ->
        # migration 0014's inbox_write WITH CHECK rejects it (fail-closed).
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await capture(pool, space_id, "p1", "note", {"t": 1}, scope_id="scope-tr")
    finally:
        _cleanup(conn, space_id)


async def test_discard_readable_nonwritable_scope_raises_scope_unknown(pool, conn):
    space_id = "ib-disc-nonwrite"
    _cleanup(conn, space_id)
    _bootstrap(conn, space_id)
    iid = _seed_inbox_row(conn, space_id, "scope-tr")  # p2 team-read: p1 sees, cannot write
    try:
        with pytest.raises(ScopeUnknownError, match="ENGRAPHY_SCOPE_UNKNOWN"):
            await discard(pool, space_id, "p1", str(iid))
    finally:
        _cleanup(conn, space_id)


async def test_discard_unreadable_row_raises_not_found(pool, conn):
    space_id = "ib-disc-unread"
    _cleanup(conn, space_id)
    _bootstrap(conn, space_id)
    iid = _seed_inbox_row(conn, space_id, "scope-p2")  # p2 private: invisible to p1
    try:
        # 07's existence-is-information collapse: an unreadable row is NOT_FOUND.
        with pytest.raises(NotFoundError, match="ENGRAPHY_NOT_FOUND"):
            await discard(pool, space_id, "p1", str(iid))
    finally:
        _cleanup(conn, space_id)


async def test_space_isolation_list_never_crosses_spaces(pool, conn):
    a, b = "ib-iso-a", "ib-iso-b"
    _cleanup(conn, a)
    _cleanup(conn, b)
    _bootstrap(conn, a)
    _bootstrap(conn, b)
    try:
        await capture(pool, a, "p1", "note", {"t": 1}, scope_id="scope1")
        res_b = await list_pending(pool, b, "p1")
        assert res_b["items"] == []  # capture in A never shows in B
        res_a = await list_pending(pool, a, "p1")
        assert len(res_a["items"]) == 1
    finally:
        _cleanup(conn, a)
        _cleanup(conn, b)


async def test_double_discard_raises_not_pending(pool, conn):
    space_id = "ib-double-disc"
    _cleanup(conn, space_id)
    _bootstrap(conn, space_id)
    iid = _seed_inbox_row(conn, space_id, "scope1")
    try:
        await discard(pool, space_id, "p1", str(iid))
        with pytest.raises(ValidationError, match="not pending"):
            await discard(pool, space_id, "p1", str(iid))
    finally:
        _cleanup(conn, space_id)


async def test_promote_already_promoted_raises_not_pending(pool, conn):
    space_id = "ib-repromote"
    _cleanup(conn, space_id)
    _bootstrap(conn, space_id)
    iid = _seed_inbox_row(conn, space_id, "scope1")
    try:
        env = await promote(pool, space_id, "p1", str(iid), "widget", "scope1",
                            "First promotion", "Body one.", {}, "pytest", embedding_vector=_UNIT_VEC)
        assert env["outcome"] == "inserted"
        with pytest.raises(ValidationError, match="not pending"):
            await promote(pool, space_id, "p1", str(iid), "widget", "scope1",
                          "Second promotion", "Body two.", {}, "pytest", embedding_vector=_UNIT_VEC)
    finally:
        _cleanup(conn, space_id)


async def test_unscoped_capture_actionable_then_discard(pool, conn):
    space_id = "ib-unscoped-act"
    _cleanup(conn, space_id)
    _bootstrap(conn, space_id)
    try:
        cap = await capture(pool, space_id, "p1", "note", {"t": 1}, scope_id=None)
        assert cap["scope"] is None
        res = await discard(pool, space_id, "p1", cap["id"])  # NULL branch of inbox_update
        assert res["outcome"] == "discarded"
        cur = conn.cursor()
        cur.execute("SELECT status FROM inbox WHERE id = %s", (cap["id"],))
        assert cur.fetchone()[0] == "discarded"
    finally:
        _cleanup(conn, space_id)


async def test_pending_promote_stays_pending_then_resolve_then_repromote(pool, conn):
    """End-to-end of Q3(a): a PENDING promote leaves the inbox row pending; the
    reviewer completes the parked write via resolve_duplicate (which has no inbox
    awareness, so the row is still pending afterward); a later re-promote now
    auto-merges into the written node and finally flips the row to 'promoted'."""
    space_id = "ib-pending-flow"
    _cleanup(conn, space_id)
    _bootstrap(conn, space_id)
    # a same-type candidate so the forced-PENDING promote has something to band against
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, status, "
        "embedding, embedding_model, source_client, author_principal) "
        "VALUES (%s, 'widget', 'scope1', 'Cand', 'Candidate body.', %s, 'active', %s::vector, "
        "'test-model', 'pytest', 'p1')",
        (space_id, Jsonb({}), _UNIT_VEC_LIT),
    )
    iid = _seed_inbox_row(conn, space_id, "scope1")
    try:
        forced = BandThresholds(t_high=2.0, t_low=-1.0)  # -> PENDING
        # A non-novel restatement of the candidate, so the >= t_high resolutions
        # below absorb (Phase B `merged`) rather than merge-linking -- the point of
        # this test is the inbox lifecycle, not the novelty split.
        env = await promote(pool, space_id, "p1", str(iid), "widget", "scope1",
                            "Maybe dup", "Candidate body.", {}, "pytest",
                            thresholds=forced, embedding_vector=_UNIT_VEC)
        assert env["outcome"] == "needs_confirmation"
        cur.execute("SELECT status FROM inbox WHERE id = %s", (iid,))
        assert cur.fetchone()[0] == "pending"  # NOT consumed

        # reviewer resolves the parked write (a >= t_high twin at default thresholds
        # legitimately merges -- the point here is only that the inbox row survives it).
        resolved = await resolve_duplicate(pool, space_id, "p1", env["pending_id"], "distinct")
        assert resolved["outcome"] in ("inserted", "merged", "merged_linked")
        cur.execute("SELECT status FROM inbox WHERE id = %s", (iid,))
        assert cur.fetchone()[0] == "pending"  # resolve_duplicate has no inbox awareness

        # a later re-promote now finds a >= t_high twin -> absorbs, flipping the row.
        env2 = await promote(pool, space_id, "p1", str(iid), "widget", "scope1",
                            "Maybe dup", "Candidate body.", {}, "pytest", embedding_vector=_UNIT_VEC)
        assert env2["outcome"] == "merged"
        cur.execute("SELECT status FROM inbox WHERE id = %s", (iid,))
        assert cur.fetchone()[0] == "promoted"
    finally:
        _cleanup(conn, space_id)


def _seed_pending_rows(conn, space_id, n, scope="scope1"):
    """n pending inbox rows with strictly increasing created_at (payload.i =
    0..n-1 is oldest..newest), so an oldest-first listing returns them in i order."""
    cur = conn.cursor()
    for i in range(n):
        cur.execute(
            "INSERT INTO inbox (space_id, scope_id, kind, payload, status, created_at) "
            "VALUES (%s, %s, 'note', %s, 'pending', now() + make_interval(secs => %s))",
            (space_id, scope, Jsonb({"i": i}), i),
        )
    conn.commit()


async def test_list_pending_default_caps_at_25(pool, conn):
    """A naive list (no limit) returns at most 25 -- E1's "worst read <= 25"
    invariant, so an agent token can never dump the whole queue. Oldest-first."""
    space_id = "ib-list-cap"
    _cleanup(conn, space_id)
    _bootstrap(conn, space_id)
    _seed_pending_rows(conn, space_id, 30)
    try:
        res = await list_pending(pool, space_id, "p1")
        assert len(res["items"]) == 25
        assert [it["payload"]["i"] for it in res["items"]] == list(range(25))  # oldest-first
    finally:
        _cleanup(conn, space_id)


async def test_list_pending_explicit_limit_and_offset_page(pool, conn):
    """A dashboard/reviewer pages the backlog with limit + offset; the short last
    page proves offset is honored and ordering is stable oldest-first."""
    space_id = "ib-list-page"
    _cleanup(conn, space_id)
    _bootstrap(conn, space_id)
    _seed_pending_rows(conn, space_id, 10)
    try:
        p1 = await list_pending(pool, space_id, "p1", limit=4, offset=0)
        p2 = await list_pending(pool, space_id, "p1", limit=4, offset=4)
        p3 = await list_pending(pool, space_id, "p1", limit=4, offset=8)
        assert [it["payload"]["i"] for it in p1["items"]] == [0, 1, 2, 3]
        assert [it["payload"]["i"] for it in p2["items"]] == [4, 5, 6, 7]
        assert [it["payload"]["i"] for it in p3["items"]] == [8, 9]  # last page short
    finally:
        _cleanup(conn, space_id)
