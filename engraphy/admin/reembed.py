"""`engraphy-admin reembed` -- rewrite stored vectors into the active embedding
profile's vector space.

Needed exactly once, when a store moves onto a profile whose vectors are not
interchangeable with the ones already written. Today that means moving onto
`onnx-int8`: quantization shifts pairwise cosine, so until every row is rewritten
the write path bands an int8 vector against fp32 neighbours, which is a comparison
across two spaces and lands on the wrong side of `t_high` for near-duplicates.

Moving between `legacy-torch` and `onnx-fp32` needs nothing. Those two produce
interchangeable vectors and therefore share a stamp, so this command correctly
finds no work and exits.

**Resumable and idempotent by construction.** Selection is
`embedding_model <> MODEL_STAMP`, and each row's stamp is written in the same
statement as its vector, so a row is either fully converted or untouched. A killed
run resumes where it stopped; a completed run finds nothing.

Two properties that are easy to get wrong and are load-bearing here:

* **One text per embed call.** The int8 graph is not batch-invariant, so a batched
  encode produces vectors the write path would not have produced for the same
  text. This walks rows one at a time through the same `embed_document` the
  server uses.
* **Embeddings computed OUTSIDE the write transaction** (trap 3, the same rule the
  write path and `surface rebuild` follow, CI-grepped).

`updated_at` is preserved: migration 0020 classifies an embedding-only change as a
re-index rather than a content edit, which is exactly what this is. The reserved
`engraphy_sentinel` is skipped, because its embedding is a constant by contract
(design/04) and verify-restore compares it.
"""
from dataclasses import dataclass, field

from engraphy.core import embedding
from engraphy.core.sentinel import SENTINEL_NODE_TYPE

_BATCH = 100


@dataclass
class ReembedSummary:
    dry_run: bool = False
    target_stamp: str = ""
    scanned: int = 0
    already_current: int = 0
    re_embedded: int = 0
    per_scope: dict = field(default_factory=dict)

    def as_line(self) -> str:
        verb = "would re-embed" if self.dry_run else "re-embedded"
        scopes = ", ".join(f"{s}:{n}" for s, n in sorted(self.per_scope.items())) or "-"
        return (
            f"reembed ({'dry-run' if self.dry_run else 'applied'}) "
            f"-> {self.target_stamp}: {self.scanned} scanned, "
            f"{self.already_current} already current, {verb} {self.re_embedded}. "
            f"per-scope: {scopes}"
        )


def _vector_literal(vec) -> str:
    return "[" + ",".join(str(x) for x in vec) + "]"


def reembed_space(
    conn,
    space_id: str,
    scope_id: str | None = None,
    *,
    dry_run: bool = False,
    embed_document=None,
    target_stamp: str | None = None,
    progress=None,
) -> ReembedSummary:
    """Re-embed every row in `space_id` (optionally one scope) that is not already
    in the active profile's vector space.

    `conn` is a privileged connection. `embed_document` and `target_stamp` are
    injectable so the tests can drive this without loading a model.
    """
    embed_document = embed_document or embedding.embed_document
    target_stamp = target_stamp or embedding.MODEL_STAMP
    summary = ReembedSummary(dry_run=dry_run, target_stamp=target_stamp)

    cur = conn.cursor()
    cur.execute(
        "SELECT id, scope_id, title, body, extra_search, embedding_model FROM nodes "
        "WHERE space_id = %s AND type <> %s AND (%s::text IS NULL OR scope_id = %s) "
        "ORDER BY id",
        (space_id, SENTINEL_NODE_TYPE, scope_id, scope_id),
    )
    rows = cur.fetchall()
    conn.commit()  # close the read transaction before embedding (trap 3)

    batch: list = []

    def _flush():
        if not batch:
            return
        # Embeddings OUTSIDE the transaction, one text per call: the int8 graph is
        # not batch-invariant, so a batched encode would write vectors that differ
        # from what the write path produces for the same node.
        embedded = [
            (nid, scope, embed_document(embedding.searchable_text(title, body, extra or "")))
            for (nid, scope, title, body, extra) in batch
        ]
        if not dry_run:
            with conn.transaction():
                for nid, _scope, vector in embedded:
                    cur.execute(
                        "UPDATE nodes SET embedding = %s::vector, embedding_model = %s "
                        "WHERE id = %s",
                        (_vector_literal(vector), target_stamp, nid),
                    )
        for _nid, scope, _vec in embedded:
            summary.re_embedded += 1
            summary.per_scope[scope] = summary.per_scope.get(scope, 0) + 1
        if progress:
            progress(summary.re_embedded, summary.scanned)
        batch.clear()

    for nid, scope, title, body, extra, stamp in rows:
        summary.scanned += 1
        if stamp == target_stamp:
            summary.already_current += 1
            continue
        batch.append((nid, scope, title, body, extra))
        if len(batch) >= _BATCH:
            _flush()
    _flush()
    return summary
