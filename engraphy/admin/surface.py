"""`engraphy-admin surface rebuild` -- recompute nodes.extra_search and re-embed
(fact-searchability-phase-c.md §4). Local CLI-only, like `import` and `addenda
promote`.

Phase C's migration 0019 adds `extra_search` with DEFAULT '' -- so existing rows
keep their pre-C (title+body) embedding until this command recomputes the render
from each node's attrs and, for rows whose render changed, re-embeds
searchable_text(title, body, extra). Idempotent by construction: the render is
STORED, so a re-run compares `new_extra == extra_search` and skips equal rows for
free; a crashed run resumes exactly where it left off (each row self-describes);
a full re-run after completion touches zero rows.

Embeddings are computed OUTSIDE the per-batch transaction (trap 3). `updated_at`
is preserved (migration 0020: an embedding/extra_search change with unchanged
semantic content is a re-index, not a content edit). All statuses are rebuilt so
`include_inactive` search never resurrects a stale-surface vector -- except the
engine's reserved `engram_sentinel`, whose embedding is a constant by contract
(design/04) and must never be recomputed.
"""
from dataclasses import dataclass

from engraphy.core import embedding
from engraphy.core.sentinel import SENTINEL_NODE_TYPE

from engraphy.admin.addenda import _surface_for_type

_BATCH = 100


@dataclass
class RebuildSummary:
    dry_run: bool = False
    scanned: int = 0
    skipped_equal: int = 0
    re_embedded: int = 0
    per_scope: dict = None

    def __post_init__(self):
        if self.per_scope is None:
            self.per_scope = {}

    def as_line(self) -> str:
        mode = "dry-run: would re-embed" if self.dry_run else "re-embedded"
        scopes = ", ".join(f"{s}:{n}" for s, n in sorted(self.per_scope.items())) or "-"
        return (
            f"surface rebuild ({'dry-run' if self.dry_run else 'applied'}): "
            f"{self.scanned} scanned, {self.skipped_equal} skipped (render unchanged), "
            f"{mode} {self.re_embedded}. per-scope re-embedded: {scopes}"
        )


def _vector_literal(vec) -> str:
    return "[" + ",".join(str(x) for x in vec) + "]"


def rebuild_surface(
    conn,
    space_id: str,
    scope_id: str | None = None,
    *,
    dry_run: bool = False,
    embed_document=embedding.embed_document,
) -> RebuildSummary:
    """Recompute extra_search + re-embed changed rows in `space_id` (optionally one
    scope). Runs on `conn` (a privileged connection). `embed_document` is injectable
    for tests. Returns a RebuildSummary."""
    summary = RebuildSummary(dry_run=dry_run)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, type, scope_id, title, body, attrs, extra_search FROM nodes "
        "WHERE space_id = %s AND type <> %s AND (%s::text IS NULL OR scope_id = %s) "
        "ORDER BY id",
        (space_id, SENTINEL_NODE_TYPE, scope_id, scope_id),
    )
    rows = cur.fetchall()
    conn.commit()  # close the read transaction before embedding (trap 3)

    surface_cache: dict[str, tuple[set, bool]] = {}
    batch: list = []

    def _flush():
        if not batch:
            return
        # embeddings OUTSIDE the transaction.
        embedded = [
            (nid, scope, new_extra,
             embed_document(embedding.searchable_text(title, body, new_extra)))
            for (nid, scope, title, body, new_extra) in batch
        ]
        if not dry_run:
            with conn.transaction():
                for nid, _scope, new_extra, vector in embedded:
                    cur.execute(
                        "UPDATE nodes SET extra_search = %s, embedding = %s::vector, "
                        "embedding_model = %s WHERE id = %s",
                        (new_extra, _vector_literal(vector), embedding.MODEL_ID, nid),
                    )
        for _nid, scope, _extra, _vec in embedded:
            summary.re_embedded += 1
            summary.per_scope[scope] = summary.per_scope.get(scope, 0) + 1
        batch.clear()

    for nid, node_type, scope, title, body, attrs, stored_extra in rows:
        summary.scanned += 1
        if node_type not in surface_cache:
            surface_cache[node_type] = _surface_for_type(cur, space_id, node_type)
            conn.commit()  # keep the read cursor out of an open write txn
        keys, on = surface_cache[node_type]
        new_extra = embedding.render_attr_surface(attrs or {}, keys) if on else ""
        if new_extra == stored_extra:
            summary.skipped_equal += 1
            continue
        batch.append((nid, scope, title, body, new_extra))
        if len(batch) >= _BATCH:
            _flush()
    _flush()
    return summary
