"""doctor: stale pendings, attrs_nonconforming counts, registry-vs-pack drift,
orphaned merges, canonical chains >3, nodes with >20 addenda (design/04 s.Hygiene).

attrs_nonconforming has no DB column (DECISIONS-DELTA.md "attrs_nonconforming is
derived, not stored"): it's computed here on demand from engraphy_validate_attrs()
against each active row's CURRENT node_types.attr_spec. This is possible because
the write-path trigger (nodes_validate_attrs, 0006_attr_spec_and_triggers.sql)
already enforces the CURRENT spec on every INSERT/UPDATE -- a nonconforming row
can only be a grandfathered one from before a tightening `pack upgrade`, and any
`update` that doesn't fix it will re-fail the same trigger. Read-only throughout:
doctor never mutates the space it inspects.
"""
import pathlib

from engraphy.admin import packs
from engraphy.core.sentinel import SENTINEL_NODE_TYPE


class DoctorError(Exception):
    """A check could not run (bad --pack-file, unknown space, etc.)."""


def _stale_pendings(cur, space_id: str) -> list[str]:
    # pending_writes.expires_at is the dedup handshake's forced-choice window
    # (design/02); a row still present after it has passed means the
    # handshake was never resolved -- that's "stale" (not dedup_log's
    # band='pending', which is an append-only log of every encounter, not
    # outstanding state).
    cur.execute(
        "SELECT count(*) FROM pending_writes WHERE space_id = %s AND expires_at < now()",
        (space_id,),
    )
    (count,) = cur.fetchone()
    return [f"stale pendings (expired, unresolved): {count}"]


def _attrs_nonconforming(cur, space_id: str) -> list[str]:
    cur.execute(
        "SELECT n.type, count(*) FROM nodes n "
        "JOIN node_types nt ON nt.space_id = n.space_id AND nt.name = n.type "
        "WHERE n.space_id = %s AND n.status = 'active' "
        "AND cardinality(engraphy_validate_attrs(nt.attr_spec, n.attrs)) > 0 "
        "GROUP BY n.type ORDER BY n.type",
        (space_id,),
    )
    rows = cur.fetchall()
    total = sum(count for _, count in rows)
    lines = [f"attrs_nonconforming: {total}"]
    lines.extend(f"  type '{type_}': {count}" for type_, count in rows)
    return lines


def _orphaned_merges(cur, space_id: str) -> list[str]:
    # A merge whose canonical target is itself not active (already
    # merged/archived/superseded further along) -- the chain moved on and
    # this row's canonical_id no longer points at the authoritative node.
    cur.execute(
        "SELECT count(*) FROM nodes n JOIN nodes c ON c.id = n.canonical_id "
        "WHERE n.space_id = %s AND n.status = 'merged' AND c.status != 'active'",
        (space_id,),
    )
    (count,) = cur.fetchone()
    return [f"orphaned merges (canonical target not active): {count}"]


def _long_canonical_chains(cur, space_id: str, max_depth: int = 3) -> list[str]:
    # depth = hop count along canonical_id while the target stays 'merged';
    # capped at 50 hops as a defensive guard against a cyclic chain (a data
    # bug, not an expected shape -- the CHECK/FK don't forbid a cycle).
    cur.execute(
        """
        WITH RECURSIVE chain(start_id, current_id, depth) AS (
            SELECT id, canonical_id, 1 FROM nodes
            WHERE space_id = %(space_id)s AND status = 'merged'
            UNION ALL
            SELECT chain.start_id, n.canonical_id, chain.depth + 1
            FROM chain JOIN nodes n ON n.id = chain.current_id
            WHERE n.status = 'merged' AND chain.depth < 50
        )
        SELECT start_id, max(depth) FROM chain GROUP BY start_id
        HAVING max(depth) > %(max_depth)s ORDER BY start_id
        """,
        {"space_id": space_id, "max_depth": max_depth},
    )
    rows = cur.fetchall()
    lines = [f"canonical chains >{max_depth} hops: {len(rows)}"]
    lines.extend(f"  node {node_id}: {depth} hops" for node_id, depth in rows)
    return lines


def _high_addenda(cur, space_id: str, threshold: int = 20) -> list[str]:
    cur.execute(
        "SELECT id, jsonb_array_length(attrs -> 'addenda') AS n FROM nodes "
        "WHERE space_id = %s AND attrs ? 'addenda' AND jsonb_array_length(attrs -> 'addenda') > %s "
        "ORDER BY n DESC",
        (space_id, threshold),
    )
    rows = cur.fetchall()
    lines = [f"nodes with >{threshold} addenda: {len(rows)}"]
    lines.extend(f"  node {node_id}: {count} addenda" for node_id, count in rows)
    return lines


def _registry_drift(cur, space_id: str, pack_file: pathlib.Path | None) -> list[str]:
    """Compares the space's DB registry against what applying `pack_file`
    would produce (packs.apply/upgrade are the ONLY writers of node_types/
    edge_types/edge_rules -- design/04: "hand-edited registry rows are
    unsupported and flagged by doctor"). Needs the pack file itself; the DB
    only records pack_name/pack_version (design/04's own gap -- content isn't
    stored), so this can't run without a local copy. Convention:
    packs/<pack_name>/pack.yaml relative to cwd, overridable via --pack-file;
    skipped (not failed) when no file is found, since a pack the operator
    doesn't have locally (a pack may be defined in another repo) is a legitimate
    state, not drift."""
    cur.execute("SELECT pack_name, pack_version FROM spaces WHERE id = %s", (space_id,))
    pack_name, pack_version = cur.fetchone()  # run() already confirmed the space exists
    if pack_name is None:
        return ["registry-vs-pack drift: skipped (no pack applied to this space)"]

    path = pack_file or pathlib.Path("packs") / pack_name / "pack.yaml"
    if not path.exists():
        return [f"registry-vs-pack drift: skipped (pack file not found at {path}; pass --pack-file)"]

    pack = packs.load_pack_file(path)
    errors = packs.validate(pack)
    if errors:
        raise DoctorError(f"--pack-file {path} is not a valid pack: {errors[0]}")

    pack_node_types = {
        name: {"attrs": spec.get("attrs", {}) or {}} for name, spec in pack.get("node_types", {}).items()
    }
    pack_edge_types = {
        name: spec.get("bidirectional", False) for name, spec in pack.get("edge_types", {}).items()
    }
    pack_edge_rules = packs.expand_edge_rules(pack)

    db_node_types, db_edge_types, db_edge_rules = packs.current_registry(space_id, cur)
    # design/04 s.Backup contract: the engine-owned `engraphy_sentinel` type is
    # registered by `space create`, not by a pack, so its presence in the DB and
    # absence from the pack file is the CORRECT state -- reporting it as drift
    # would put a permanent, unfixable line in every healthy instance's doctor
    # output, which is how operators learn to ignore doctor.
    db_node_types = {n: s for n, s in db_node_types.items() if n != SENTINEL_NODE_TYPE}

    lines: list[str] = []
    extra_node_types = db_node_types.keys() - pack_node_types.keys()
    missing_node_types = pack_node_types.keys() - db_node_types.keys()
    changed_node_types = {
        name for name in db_node_types.keys() & pack_node_types.keys()
        if db_node_types[name] != pack_node_types[name]
    }
    extra_edge_types = db_edge_types.keys() - pack_edge_types.keys()
    missing_edge_types = pack_edge_types.keys() - db_edge_types.keys()
    extra_edge_rules = db_edge_rules - pack_edge_rules
    missing_edge_rules = pack_edge_rules - db_edge_rules

    drift_count = (len(extra_node_types) + len(missing_node_types) + len(changed_node_types)
                   + len(extra_edge_types) + len(missing_edge_types)
                   + len(extra_edge_rules) + len(missing_edge_rules))
    lines.append(f"registry-vs-pack drift (against {path}, pack version {pack_version}): {drift_count}")
    lines.extend(f"  node type '{n}' in DB but not in pack file (hand-edited?)" for n in sorted(extra_node_types))
    lines.extend(f"  node type '{n}' in pack file but not applied to DB" for n in sorted(missing_node_types))
    lines.extend(f"  node type '{n}' attr_spec differs from pack file" for n in sorted(changed_node_types))
    lines.extend(f"  edge type '{n}' in DB but not in pack file (hand-edited?)" for n in sorted(extra_edge_types))
    lines.extend(f"  edge type '{n}' in pack file but not applied to DB" for n in sorted(missing_edge_types))
    lines.extend(f"  edge rule '{t}' {s} -> {d} in DB but not in pack file" for t, s, d in sorted(extra_edge_rules))
    lines.extend(f"  edge rule '{t}' {s} -> {d} in pack file but not applied" for t, s, d in sorted(missing_edge_rules))
    return lines


def run(cur, space_id: str, *, pack_file: pathlib.Path | None = None) -> list[str]:
    cur.execute("SELECT 1 FROM spaces WHERE id = %s", (space_id,))
    if cur.fetchone() is None:
        raise DoctorError(f"space '{space_id}' does not exist")

    report = [f"doctor report for space '{space_id}':"]
    report.extend(_stale_pendings(cur, space_id))
    report.extend(_attrs_nonconforming(cur, space_id))
    report.extend(_registry_drift(cur, space_id, pack_file))
    report.extend(_orphaned_merges(cur, space_id))
    report.extend(_long_canonical_chains(cur, space_id))
    report.extend(_high_addenda(cur, space_id))
    return report
