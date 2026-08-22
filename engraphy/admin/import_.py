"""Bulk import (design/02 s.Bulk import): a JSONL batch driven through the standard
write pipeline in import mode, with the review-queue report written to CSV.
Idempotent by construction.

    engraphy-admin import --space X --scope Y --principal Z file.jsonl

The file is lines of ``{type, title, body, attrs?, links?}`` -- the agent (or a
one-off exporter) produces it from exported conversations; "extraction quality is
the exporter's problem, idempotency is Engraphy's" (02). Every line runs the SAME
pipeline as ``write`` with ``import_mode=True`` -- there is no second write path
here (dedup-write-path-plan.md trap 6). The two import-mode differences are the
engine's, not this module's:

* AUTO-MERGE (>= t_high) absorbs silently -- the merge, addendum, dedup_log and
  audit_log rows all happen; only the per-item report is dropped.
* PENDING ([t_low, t_high)) is NOT parked for interactive resolution -- the engine
  hands back a ``review_queued`` envelope and this module appends it to the review
  CSV ("incoming, candidate, similarity", 02).

So an import can never spray near-duplicates, and re-running the same file is a
no-op: every item hits >= 0.95 against the node it created the first time and
absorbs silently (02). That idempotency is a *property of the pipeline*, proven at
the ``write`` level in test_dedup.py; this module's tests prove it end-to-end over
a real file.

Judgement calls (recorded in DECISIONS-DELTA.md 2026-07-18):

* ``--principal`` is a required argument. The design's example line
  (``import --space X --scope Y file.jsonl``) omits it, but the write path is
  RLS-enforced: every node needs an ``author_principal`` and the target scope must
  be *writable* by it. There is no designed "system principal", so the operator
  names a real one. A boring gap, not a behavioral one.
* ``--scope`` names a scope *id* (the ``scopes.id`` primary key), consistent with
  how every other engine call takes a scope_id; no display-name lookup.
* Items are processed sequentially, one ``write`` transaction each. The design's
  "in batches" is a throughput note ("embeddings dominate; ~50ms/item"), not an
  atomicity contract -- and the per-(space, type) advisory lock serializes writes
  anyway, so concurrency would buy nothing at import's write shape. ``batch_size``
  gates only how many items are embedded before the first is written, keeping
  peak memory bounded on large files.
* Review CSV columns expand the design's three conceptual fields
  (incoming / candidate / similarity) into ``incoming_title, incoming_body,
  candidate_id, candidate_title, candidate_body, similarity`` so the report is
  actionable without a second lookup. Written only when >= 1 item is review-queued.

Admin-CLI only: import "must never be reachable through an agent's token" (02), so
this lives under ``engraphy/admin`` and is never an MCP tool. The Typer verb that
wraps ``run_import`` is part of the E2 admin surface (engraphy/admin/cli.py); this
module is the importable engine that verb and the tests both drive.
"""

import csv
import dataclasses
import json
import pathlib
from collections.abc import Callable, Iterator

from engraphy.core import embedding
from engraphy.core.dedup import resolve_attr_surface, write

# Line shape (02 s.Bulk import). attrs/links optional; everything else required and
# non-empty. Anything past these is a malformed export line -- reported, not guessed.
_REQUIRED_FIELDS = ("type", "title", "body")
_ALLOWED_FIELDS = frozenset(_REQUIRED_FIELDS) | {"attrs", "links"}

_REVIEW_CSV_HEADER = [
    "incoming_title",
    "incoming_body",
    "candidate_id",
    "candidate_title",
    "candidate_body",
    "similarity",
]


class ImportLineError(ValueError):
    """A malformed JSONL line -- names the 1-based line number so the operator can
    fix the export and re-run (re-running is safe: idempotent by construction)."""


@dataclasses.dataclass
class ImportSummary:
    """What an import run did, by pipeline outcome. ``total`` == inserted +
    merged + merged_linked + review_queued; ``review_queued`` rows are also in the
    CSV at ``review_queue_path`` (None when nothing was queued).

    ``merged_addendum_dropped`` is a SUBSET of ``merged``, not a separate outcome,
    so it is deliberately excluded from the ``total`` identity.

    ``merged_linked`` (Phase B) is a distinct terminal outcome: the incoming body
    was novel against the cluster, so it was inserted as its own searchable member
    row and linked to the canonical -- NOT a loss, and by construction never a
    dropped addendum."""

    total: int = 0
    inserted: int = 0
    merged: int = 0
    merged_linked: int = 0
    review_queued: int = 0
    #: Merges whose incoming body was discarded entirely -- the Jaccard novelty
    #: check found it >= 0.8 similar to the canonical plus its addenda, so no
    #: addendum was appended and the row left no trace but this count (ruled
    #: 2026-07-21, the dupstream contradiction finding; design/05's E5 backfill
    #: caveat). It matters most where it is least visible: a near-verbatim
    #: NEGATION ("... no longer works as a paediatric nurse") is exactly the
    #: shape that trips the novelty check, so a chronologically-ordered backfill
    #: can silently destroy its own corrections, with nobody in the loop to read
    #: an envelope. Non-zero means: re-read the export before trusting the store.
    merged_addendum_dropped: int = 0
    review_queue_path: "pathlib.Path | None" = None

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "inserted": self.inserted,
            "merged": self.merged,
            "merged_linked": self.merged_linked,
            "review_queued": self.review_queued,
            "merged_addendum_dropped": self.merged_addendum_dropped,
            "review_queue_path": str(self.review_queue_path) if self.review_queue_path else None,
        }


def parse_jsonl(path) -> Iterator[dict]:
    """Yield one validated item dict per non-blank line. Raises ImportLineError
    (with the 1-based line number) on invalid JSON, a non-object line, a missing or
    empty required field, or an unknown key -- so a bad export fails loudly before
    any row is written rather than silently importing garbage."""
    path = pathlib.Path(path)
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            if not raw.strip():
                continue  # blank lines are ignorable formatting, not data
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ImportLineError(f"line {lineno}: not valid JSON ({exc.msg})") from exc
            if not isinstance(item, dict):
                raise ImportLineError(f"line {lineno}: expected a JSON object, got {type(item).__name__}")
            unknown = set(item) - _ALLOWED_FIELDS
            if unknown:
                raise ImportLineError(f"line {lineno}: unknown field(s) {sorted(unknown)}")
            for field in _REQUIRED_FIELDS:
                value = item.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ImportLineError(f"line {lineno}: missing or empty required field '{field}'")
            if "attrs" in item and not isinstance(item["attrs"], dict):
                raise ImportLineError(f"line {lineno}: 'attrs' must be an object")
            if "links" in item and not isinstance(item["links"], list):
                raise ImportLineError(f"line {lineno}: 'links' must be a list")
            yield item


def _default_review_queue_path(jsonl_path: pathlib.Path) -> pathlib.Path:
    """Report next to the source file: ``<name>.review_queue.csv``."""
    return jsonl_path.with_suffix(jsonl_path.suffix + ".review_queue.csv")


async def run_import(
    pool,
    space_id: str,
    principal: str,
    scope_id: str,
    jsonl_path,
    *,
    source_client: str = "engraphy-admin import",
    review_queue_path=None,
    batch_size: int = 100,
    embed_document: Callable[[str], list] = embedding.embed_document,
) -> ImportSummary:
    """Drive ``jsonl_path`` through the write pipeline in import mode and return a
    per-outcome summary, writing the review-queue CSV if any item lands in the
    PENDING band.

    ``embed_document`` is injectable so callers (and tests) can supply a
    deterministic vector; production passes the default. It is called OUTSIDE the
    write transaction (write() embeds nothing itself -- it takes the vector), which
    keeps the "no embed() inside a transaction" invariant intact (trap 3).
    """
    jsonl_path = pathlib.Path(jsonl_path)
    if review_queue_path is None:
        review_queue_path = _default_review_queue_path(jsonl_path)
    else:
        review_queue_path = pathlib.Path(review_queue_path)

    summary = ImportSummary()
    review_rows = []

    # Phase C: the searchable attr surface, resolved per node type (keys) + once
    # per space (the write.attr_surface flag). Cached across items -- the registry
    # is static for the run. The injected `embed_document` is preserved: it embeds
    # searchable_text(title, body, extra), so a test's deterministic stub still
    # governs the vector.
    surface_cache: dict[str, tuple[set, bool]] = {}

    async def _surface(node_type: str) -> tuple[set, bool]:
        if node_type not in surface_cache:
            surface_cache[node_type] = await resolve_attr_surface(pool, space_id, node_type)
        return surface_cache[node_type]

    for batch in _batched(parse_jsonl(jsonl_path), batch_size):
        # Embed the whole batch first (peak-memory bound = batch_size vectors),
        # then write each item on its own transaction. Embedding stays outside
        # transaction() either way; batching is purely a throughput/memory lever.
        embedded = []
        for item in batch:
            keys, on = await _surface(item["type"])
            extra = embedding.render_attr_surface(item.get("attrs", {}), keys) if on else ""
            vector = embed_document(embedding.searchable_text(item["title"], item["body"], extra))
            embedded.append((item, vector, extra))
        for item, vector, extra in embedded:
            result = await write(
                pool,
                space_id,
                principal,
                item["type"],
                scope_id,
                item["title"],
                item["body"],
                item.get("attrs", {}),
                vector,
                source_client,
                import_mode=True,
                links=item.get("links"),
                extra_search=extra,
            )
            summary.total += 1
            outcome = result["outcome"]
            if outcome == "inserted":
                summary.inserted += 1
            elif outcome == "merged":
                summary.merged += 1
                # The import-mode envelope is stripped to {v, outcome,
                # addendum_added} precisely so this count is possible.
                if not result.get("addendum_added", True):
                    summary.merged_addendum_dropped += 1
            elif outcome == "merged_linked":
                # Phase B: the incoming was novel, so it was kept as its own
                # searchable member row and linked -- a terminal write, never a
                # dropped-addendum loss.
                summary.merged_linked += 1
            elif outcome == "review_queued":
                summary.review_queued += 1
                incoming = result["incoming"]
                candidate = result["candidate"]
                review_rows.append([
                    incoming["title"],
                    incoming["body"],
                    candidate["id"],
                    candidate["title"],
                    candidate["body"],
                    result["similarity"],
                ])
            else:  # pragma: no cover - the engine returns only these three in import mode
                raise AssertionError(f"unexpected import outcome {outcome!r}")

    if review_rows:
        with review_queue_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(_REVIEW_CSV_HEADER)
            writer.writerows(review_rows)
        summary.review_queue_path = review_queue_path

    return summary


def _batched(iterable, n: int):
    """Yield lists of up to ``n`` items. (itertools.batched is 3.12+, and the code
    still runs on the 3.10 scratch box the design tolerates, so it is spelled out.)"""
    if n < 1:
        raise ValueError("batch_size must be >= 1")
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) == n:
            yield batch
            batch = []
    if batch:
        yield batch
