"""Briefing engine — golden fixtures in fixtures/briefing/ (design/07 §Golden
fixtures: "Every section construct against a seeded graph"; design/02 §The
briefing engine; the E1 briefing gate's Q1–Q4 decisions, DECISIONS-DELTA.md).

section_cases.yaml: labeled seed graph + a pack `briefing:` fragment +
expected label sequences. Node/edge types are auto-created LOOSE (closed:
false, rules derived from the seeded edges) — these cases test the GRAMMAR,
not attr validation. example_briefing.yaml: applies the REAL example pack
and byte-compares the whole label-mapped briefing (hint-less, so the semantic
section is deterministically empty — advisor fix #1).

Seeding conventions (the fixture headers are normative about these):
- every node embeds via embed_document (the write path's own convention);
- each node commits in its OWN transaction, so updated_at strictly increases
  in seed order (nodes_touch stamps INSERTs);
- ids are explicit and sequential, so (updated_at DESC, id ASC) is a
  deterministic total order even under a microsecond timestamp tie;
- attr values "REL:+Nd" / "REL:-Nd" resolve to dates relative to today.
"""
import datetime
import pathlib

import pytest
import yaml
from psycopg.types.json import Jsonb

from engraphy.admin import packs as packs_mod
from engraphy.core import embedding as _emb
from engraphy.core.briefing import briefing

FIXDIR = pathlib.Path(__file__).parent / "fixtures" / "briefing"
PACKS_DIR = pathlib.Path(__file__).parent / "fixtures" / "packs"

SECTION_CASES = yaml.safe_load((FIXDIR / "section_cases.yaml").read_text(encoding="utf-8"))
EXAMPLE_PACK = yaml.safe_load((FIXDIR / "example_briefing.yaml").read_text(encoding="utf-8"))

# QUESTIONS.md "semantic-section-relevance-floor" resolved 2026-07-17 (Devon
# option b / Fable design): a vector-leg relevance floor (briefing.semantic_floor,
# default 0.50) drops PR_far (query<->doc cosine 0.4313, baselined) before fusion;
# it has no lexical match either, so `excludes` now holds by design. Un-xfailed.


def _label_id(i: int) -> str:
    return f"00000000-0000-4000-8000-{i:012d}"


def _rel(value):
    """'REL:+2d' / 'REL:-3d' -> ISO date relative to today; anything else as-is."""
    if isinstance(value, str) and value.startswith("REL:"):
        sign = 1 if value[4] == "+" else -1
        return (
            datetime.date.today()  # noqa: DTZ011 -- mirrors briefing._resolve_relative_date
            + sign * datetime.timedelta(days=int(value[5:-1]))
        ).isoformat()
    return value


def _insert_node(conn, space_id, nid, node_type, scope, title, body, attrs, status,
                 created_days_ago=None):
    """One node, committed in its OWN transaction (updated_at ordering)."""
    vec = _emb.embed_document(title + "\n" + body)
    lit = "[" + ",".join(str(x) for x in vec) + "]"
    cur = conn.cursor()
    cols = ("id, space_id, type, scope_id, title, body, attrs, status, embedding, "
            "embedding_model, source_client, author_principal")
    placeholders = "%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, 'pytest', 'p1'"
    params = [nid, space_id, node_type, scope, title, body,
              Jsonb(attrs), status, lit, _emb.MODEL_STAMP]
    if created_days_ago is not None:
        cols += ", created_at"
        placeholders += ", now() - make_interval(days => %s)"
        params.append(created_days_ago)
    cur.execute(f"INSERT INTO nodes ({cols}) VALUES ({placeholders})", params)
    conn.commit()


def _seed_space(conn, space_id, seed, *, pack=None, sections=None):
    """Space + p1 + scopes + types + labeled nodes/edges/inbox. Returns
    (label->id, id->label). pack given -> its registries are applied and its
    ambient_scopes created; otherwise types are auto-created loose from the
    seed + section references and edge rules derived from the seeded edges."""
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'Briefing T')", (space_id,))
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name) VALUES (%s, 'p1', 'P1')", (space_id,)
    )
    for p in seed.get("principals", []):
        cur.execute(
            "INSERT INTO principals (space_id, id, display_name) VALUES (%s, %s, %s)",
            (space_id, p, p.upper()),
        )

    def make_scope(sid, owner="p1", visibility="private", ambient=False):
        cur.execute(
            "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility, ambient) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (space_id, sid, sid.title(), owner, visibility, ambient),
        )

    if pack is not None:
        packs_mod.apply(pack, space_id, cur)
        for sid in pack.get("ambient_scopes", []):
            make_scope(sid, ambient=True)
    else:
        make_scope("scope1")  # the section cases' default scope

    for s in seed.get("scopes", []):
        make_scope(s["id"], s.get("owner", "p1"), s.get("visibility", "private"),
                   s.get("ambient", False))

    nodes = list(seed.get("nodes", []))
    for i in range(1, seed.get("cap_decisions", 0) + 1):
        nodes.append({
            "label": f"D{i:02d}", "type": "decision", "scope": "scope1",
            "title": f"Decision {i:02d}",
            "body": f"Generated standing decision number {i:02d} for the cap case.",
        })

    if pack is None:
        # Auto-create loose types: the GRAMMAR is under test, not attr validation.
        node_types = {n["type"] for n in nodes}
        for section in sections or []:
            if "type" in section:
                node_types.add(section["type"])
            node_types.update(section.get("types", []))
        for t in sorted(node_types):
            cur.execute(
                "INSERT INTO node_types (space_id, name, description, attr_spec) "
                "VALUES (%s, %s, 'loose test type', %s)",
                (space_id, t, Jsonb({"attrs": {"closed": False}})),
            )
        edge_types = {e["type"] for e in seed.get("edges", [])}
        for section in sections or []:
            for key in ("include_linked", "without_edge"):
                edge = (section.get(key) or {}).get("edge")
                if edge:
                    edge_types.add(edge)
        for t in sorted(edge_types):
            cur.execute(
                "INSERT INTO edge_types (space_id, name, description) VALUES (%s, %s, 'loose')",
                (space_id, t),
            )
    conn.commit()

    label_to_id = {}
    for i, n in enumerate(nodes, start=1):
        nid = _label_id(i)
        label_to_id[n["label"]] = nid
        attrs = {k: _rel(v) for k, v in (n.get("attrs") or {}).items()}
        _insert_node(
            conn, space_id, nid, n["type"], n["scope"], n["title"], n["body"],
            attrs, n.get("status", "active"), n.get("created_days_ago"),
        )

    node_type_of = {n["label"]: n["type"] for n in nodes}
    if pack is None:
        rules = {(e["type"], node_type_of[e["src"]], node_type_of[e["dst"]])
                 for e in seed.get("edges", [])}
        for etype, src_t, dst_t in sorted(rules):
            cur.execute(
                "INSERT INTO edge_rules (space_id, type, src_type, dst_type) "
                "VALUES (%s, %s, %s, %s)",
                (space_id, etype, src_t, dst_t),
            )
    for e in seed.get("edges", []):
        cur.execute(
            "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, %s)",
            (space_id, label_to_id[e["src"]], label_to_id[e["dst"]], e["type"]),
        )
    for row in seed.get("inbox", []):
        cols = "space_id, scope_id, status"
        placeholders = "%s, %s, %s"
        params = [space_id, row.get("scope"), row.get("status", "pending")]
        if row.get("created_days_ago") is not None:  # for the 14-day nag age filter
            cols += ", created_at"
            placeholders += ", now() - make_interval(days => %s)"
            params.append(row["created_days_ago"])
        cur.execute(f"INSERT INTO inbox ({cols}) VALUES ({placeholders})", params)
    conn.commit()
    return label_to_id, {v: k for k, v in label_to_id.items()}


def _cleanup(conn, space_id):
    cur = conn.cursor()
    for t in ("inbox", "audit_log", "dedup_log", "pending_writes", "edges", "nodes",
              "edge_rules", "edge_types", "node_types", "scopes", "config", "principals"):
        cur.execute(f"DELETE FROM {t} WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


def _updated_desc_order(conn, ids_to_labels, labels, label_to_id, cap=None):
    """The labels sorted by the implementation's own total order (updated_at
    DESC, id ASC), read back from the DB — the harness computes the expected
    sequence rather than hand-pinning timestamps (fixture header)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM nodes WHERE id = ANY(%s::uuid[]) ORDER BY updated_at DESC, id ASC",
        ([label_to_id[label] for label in labels],),
    )
    ordered = [ids_to_labels[str(r[0])] for r in cur.fetchall()]
    return ordered[:cap] if cap is not None else ordered


def _section_labels(section, by_id):
    return [by_id[n["id"]] for n in section["nodes"]]


def _assert_section(exp, got, by_id, conn, label_to_id):
    assert got["name"] == exp["name"]
    labels = _section_labels(got, by_id)

    if "nodes" in exp:
        if exp.get("order") == "updated_at_desc":
            assert set(labels) == set(exp["nodes"])
            assert labels == _updated_desc_order(conn, by_id, exp["nodes"], label_to_id)
        else:
            assert labels == exp["nodes"]
    if "count" in exp:
        assert len(labels) == exp["count"]
        if exp.get("order") == "updated_at_desc":
            everything = list(label_to_id)
            assert labels == _updated_desc_order(
                conn, by_id, everything, label_to_id, cap=exp["count"]
            )
    if "members" in exp:
        assert set(exp["members"]) <= set(labels)
    if "excludes" in exp:
        assert not set(exp["excludes"]) & set(labels)
    if "linked" in exp:
        for node in got["nodes"]:
            label = by_id[node["id"]]
            expected_linked = exp["linked"].get(label, [])
            assert "linked" in node, f"{label}: include_linked section must carry linked"
            assert [by_id[ln["id"]] for ln in node["linked"]] == expected_linked


@pytest.mark.parametrize("case", SECTION_CASES, ids=[c["name"] for c in SECTION_CASES])
async def test_briefing_section_case(pool, conn, case):
    space_id = ("br-" + case["name"].replace("_", "-"))[:60]
    _cleanup(conn, space_id)
    config = case["briefing"]
    label_to_id, by_id = _seed_space(
        conn, space_id, case.get("seed", {}), sections=config["sections"]
    )
    try:
        result = await briefing(
            pool, space_id, "p1", case.get("scope", "scope1"), case.get("hint"),
            "pytest", config, semantic_floor=case.get("floor"),
        )

        assert result["v"] == 1
        expect = case["expect"]
        assert [s["name"] for s in result["sections"]] == [s["name"] for s in expect["sections"]]
        for exp, got in zip(expect["sections"], result["sections"]):
            _assert_section(exp, got, by_id, conn, label_to_id)

        if "footer" in expect:
            assert result["footer"] == {"inbox_pending": expect["footer"]["inbox_pending"]}
        if expect.get("node_detail") == "full":
            for s in result["sections"]:
                assert all("body" in n for n in s["nodes"])
        if expect.get("linked_detail") == "full":
            for s in result["sections"]:
                for n in s["nodes"]:
                    assert all("body" in ln for ln in n.get("linked", []))
        if expect.get("audit_all"):
            cur = conn.cursor()
            cur.execute(
                "SELECT action, detail FROM audit_log WHERE space_id = %s AND action = 'briefing'",
                (space_id,),
            )
            rows = cur.fetchall()
            assert len(rows) == 1
            assert rows[0][1]["scope"] == "all"
    finally:
        _cleanup(conn, space_id)


async def test_briefing_merged_node_never_carries_attrs_addenda(pool, conn):
    """Q1: addenda never ships in `attrs` on the wire, from any tool -- briefing
    builds node dicts through search._node_envelope too, so a node with
    attrs.addenda set (mirroring dedup._do_merge's storage) must not leak it."""
    space_id = "br-q1-addenda-strip"
    _cleanup(conn, space_id)
    case_seed = {
        "nodes": [
            {"label": "D1", "type": "decision", "scope": "scope1",
             "title": "Pin dependencies", "body": "Always pin exact dependency versions."},
        ],
    }
    config = {"sections": [{"name": "standing_decisions", "type": "decision", "status": "active"}]}
    label_to_id, _by_id = _seed_space(conn, space_id, case_seed, sections=config["sections"])
    cur = conn.cursor()
    cur.execute(
        "UPDATE nodes SET attrs = %s WHERE id = %s",
        (Jsonb({"addenda": [{"body": "a reworded duplicate", "author_principal": "p1"}]}),
         label_to_id["D1"]),
    )
    conn.commit()
    try:
        result = await briefing(pool, space_id, "p1", "scope1", None, "pytest", config)
        section = result["sections"][0]
        assert len(section["nodes"]) == 1
        assert "addenda" not in section["nodes"][0]["attrs"]
    finally:
        _cleanup(conn, space_id)


async def test_example_briefing_byte_compare(pool, conn):
    """design/07 §Testing: the example pack's briefing byte-compares
    to its committed expected fixture. Hint-less (advisor fix #1), so the
    semantic section is [] and the label-mapped structure is deterministic."""
    space_id = "br-example-byte-compare"
    _cleanup(conn, space_id)
    pack = packs_mod.load_pack_file(PACKS_DIR / "example-pack.yaml")
    assert packs_mod.validate(pack) == [], "the example pack must be valid"

    _label_to_id, by_id = _seed_space(conn, space_id, EXAMPLE_PACK["seed"], pack=pack)
    try:
        result = await briefing(
            pool, space_id, "p1", EXAMPLE_PACK["scope"], EXAMPLE_PACK.get("hint"),
            "pytest", pack["briefing"],
        )

        got = {"sections": [], "footer": result["footer"]}
        for s in result["sections"]:
            entry = {"name": s["name"], "nodes": _section_labels(s, by_id)}
            linked = {
                by_id[n["id"]]: [by_id[ln["id"]] for ln in n["linked"]]
                for n in s["nodes"] if "linked" in n
            }
            if linked:
                entry["linked"] = linked
            got["sections"].append(entry)

        expect = EXAMPLE_PACK["expect"]
        want = {"sections": [], "footer": expect["footer"]}
        for s in expect["sections"]:
            entry = {"name": s["name"], "nodes": list(s["nodes"])}
            if "linked" in s:
                entry["linked"] = {k: list(v) for k, v in s["linked"].items()}
            want["sections"].append(entry)

        assert got == want  # the byte-compare, label-mapped

        # Detail levels: briefing is always FULL, linked included (07).
        for s in result["sections"]:
            for n in s["nodes"]:
                assert "body" in n
                for ln in n.get("linked", []):
                    assert "body" in ln
    finally:
        _cleanup(conn, space_id)


# ---- semantic_floor config resolution (mirrors the dedup config trio) --------
# briefing.semantic_floor: caller param > config row > 0.50 default; malformed
# config fails loud. PA's body sits at ~0.67 query<->doc cosine to the hint
# (baselined), so a 0.90 floor drops it and a 0.10 floor keeps it.

from engraphy.core.dedup import ConfigError  # noqa: E402

_SEM_SECTIONS = [{"name": "relevant", "semantic": True, "types": ["preference"], "top_k": 5}]
_SEM_CONFIG = {"sections": _SEM_SECTIONS, "footer": {}}
_SEM_HINT = "what do we know about descaling the office coffee machine"
_PA_SEED = {"nodes": [{"label": "PA", "type": "preference", "scope": "scope1",
                       "title": "Appliance maintenance cadence",
                       "body": "Kitchen appliances need monthly descaling to avoid limescale damage."}]}


def _set_config(conn, space_id, key, value):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO config (space_id, key, value) VALUES (%s, %s, %s) "
        "ON CONFLICT (space_id, key) DO UPDATE SET value = excluded.value",
        (space_id, key, Jsonb(value)),
    )
    conn.commit()


async def test_briefing_semantic_floor_config_row_applied(pool, conn):
    space_id = "br-sem-floor-config"
    _cleanup(conn, space_id)
    _seed_space(conn, space_id, _PA_SEED, sections=_SEM_SECTIONS)
    _set_config(conn, space_id, "briefing.semantic_floor", 0.90)  # above PA's ~0.67
    try:
        result = await briefing(pool, space_id, "p1", "scope1", _SEM_HINT, "pytest", _SEM_CONFIG)
        assert result["sections"][0]["nodes"] == [], "config floor 0.90 drops the ~0.67 node"
    finally:
        _cleanup(conn, space_id)


async def test_briefing_semantic_floor_caller_outranks_config(pool, conn):
    space_id = "br-sem-floor-caller"
    _cleanup(conn, space_id)
    _seed_space(conn, space_id, _PA_SEED, sections=_SEM_SECTIONS)
    _set_config(conn, space_id, "briefing.semantic_floor", 0.90)
    try:
        result = await briefing(
            pool, space_id, "p1", "scope1", _SEM_HINT, "pytest", _SEM_CONFIG, semantic_floor=0.10
        )
        assert len(result["sections"][0]["nodes"]) == 1, "caller 0.10 outranks config 0.90"
    finally:
        _cleanup(conn, space_id)


async def test_briefing_semantic_floor_malformed_config_fails_loud(pool, conn):
    space_id = "br-sem-floor-bad"
    _cleanup(conn, space_id)
    _seed_space(conn, space_id, _PA_SEED, sections=_SEM_SECTIONS)
    _set_config(conn, space_id, "briefing.semantic_floor", "not a number")
    try:
        with pytest.raises(ConfigError, match="ENGRAPHY_INTERNAL"):
            await briefing(pool, space_id, "p1", "scope1", _SEM_HINT, "pytest", _SEM_CONFIG)
    finally:
        _cleanup(conn, space_id)
