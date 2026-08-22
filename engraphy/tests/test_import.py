"""engraphy.admin.import_ -- bulk import end-to-end (design/02 §Bulk import,
acceptance "A 1,000-item synthetic backfill imports idempotently with a correct
review queue"; dedup-write-path-plan.md trap 6).

The pipeline-level import-mode gates (silent AUTO-MERGE, PENDING -> review-queue
envelope, INSERT == a normal write) are proven in test_dedup.py. These tests prove
the *module*: JSONL parsing, per-outcome tallying, the review CSV, and above all
idempotency over a real file re-run.

Embeddings are deterministic and model-independent, exactly as the rest of the
dedup suite is (test_dedup._unit_vector_at_angle): a text maps to a fixed 384-dim
unit vector, so re-importing the same file yields the same vectors and therefore
the same >= 0.95 self-hits that make re-import a no-op. Distinct texts map to
near-orthogonal vectors (random high-dim unit vectors have expected cosine ~0,
std ~1/sqrt(384) ~ 0.05), so unrelated items all INSERT. Controlled-similarity
pairs are built by Gram-Schmidt so a review-queue item sits at exactly 0.85.
"""

import json
import math
import random

import pytest

from psycopg.types.json import Jsonb

from engraphy.admin import import_
from engraphy.admin.import_ import ImportLineError, parse_jsonl, run_import
from engraphy.core import sentinel
from engraphy.core.dedup import ValidationError

# Reuse test_dedup's live-Postgres scaffolding verbatim -- importing a fixture
# into a test module registers it for use here (pytest resolves by name).
from engraphy.tests.test_dedup import _seed_node, write_space  # noqa: F401

_DIMS = 384


def _unit_from_text(text: str) -> list[float]:
    """A deterministic near-orthogonal 384-dim unit vector keyed by text. Same
    text -> same vector (idempotency); distinct texts -> ~orthogonal (all INSERT)."""
    r = random.Random(text)
    v = [r.gauss(0.0, 1.0) for _ in range(_DIMS)]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


def _controlled_vector(base: list[float], sim: float, seed: str) -> list[float]:
    """A unit vector whose cosine with ``base`` is exactly ``sim`` (base is unit).
    b = sim*base + sqrt(1-sim^2)*orth, orth = a fresh vector made orthonormal to
    base via one Gram-Schmidt step."""
    raw = _unit_from_text(seed)
    dot = sum(x * y for x, y in zip(raw, base))
    orth = [x - dot * b for x, b in zip(raw, base)]
    onorm = math.sqrt(sum(x * x for x in orth))
    orth = [x / onorm for x in orth]
    scale = math.sqrt(max(0.0, 1.0 - sim * sim))
    return [sim * b + scale * o for b, o in zip(base, orth)]


class _Embedder:
    """embed_document stand-in: explicit overrides first (controlled-similarity
    items), else the hash-based near-orthogonal vector. The join `title + "\\n" +
    body` is what run_import passes, so keys are that joined string."""

    def __init__(self, overrides: dict[str, list[float]] | None = None):
        self.overrides = overrides or {}

    def __call__(self, text: str) -> list[float]:
        if text in self.overrides:
            return self.overrides[text]
        return _unit_from_text(text)


def _write_jsonl(path, items):
    path.write_text("".join(json.dumps(it) + "\n" for it in items), encoding="utf-8")
    return path


def _node_count(conn, space_id):
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s", (space_id,))
    return cur.fetchone()[0]


# ---- parsing --------------------------------------------------------------


def test_parse_jsonl_accepts_valid_and_skips_blanks(tmp_path):
    p = tmp_path / "in.jsonl"
    p.write_text(
        json.dumps({"type": "widget", "title": "t", "body": "b"}) + "\n"
        + "\n"  # blank line, ignorable
        + json.dumps({"type": "widget", "title": "t2", "body": "b2", "attrs": {"k": 1}, "links": []}) + "\n",
        encoding="utf-8",
    )
    items = list(parse_jsonl(p))
    assert len(items) == 2
    assert items[1]["attrs"] == {"k": 1}


@pytest.mark.parametrize(
    "line,needle",
    [
        ("not json", "not valid JSON"),
        (json.dumps([1, 2]), "expected a JSON object"),
        (json.dumps({"type": "w", "title": "t"}), "missing or empty required field 'body'"),
        (json.dumps({"type": "w", "title": "", "body": "b"}), "missing or empty required field 'title'"),
        (json.dumps({"type": "w", "title": "t", "body": "b", "junk": 1}), "unknown field"),
        (json.dumps({"type": "w", "title": "t", "body": "b", "attrs": [1]}), "'attrs' must be an object"),
        (json.dumps({"type": "w", "title": "t", "body": "b", "links": {}}), "'links' must be a list"),
    ],
)
def test_parse_jsonl_rejects_malformed_lines(tmp_path, line, needle):
    p = tmp_path / "bad.jsonl"
    p.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ImportLineError) as exc:
        list(parse_jsonl(p))
    assert needle in str(exc.value)
    assert "line 1" in str(exc.value)


# ---- import behavior ------------------------------------------------------


async def test_import_distinct_items_all_insert(pool, write_space, conn, tmp_path):
    items = [{"type": "widget", "title": f"Item {i}", "body": f"Body number {i}."} for i in range(5)]
    path = _write_jsonl(tmp_path / "in.jsonl", items)

    summary = await run_import(
        pool, write_space, "p1", "scope1", path, embed_document=_Embedder(),
    )

    assert (summary.total, summary.inserted, summary.merged, summary.review_queued) == (5, 5, 0, 0)
    assert summary.review_queue_path is None  # nothing queued -> no CSV
    assert not (tmp_path / "in.jsonl.review_queue.csv").exists()
    assert _node_count(conn, write_space) == 5


async def test_import_rerun_is_idempotent(pool, write_space, conn, tmp_path):
    """The property backfill safety rests on: re-importing the same file creates
    zero new nodes -- every item hits >= 0.95 against the node it made and absorbs
    silently (02 §Bulk import)."""
    items = [{"type": "widget", "title": f"Item {i}", "body": f"Body number {i}."} for i in range(5)]
    path = _write_jsonl(tmp_path / "in.jsonl", items)
    embed = _Embedder()

    first = await run_import(pool, write_space, "p1", "scope1", path, embed_document=embed)
    assert first.inserted == 5
    assert _node_count(conn, write_space) == 5

    second = await run_import(pool, write_space, "p1", "scope1", path, embed_document=embed)
    assert (second.inserted, second.merged, second.review_queued) == (0, 5, 0)
    assert _node_count(conn, write_space) == 5, "re-import must create zero rows"


async def test_import_review_queue_routing(pool, write_space, conn, tmp_path):
    """A 0.85-similarity item lands in the CSV, not the DB (02 acceptance; test
    matrix 'Import review queue')."""
    base_text = "Anchor\nThe original memory body."
    base_vec = _unit_from_text(base_text)
    anchor_id = _seed_node(conn, write_space, "widget", "Anchor", "The original memory body.", {}, base_vec)

    dup_text = "Near duplicate\nA reworded near-duplicate body."
    dup_vec = _controlled_vector(base_vec, 0.85, seed="dup")

    path = _write_jsonl(
        tmp_path / "in.jsonl",
        [{"type": "widget", "title": "Near duplicate", "body": "A reworded near-duplicate body."}],
    )

    summary = await run_import(
        pool, write_space, "p1", "scope1", path,
        embed_document=_Embedder({dup_text: dup_vec}),
    )

    assert (summary.inserted, summary.merged, summary.review_queued) == (0, 0, 1)
    # not written to the DB: still just the anchor, and nothing parked.
    assert _node_count(conn, write_space) == 1
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM pending_writes WHERE space_id = %s", (write_space,))
    assert cur.fetchone()[0] == 0

    # ...written to the CSV, with the design's incoming/candidate/similarity data.
    assert summary.review_queue_path is not None
    lines = summary.review_queue_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "incoming_title,incoming_body,candidate_id,candidate_title,candidate_body,similarity"
    assert len(lines) == 2
    row = lines[1].split(",")
    assert row[0] == "Near duplicate"
    assert row[2] == str(anchor_id)
    assert row[5] == "0.85"


async def test_import_scope_must_be_writable_by_principal(pool, write_space, tmp_path):
    """Import is RLS-enforced like every other write: an unknown/unwritable scope
    for the principal is a not-found, never a silent insert into the wrong place."""
    path = _write_jsonl(
        tmp_path / "in.jsonl",
        [{"type": "widget", "title": "x", "body": "y"}],
    )
    with pytest.raises(Exception):  # ScopeUnknownError-class; no row escapes
        await run_import(
            pool, write_space, "p1", "no-such-scope", path, embed_document=_Embedder(),
        )


# On Windows the asyncio selector event loop's ~15ms timer granularity inflates
# each of the ~2000 statements this test issues, so the 1000-item run takes
# ~107s locally; on Linux CI (epoll) the same work is <10s, well within the
# default 60s budget. This is a Linux-CI acceptance test; the override exists
# only so it can complete on Windows-local without tripping the global timeout.
@pytest.mark.timeout(180)
async def test_thousand_item_backfill_idempotent_with_review_queue(pool, write_space, conn, tmp_path):
    """design/02 acceptance criterion, end to end: a 1,000-item synthetic backfill
    imports idempotently AND produces a correct review queue. The file is 1,000
    distinct items (all INSERT) plus a handful of deliberate 0.85 near-duplicates
    of some of them (all -> review queue, never the DB)."""
    n_base = 1000
    n_review = 5

    base_items = [
        {"type": "widget", "title": f"Backfill {i}", "body": f"Synthetic backfill body {i}."}
        for i in range(n_base)
    ]
    # Near-duplicates of the first few base items, at exactly 0.85.
    overrides = {}
    review_items = []
    for j in range(n_review):
        base = base_items[j]
        base_vec = _unit_from_text(base["title"] + "\n" + base["body"])
        title, body = f"Dup of {j}", f"A reworded duplicate of backfill {j}."
        overrides[f"{title}\n{body}"] = _controlled_vector(base_vec, 0.85, seed=f"dup-{j}")
        review_items.append({"type": "widget", "title": title, "body": body})

    path = _write_jsonl(tmp_path / "backfill.jsonl", base_items + review_items)
    embed = _Embedder(overrides)

    first = await run_import(pool, write_space, "p1", "scope1", path, embed_document=embed)
    assert first.total == n_base + n_review
    assert first.inserted == n_base
    assert first.review_queued == n_review
    assert first.merged == 0
    assert _node_count(conn, write_space) == n_base

    # correct review queue: header + one row per near-duplicate.
    lines = first.review_queue_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 + n_review
    assert all(r.rsplit(",", 1)[1] == "0.85" for r in lines[1:])

    # idempotent re-run: the 1,000 originals now self-hit and merge silently; the
    # near-duplicates stay at 0.85 and re-queue. Zero new nodes either way.
    second = await run_import(pool, write_space, "p1", "scope1", path, embed_document=embed)
    assert second.inserted == 0
    assert second.merged == n_base
    assert second.review_queued == n_review
    assert _node_count(conn, write_space) == n_base, "re-import creates zero rows"


def test_default_review_queue_path_sits_next_to_source(tmp_path):
    src = tmp_path / "export.jsonl"
    assert import_._default_review_queue_path(src).name == "export.jsonl.review_queue.csv"


async def test_import_fails_the_run_loudly_on_a_reserved_type_row(
        pool, write_space, conn, tmp_path):
    """Fable's correction 2 (ruled 2026-07-21): a reserved-type JSONL row does
    NOT route to the review queue -- that is the PENDING band's import-mode
    surface, for items a human should adjudicate. This is a malformed export,
    so the ValidationError propagates straight out of run_import and the run
    dies at that line, exactly as a reserved `attrs.addenda` key does today.

    Rows already written stay written; the re-run after fixing the export is
    safe because import is idempotent by construction -- which is why failing
    loudly costs nothing and silently queueing would cost a permanent junk row
    of an engine-owned type.

    The type is REGISTERED in this space first, so the refusal proved here is
    ours and not the nodes.(space_id, type) FK's."""
    conn.cursor().execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES (%s, %s, %s, %s)",
        (write_space, sentinel.SENTINEL_NODE_TYPE, sentinel.SENTINEL_TYPE_DESCRIPTION,
         Jsonb(sentinel.SENTINEL_ATTR_SPEC)),
    )
    conn.commit()

    items = [
        {"type": "widget", "title": "Good item", "body": "A perfectly normal body."},
        {"type": sentinel.SENTINEL_NODE_TYPE, "title": "Decoy", "body": "Reserved type."},
        {"type": "widget", "title": "Never reached", "body": "Behind the bad row."},
    ]
    path = _write_jsonl(tmp_path / "reserved.jsonl", items)

    with pytest.raises(ValidationError) as exc:
        await run_import(pool, write_space, "p1", "scope1", path, embed_document=_Embedder())
    assert sentinel.SENTINEL_NODE_TYPE in str(exc.value)
    assert "reserved for the engine" in str(exc.value)

    # The run died AT the bad line: the row before it is committed (import
    # writes one transaction per item), the row after it never ran.
    assert _node_count(conn, write_space) == 1
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s AND type = %s",
                (write_space, sentinel.SENTINEL_NODE_TYPE))
    assert cur.fetchone()[0] == 0
    assert not (tmp_path / "reserved.jsonl.review_queue.csv").exists()


async def test_import_counts_merges_that_dropped_the_incoming_body(
        pool, write_space, conn, tmp_path):
    """Obligation 3 of the 2026-07-21 contradiction ruling. Import-mode absorb is
    silent by design (02 §Bulk import) and there is nobody in the loop to read an
    envelope, so a backfill can discard caller text with no trace at all. This
    count is the after-the-fact tell -- design/05's E5 backfill caveat.

    Why it matters beyond bookkeeping: the wording that trips the Jaccard novelty
    check is near-verbatim wording, and a NEGATION is near-verbatim by nature
    ("... no longer works as a paediatric nurse"). So a chronologically-ordered
    backfill is exactly the shape that destroys its own corrections, and this is
    the number that says so.
    """
    embed = _Embedder()
    items = [{"type": "widget", "title": f"Item {i}", "body": f"Body number {i}."}
             for i in range(3)]
    path = _write_jsonl(tmp_path / "in.jsonl", items)

    first = await run_import(pool, write_space, "p1", "scope1", path, embed_document=embed)
    assert first.inserted == 3
    assert first.merged_addendum_dropped == 0, "nothing merged on a clean first pass"

    # Re-import: every item self-hits at 1.0 and its body is byte-identical, so
    # each merge appends nothing at all.
    second = await run_import(pool, write_space, "p1", "scope1", path, embed_document=embed)
    assert second.merged == 3
    assert second.merged_addendum_dropped == 3
    assert second.as_dict()["merged_addendum_dropped"] == 3

    # A subset of `merged`, not a separate outcome -- the total identity holds.
    assert second.total == (
        second.inserted + second.merged + second.merged_linked + second.review_queued
    )


async def test_import_novel_merge_links_and_is_not_a_dropped_addendum(
        pool, write_space, conn, tmp_path):
    """Phase B: a merge whose text WAS novel is not a loss and never a dropped
    addendum -- it merge-LINKS (its own searchable member row), counted under
    `merged_linked`, and `merged_addendum_dropped` stays 0."""
    base_text = "Anchor\nThe original memory body."
    base_vec = _unit_from_text(base_text)
    canonical_id = _seed_node(
        conn, write_space, "widget", "Anchor", "The original memory body.", {}, base_vec)

    novel_body = "A completely different sentence carrying genuinely new information."
    embed = _Embedder({"Anchor\n" + novel_body: _controlled_vector(base_vec, 0.97, seed="m")})
    path = _write_jsonl(tmp_path / "novel.jsonl",
                        [{"type": "widget", "title": "Anchor", "body": novel_body}])

    summary = await run_import(pool, write_space, "p1", "scope1", path, embed_document=embed)
    assert summary.merged_linked == 1
    assert summary.merged == 0
    assert summary.merged_addendum_dropped == 0

    cur = conn.cursor()
    # the canonical kept NO addendum (the old leak site); the novel text is a row.
    cur.execute(
        "SELECT attrs->'addenda' FROM nodes WHERE id = %s", (canonical_id,))
    assert cur.fetchone()[0] in (None, []), "no addendum: the novel text became a member row"
    cur.execute(
        "SELECT count(*) FROM nodes WHERE space_id = %s AND body = %s", (write_space, novel_body))
    assert cur.fetchone()[0] == 1, "the novel body is its own searchable member row"
