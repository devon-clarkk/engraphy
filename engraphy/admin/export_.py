"""Per-space export: one space's memory as a JSONL bundle that
``engraphy-admin space import`` replays into another engine.

    engraphy-admin space export --space devon --out devon.jsonl [--scope ID ...]

The bundle carries what a memory is made of: scopes (with their visibility,
ambient flag, hints, description and archived flag), the node types the nodes
use, every node (type, scope, title, body, attrs, status, canonical_id, author,
session, created_at) and every edge whose two endpoints are both exported. It
carries no embeddings: the importing engine embeds each node with its own model,
so a bundle moves between engines on different embedding profiles. It carries no
tokens, principals' credentials, pending writes, inbox items, audit rows or
metrics.

Read-only by construction. The connection opens with
``default_transaction_read_only=on``, so PostgreSQL itself refuses any write on
it, and every statement here is a SELECT. Exporting a live engine changes
nothing in it.

The export runs on the operator connection (``ENGRAPHY_DATABASE_URL``), which
must bypass row-level security: a role that does not would see only the rows
its policies allow, and the bundle would be silently incomplete. The export
checks the role first and refuses one that does not bypass RLS.

Bundle format, one JSON object per line, in this order:

* ``{"kind": "header", "format": "engraphy.space-export", "v": 1, ...}``
* ``{"kind": "scope", ...}`` for each exported scope
* ``{"kind": "node_type", "name", "description", "attr_spec"}``
* ``{"kind": "node", "id", "type", "scope", "title", "body", "attrs", "status",
  "canonical_id", "author", "source_session", "created_at"}``, oldest first
* ``{"kind": "edge", "src", "dst", "type"}``, oldest first
"""

import datetime
import json
import pathlib

import psycopg

import engraphy
from engraphy.core.sentinel import RESERVED_NODE_TYPES

FORMAT = "engraphy.space-export"
FORMAT_VERSION = 1


class ExportError(Exception):
    """The export cannot run as asked: an unknown space or scope, or a
    connection role that does not bypass row-level security."""


def open_readonly(conninfo: str) -> psycopg.Connection:
    """A connection on which the server refuses every write."""
    return psycopg.connect(conninfo, options="-c default_transaction_read_only=on")


def _check_bypasses_rls(cur) -> None:
    cur.execute(
        "SELECT current_user, rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )
    role, bypasses = cur.fetchone()
    if not bypasses:
        raise ExportError(
            f"role '{role}' does not bypass row-level security, so an export on it would "
            f"omit every row its policies hide. Run the export on the operator connection "
            f"(ENGRAPHY_DATABASE_URL)."
        )


def _iso(value) -> str | None:
    return value.isoformat() if isinstance(value, datetime.datetime) else value


def export_space(conninfo: str, space_id: str, out_path, scopes: list[str] | None = None) -> dict:
    """Write ``space_id``'s bundle to ``out_path`` and return the header's counts.

    ``scopes`` limits the export to those scope ids. Edges are kept only when
    both endpoints are exported, so a scope-limited bundle never points at a
    node it does not carry."""
    out_path = pathlib.Path(out_path)
    reserved = sorted(RESERVED_NODE_TYPES)
    with open_readonly(conninfo) as conn:
        cur = conn.cursor()
        _check_bypasses_rls(cur)

        cur.execute("SELECT 1 FROM spaces WHERE id = %s", (space_id,))
        if cur.fetchone() is None:
            raise ExportError(f"space '{space_id}' does not exist on this engine")

        cur.execute(
            "SELECT id, display_name, owner_principal, visibility, ambient, hints, "
            "description, archived FROM scopes WHERE space_id = %s ORDER BY id",
            (space_id,),
        )
        scope_rows = cur.fetchall()
        known = {r[0] for r in scope_rows}
        if scopes:
            unknown = sorted(set(scopes) - known)
            if unknown:
                raise ExportError(f"scope(s) not in space '{space_id}': {', '.join(unknown)}")
            scope_rows = [r for r in scope_rows if r[0] in set(scopes)]
        scope_ids = [r[0] for r in scope_rows]

        cur.execute(
            "SELECT id, type, scope_id, title, body, attrs, status, canonical_id, "
            "author_principal, source_session, created_at FROM nodes "
            "WHERE space_id = %s AND scope_id = ANY(%s) AND NOT (type = ANY(%s)) "
            "ORDER BY created_at, id",
            (space_id, scope_ids, reserved),
        )
        node_rows = cur.fetchall()
        node_ids = {r[0] for r in node_rows}
        used_types = sorted({r[1] for r in node_rows})

        cur.execute(
            "SELECT name, description, attr_spec FROM node_types "
            "WHERE space_id = %s AND name = ANY(%s) ORDER BY name",
            (space_id, used_types),
        )
        type_rows = cur.fetchall()

        cur.execute(
            "SELECT src_id, dst_id, type FROM edges WHERE space_id = %s ORDER BY created_at, id",
            (space_id,),
        )
        edge_rows = [r for r in cur.fetchall() if r[0] in node_ids and r[1] in node_ids]

        cur.execute("SELECT max(version) FROM schema_migrations")
        schema_version = cur.fetchone()[0]

    counts = {
        "scopes": len(scope_rows),
        "node_types": len(type_rows),
        "nodes": len(node_rows),
        "edges": len(edge_rows),
    }
    header = {
        "kind": "header",
        "format": FORMAT,
        "v": FORMAT_VERSION,
        "source_space": space_id,
        "scopes": scope_ids,
        "exported_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "engine_version": engraphy.__version__,
        "schema_version": schema_version,
        "counts": counts,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as fh:
        def emit(obj):
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

        emit(header)
        for sid, display, owner, visibility, ambient, hints, description, archived in scope_rows:
            emit({"kind": "scope", "id": sid, "display_name": display, "owner": owner,
                  "visibility": visibility, "ambient": ambient, "hints": list(hints or []),
                  "description": description, "archived": archived})
        for name, description, attr_spec in type_rows:
            emit({"kind": "node_type", "name": name, "description": description,
                  "attr_spec": attr_spec})
        for (nid, ntype, sid, title, body, attrs, status, canonical_id,
             author, session, created_at) in node_rows:
            emit({"kind": "node", "id": str(nid), "type": ntype, "scope": sid,
                  "title": title, "body": body, "attrs": attrs or {}, "status": status,
                  "canonical_id": str(canonical_id) if canonical_id else None,
                  "author": author, "source_session": session,
                  "created_at": _iso(created_at)})
        for src, dst, etype in edge_rows:
            emit({"kind": "edge", "src": str(src), "dst": str(dst), "type": etype})
    return counts
