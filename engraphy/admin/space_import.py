"""Per-space import: replay a bundle from ``engraphy-admin space export`` into a
space on this engine, under a principal of this engine.

    engraphy-admin space import devon.jsonl --space devon --principal devon \\
        [--scope-map SRC=DST ...] [--no-create-scopes] [--dry-run]

Every node goes through the engine's own ``write`` pipeline, so each one is
written into the destination space, authored by the destination principal,
checked against the destination's scope writability and attr specs, embedded
with the destination's model, and deduplicated against what the destination
already holds. Every edge goes through the engine's own ``link``. Nothing from
the source engine's ``space_id``, principals or embeddings is carried across.

A bundle is replayed in five steps, after prechecks that write nothing:

1. **Prechecks.** The destination space and principal exist and the principal is
   not archived; every node type the bundle uses is registered in the
   destination space; every source scope maps to a destination scope that exists
   or can be created, and that the principal can write. Any failure stops the
   import before a row is written.
2. **Scopes.** A source scope maps to the destination scope of the same id,
   ``personal-<source owner>`` maps to ``personal-<principal>``, and
   ``--scope-map SRC=DST`` overrides either. A mapped scope that does not exist
   is created with the source scope's display name, visibility, ambient flag,
   hints and description, owned by the destination principal.
3. **Nodes, oldest first.** A node whose type, scope, title and body already
   exist in the destination is matched to that node and not written again, which
   makes a re-run a no-op whatever the node's status. Every other node goes
   through ``write``. When ``write`` parks a node because it is close to a node
   this import has already written, the source engine held both, so the import
   resolves it as distinct. When the node is close to content the destination
   held before the import, it stays parked for the principal to resolve, and it
   is listed in the review CSV. A source node with status ``merged`` is not
   written; its content lives on its canonical, and its edges move there.
4. **Edges.** Each source edge is remapped to the destination ids and attached
   with ``link``. An edge is skipped, and counted, when an endpoint was not
   written (a parked node), when both endpoints landed on one node, or when the
   destination declares no edge rule for its type and endpoint types.
5. **Statuses and reserved attrs.** On nodes this import created, the source's
   ``superseded`` and ``archived`` statuses and its reserved ``addenda`` and
   ``dropped`` attrs are restored, and an archived source scope is archived.
   Nodes that already existed in the destination are left as they were.

Re-running the same bundle writes nothing new: every node matches the node it
created the first time, and every edge is already present.

The structural steps (scope creation, prechecks, the content match, statuses and
reserved attrs) run on the operator connection. Node and edge writes run on
``pool`` under the destination principal, the same path every client uses.
"""

import csv
import dataclasses
import json
import pathlib
from collections.abc import Callable

import psycopg
from psycopg.types.json import Jsonb

from engraphy.admin.export_ import FORMAT, FORMAT_VERSION
from engraphy.core import embedding
from engraphy.core.attr_spec import RESERVED_ATTR_KEYS
from engraphy.core.dedup import resolve_attr_surface, resolve_duplicate, write
from engraphy.core.link import link
from engraphy.core.visibility import writable_scopes_async
from engraphy.server.db import transaction

SOURCE_CLIENT = "engraphy-admin space import"
_RESTORABLE_STATUSES = ("superseded", "archived")


class SpaceImportError(Exception):
    """The bundle is malformed, or a precheck failed. Nothing was written."""


@dataclasses.dataclass
class SpaceImportSummary:
    """What an import did. ``nodes_total`` counts every node line in the bundle."""

    dry_run: bool = False
    nodes_total: int = 0
    already_present: int = 0
    inserted: int = 0
    merged: int = 0
    merged_linked: int = 0
    resolved_distinct: int = 0
    left_for_review: int = 0
    merged_status_skipped: int = 0
    statuses_restored: int = 0
    reserved_attrs_restored: int = 0
    edges_total: int = 0
    edges_attached: int = 0
    edges_already_present: int = 0
    edges_skipped_unmapped: int = 0
    edges_skipped_same_node: int = 0
    edges_skipped_no_rule: int = 0
    edges_failed: int = 0
    scopes_created: list = dataclasses.field(default_factory=list)
    scope_map: dict = dataclasses.field(default_factory=dict)
    source_authors: dict = dataclasses.field(default_factory=dict)
    skipped_edge_samples: list = dataclasses.field(default_factory=list)
    review_path: "pathlib.Path | None" = None

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["review_path"] = str(self.review_path) if self.review_path else None
        return d


def read_bundle(path) -> tuple[dict, list[dict], list[dict], list[dict], list[dict]]:
    """Parse a bundle into (header, scopes, node_types, nodes, edges). Raises
    SpaceImportError naming the 1-based line on anything malformed."""
    path = pathlib.Path(path)
    header, scopes, types, nodes, edges = None, [], [], [], []
    buckets = {"scope": scopes, "node_type": types, "node": nodes, "edge": edges}
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SpaceImportError(f"line {lineno}: not valid JSON ({exc.msg})") from exc
            kind = obj.get("kind") if isinstance(obj, dict) else None
            if lineno == 1 or header is None:
                if kind != "header" or obj.get("format") != FORMAT:
                    raise SpaceImportError(
                        f"line {lineno}: not an Engraphy space export (expected a "
                        f"'{FORMAT}' header first)")
                if obj.get("v") != FORMAT_VERSION:
                    raise SpaceImportError(
                        f"line {lineno}: bundle format v{obj.get('v')} is not v{FORMAT_VERSION}")
                header = obj
                continue
            if kind not in buckets:
                raise SpaceImportError(f"line {lineno}: unknown line kind {kind!r}")
            buckets[kind].append(obj)
    if header is None:
        raise SpaceImportError("the bundle is empty")
    return header, scopes, types, nodes, edges


def _parse_scope_map(pairs: list[str] | None) -> dict:
    mapping = {}
    for pair in pairs or []:
        src, sep, dst = pair.partition("=")
        if not sep or not src or not dst:
            raise SpaceImportError(f"--scope-map expects SRC=DST, got {pair!r}")
        mapping[src] = dst
    return mapping


def _plan_scopes(scopes: list[dict], explicit: dict, principal: str) -> dict:
    """Source scope id -> destination scope id."""
    plan = {}
    for s in scopes:
        sid = s["id"]
        if sid in explicit:
            plan[sid] = explicit[sid]
        elif s.get("owner") and sid == f"personal-{s['owner']}":
            plan[sid] = f"personal-{principal}"
        else:
            plan[sid] = sid
    return plan


async def run_space_import(
    pool,
    operator_conninfo: str,
    space_id: str,
    principal: str,
    bundle_path,
    *,
    scope_map: list[str] | None = None,
    create_scopes: bool = True,
    dry_run: bool = False,
    review_path=None,
    embed_document: Callable[[str], list] = embedding.embed_document,
) -> SpaceImportSummary:
    """Replay ``bundle_path`` into ``space_id`` as ``principal``. See the module
    docstring for the steps and their guarantees."""
    bundle_path = pathlib.Path(bundle_path)
    _header, scopes, _types, nodes, edges = read_bundle(bundle_path)
    summary = SpaceImportSummary(dry_run=dry_run, nodes_total=len(nodes), edges_total=len(edges))
    for n in nodes:
        summary.source_authors[n["author"]] = summary.source_authors.get(n["author"], 0) + 1

    plan = _plan_scopes(scopes, _parse_scope_map(scope_map), principal)
    summary.scope_map = dict(plan)
    source_scope = {s["id"]: s for s in scopes}
    unplanned = sorted({n["scope"] for n in nodes} - set(plan))
    if unplanned:
        raise SpaceImportError(f"node(s) reference scope(s) with no scope line: {unplanned}")

    with psycopg.connect(operator_conninfo, autocommit=True) as op:
        cur = op.cursor()

        # ---- 1. prechecks: nothing is written until all of these pass ----
        cur.execute("SELECT 1 FROM spaces WHERE id = %s", (space_id,))
        if cur.fetchone() is None:
            raise SpaceImportError(f"space '{space_id}' does not exist on this engine")
        cur.execute("SELECT archived FROM principals WHERE space_id = %s AND id = %s",
                    (space_id, principal))
        row = cur.fetchone()
        if row is None:
            raise SpaceImportError(f"principal '{principal}' is not a member of space '{space_id}'")
        if row[0]:
            raise SpaceImportError(f"principal '{principal}' is archived in space '{space_id}'")

        cur.execute("SELECT name FROM node_types WHERE space_id = %s", (space_id,))
        registered = {r[0] for r in cur.fetchall()}
        missing_types = sorted({n["type"] for n in nodes} - registered)
        if missing_types:
            raise SpaceImportError(
                f"node type(s) not registered in space '{space_id}': {', '.join(missing_types)}. "
                f"Apply the same pack the source space uses (engraphy-admin pack apply) first.")

        cur.execute("SELECT id FROM scopes WHERE space_id = %s", (space_id,))
        existing_scopes = {r[0] for r in cur.fetchall()}
        to_create = sorted({dst for dst in plan.values() if dst not in existing_scopes})
        if to_create and not create_scopes:
            raise SpaceImportError(
                f"destination scope(s) do not exist and --no-create-scopes is set: "
                f"{', '.join(to_create)}")
        if dry_run:
            summary.scopes_created = to_create
            return summary

        # ---- 2. scopes ----
        dst_source = {}
        for src_id, dst_id in plan.items():
            dst_source.setdefault(dst_id, source_scope[src_id])
        for dst_id in to_create:
            s = dst_source[dst_id]
            cur.execute(
                "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility, "
                "ambient, hints, description) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (space_id, dst_id, s.get("display_name") or dst_id, principal,
                 s.get("visibility") or "private", bool(s.get("ambient")),
                 list(s.get("hints") or []), s.get("description")),
            )
        summary.scopes_created = to_create

    async with transaction(pool, space_id, principal) as conn:
        writable = set(await writable_scopes_async(conn.cursor()))
    unwritable = sorted(set(plan.values()) - writable)
    if unwritable:
        raise SpaceImportError(
            f"principal '{principal}' cannot write destination scope(s): {', '.join(unwritable)}")

    # ---- 3. nodes ----
    mapping: dict[str, str] = {}
    created: set[str] = set()
    touched: set[str] = set()
    review_rows = []
    surface_cache: dict[str, tuple[set, bool]] = {}

    with psycopg.connect(operator_conninfo, autocommit=True) as op:
        cur = op.cursor()
        for node in nodes:
            if node["status"] == "merged":
                summary.merged_status_skipped += 1
                continue
            dst_scope = plan[node["scope"]]
            cur.execute(
                "SELECT id FROM nodes WHERE space_id = %s AND type = %s AND scope_id = %s "
                "AND title = %s AND body = %s AND status <> 'merged' ORDER BY created_at LIMIT 1",
                (space_id, node["type"], dst_scope, node["title"], node["body"]),
            )
            hit = cur.fetchone()
            if hit is not None:
                mapping[node["id"]] = str(hit[0])
                touched.add(str(hit[0]))
                summary.already_present += 1
                continue

            attrs = {k: v for k, v in (node.get("attrs") or {}).items()
                     if k not in RESERVED_ATTR_KEYS}
            if node["type"] not in surface_cache:
                surface_cache[node["type"]] = await resolve_attr_surface(pool, space_id, node["type"])
            keys, on = surface_cache[node["type"]]
            extra = embedding.render_attr_surface(attrs, keys) if on else ""
            vector = embed_document(embedding.searchable_text(node["title"], node["body"], extra))
            env = await write(
                pool, space_id, principal, node["type"], dst_scope, node["title"],
                node["body"], attrs, vector, SOURCE_CLIENT,
                source_session=node.get("source_session"), extra_search=extra,
            )
            dst_id = _record(env, summary, created)
            if dst_id is None and env["outcome"] == "needs_confirmation":
                candidate_ids = {c["id"] for c in env.get("candidates", [])}
                if candidate_ids and candidate_ids <= touched:
                    resolved = await resolve_duplicate(
                        pool, space_id, principal, env["pending_id"], "distinct")
                    summary.resolved_distinct += 1
                    dst_id = _record(resolved, summary, created)
                else:
                    summary.left_for_review += 1
                    review_rows.append([env["pending_id"], node["type"], dst_scope,
                                        node["title"], ";".join(sorted(candidate_ids))])
            if dst_id is not None:
                mapping[node["id"]] = dst_id
                touched.add(dst_id)

        for node in nodes:
            if node["status"] == "merged" and node.get("canonical_id") in mapping:
                mapping[node["id"]] = mapping[node["canonical_id"]]

        # ---- 5a. reserved attrs, on nodes this import created ----
        for node in nodes:
            dst_id = mapping.get(node["id"])
            reserved = {k: v for k, v in (node.get("attrs") or {}).items() if k in RESERVED_ATTR_KEYS}
            if dst_id not in created or not reserved:
                continue
            cur.execute("SELECT attrs FROM nodes WHERE id = %s", (dst_id,))
            current = cur.fetchone()[0] or {}
            if "addenda" in reserved:
                current["addenda"] = reserved["addenda"]
            if isinstance(reserved.get("dropped"), dict):
                bucket = dict(reserved["dropped"])
                bucket.update(current.get("dropped") or {})
                current["dropped"] = bucket
            cur.execute("UPDATE nodes SET attrs = %s WHERE id = %s", (Jsonb(current), dst_id))
            summary.reserved_attrs_restored += 1

        # ---- 4. edges ----
        cur.execute("SELECT id::text, type FROM nodes WHERE space_id = %s AND id::text = ANY(%s)",
                    (space_id, sorted(set(mapping.values()))))
        dst_type = dict(cur.fetchall())
        rule_cache: dict[tuple, bool] = {}
        for edge in edges:
            s, d = mapping.get(edge["src"]), mapping.get(edge["dst"])
            if s is None or d is None:
                summary.edges_skipped_unmapped += 1
                continue
            if s == d:
                summary.edges_skipped_same_node += 1
                continue
            key = (edge["type"], dst_type[s], dst_type[d])
            if key not in rule_cache:
                cur.execute(
                    "SELECT 1 FROM edge_rules WHERE space_id = %s AND type = %s "
                    "AND src_type = %s AND dst_type = %s", (space_id, *key))
                rule_cache[key] = cur.fetchone() is not None
            if not rule_cache[key]:
                summary.edges_skipped_no_rule += 1
                if len(summary.skipped_edge_samples) < 10:
                    summary.skipped_edge_samples.append(
                        {"type": key[0], "src_type": key[1], "dst_type": key[2],
                         "reason": "no edge rule in the destination space"})
                continue
            try:
                res = await link(pool, space_id, principal,
                                 [{"type": edge["type"], "src_id": s, "dst_id": d}])
            except Exception as exc:  # noqa: BLE001 -- counted and sampled, never fatal
                summary.edges_failed += 1
                if len(summary.skipped_edge_samples) < 10:
                    summary.skipped_edge_samples.append(
                        {"type": edge["type"], "reason": str(exc)[:200]})
                continue
            summary.edges_attached += res["attached"]
            summary.edges_already_present += res["skipped"]

        # ---- 5b. statuses and archived scopes, last: link refuses archived nodes ----
        for node in nodes:
            dst_id = mapping.get(node["id"])
            if node["status"] in _RESTORABLE_STATUSES and dst_id in created:
                cur.execute("UPDATE nodes SET status = %s WHERE id = %s AND status = 'active'",
                            (node["status"], dst_id))
                summary.statuses_restored += cur.rowcount
        for dst_id in summary.scopes_created:
            if dst_source[dst_id].get("archived"):
                cur.execute("UPDATE scopes SET archived = true WHERE space_id = %s AND id = %s",
                            (space_id, dst_id))

    if review_rows:
        path = pathlib.Path(review_path) if review_path else bundle_path.with_suffix(
            bundle_path.suffix + ".review.csv")
        with path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["pending_id", "type", "scope", "title", "candidate_ids"])
            w.writerows(review_rows)
        summary.review_path = path
    return summary


def _record(env: dict, summary: SpaceImportSummary, created: set) -> str | None:
    """The destination node id a write or resolve envelope names, tallied."""
    outcome = env["outcome"]
    if outcome == "inserted":
        summary.inserted += 1
        created.add(env["node"]["id"])
        return env["node"]["id"]
    if outcome == "merged":
        summary.merged += 1
        return env["canonical"]["id"]
    if outcome == "merged_linked":
        summary.merged_linked += 1
        created.add(env["node"]["id"])
        return env["node"]["id"]
    return None
