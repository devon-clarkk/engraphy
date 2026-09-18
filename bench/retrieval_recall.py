"""Retrieval recall@k for one embedding profile, with no LLM anywhere in the loop.

    python -m bench.retrieval_recall --dataset datasets/locomo10.json \
        --haystacks conv-26,conv-30,conv-49 --profile micro

`bench.core.run` is the full harness and it answers and judges, which needs
Claude and Gemini. That is the right instrument for end-to-end accuracy and the
wrong one for "does changing the embedder cost recall", because two thirds of its
pipeline is noise for that question and it cannot run unattended. This measures
the retrieval stack alone: whether the nodes carrying a question's gold evidence
come back in the top k.

## What makes the comparison honest

**The store is the same rows for every arm.** Turn-nodes are inserted directly,
one per turn, with no dedup and no extractor judgment, so the lexical leg is
literally identical between profiles and the embedding column is the only thing
that changes. Running the write path instead would have each profile's own bands
deciding which turns survive, and the arms would then differ in their CORPUS as
well as their vectors -- a much larger and less interpretable difference than the
one under test.

**The read path is the shipped one.** `core.search.search` is called, not a
hand-rolled fusion, so the number includes RRF against the real lexical leg and
the read-time near-duplicate collapse, both of which an operator gets. The
collapse threshold is per-profile, so it is part of what a profile does rather
than a confound to be excluded.

**Embedding is one text per call**, through `embed_document`, which is the write
path. A batched encode would return different vectors on a quantized graph (see
core/embedding.py on batch-invariance) and the measurement would partly be of the
harness.

## Scoring

A question counts as recalled when any node in the top k carries a turn id the
question names as evidence. Questions with no evidence -- LoCoMo's adversarial
abstention set is the bulk of them -- are excluded and counted separately: there
is no gold node to retrieve, so they measure abstention rather than recall.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import time

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from bench.adapters.locomo import LoCoMoLoader
from bench.core.extract import ExtractWindow, VerbatimExtractor
from engraphy.core import embedding
from engraphy.core import search as search_module
from engraphy.core.search import search

SPACE = "bench-recall"
SCOPE = "recall"
PRINCIPAL = "p1"
NODE_TYPE = "note"


def _database_url() -> str:
    url = os.environ.get("ENGRAPHY_TEST_DATABASE_URL") or os.environ.get("ENGRAPHY_DATABASE_URL")
    if not url:
        sys.exit("set ENGRAPHY_TEST_DATABASE_URL (or ENGRAPHY_DATABASE_URL) to a THROWAWAY database")
    return url


def bootstrap(conn) -> None:
    """A space, a principal, a type and a scope, created once. Dropped and
    recreated on every run so a re-run never measures a half-populated store."""
    cur = conn.cursor()
    for table in ("audit_log", "dedup_log", "edges", "nodes", "scopes",
                  "node_types", "principals"):
        cur.execute(f"DELETE FROM {table} WHERE space_id = %s", (SPACE,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (SPACE,))
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, 'Recall')", (SPACE,))
    cur.execute("INSERT INTO principals (space_id, id, display_name) VALUES (%s, %s, 'P')",
                (SPACE, PRINCIPAL))
    cur.execute("INSERT INTO node_types (space_id, name, description, attr_spec) "
                "VALUES (%s, %s, 'turn', %s)",
                (SPACE, NODE_TYPE, Jsonb({"attrs": {"closed": False}})))
    cur.execute("INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
                "VALUES (%s, %s, 'Recall', %s, 'private')", (SPACE, SCOPE, PRINCIPAL))
    conn.commit()


def turn_nodes(haystacks):
    """One draft per turn, through the repo's own verbatim extractor rather than
    a local reimplementation, so the text that gets embedded here is the text the
    harness would have stored."""
    extractor = VerbatimExtractor(node_type=NODE_TYPE)
    for haystack in haystacks:
        for session in haystack.sessions:
            result = extractor.extract(ExtractWindow(
                haystack_id=haystack.haystack_id, session=session,
                window_index=0, turns=session.turns))
            yield from result.nodes


def load_store(conn, drafts) -> dict[str, tuple[str, ...]]:
    """Insert every draft and return node id -> the turn ids it came from.

    Direct inserts, deliberately: see the module docstring. The embedding is
    computed one text per call through the production wrapper."""
    cur = conn.cursor()
    provenance: dict[str, tuple[str, ...]] = {}
    started = time.perf_counter()
    for i, draft in enumerate(drafts, 1):
        text = embedding.searchable_text(draft.title, draft.body, "")
        vec = embedding.embed_document(text)
        cur.execute(
            "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
            "embedding_model, source_client, author_principal) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s, 'bench', %s) RETURNING id",
            (SPACE, draft.node_type, SCOPE, draft.title, draft.body, Jsonb({}),
             "[" + ",".join(str(x) for x in vec) + "]", embedding.MODEL_STAMP, PRINCIPAL))
        provenance[str(cur.fetchone()[0])] = draft.provenance
        if i % 200 == 0:
            print(f"  embedded {i} turn-nodes", file=sys.stderr)
    conn.commit()
    elapsed = time.perf_counter() - started
    print(f"  {len(provenance)} turn-nodes in {elapsed:.1f}s "
          f"({elapsed / max(len(provenance), 1) * 1000:.1f} ms each)", file=sys.stderr)
    return provenance


def existing_store(conn, drafts) -> dict[str, tuple[str, ...]]:
    """Rebuild the node id -> turn ids map from a store that is already loaded,
    matching on body text. For re-running the measurement without paying the
    embedding cost again; the caller is responsible for the store already being
    in the active profile's vector space, and the stamp check below is what
    catches them when it is not."""
    cur = conn.cursor()
    cur.execute("SELECT id, body, embedding_model FROM nodes WHERE space_id = %s", (SPACE,))
    rows = cur.fetchall()
    stamps = {r[2] for r in rows}
    if stamps != {embedding.MODEL_STAMP}:
        sys.exit(f"--skip-load but the store holds {sorted(stamps)}, not "
                 f"{embedding.MODEL_STAMP!r}. Re-run without --skip-load.")
    by_body = {body: str(nid) for nid, body, _ in rows}
    provenance = {}
    for draft in drafts:
        nid = by_body.get(draft.body)
        if nid is not None:
            provenance[nid] = draft.provenance
    print(f"  reusing {len(provenance)} turn-nodes already in the store", file=sys.stderr)
    return provenance


async def measure(url: str, questions, provenance, k: int) -> dict:
    hits, asked = 0, 0
    async with AsyncConnectionPool(url, min_size=1, max_size=4, open=False) as pool:
        await pool.open()
        for question in questions:
            evidence = set(question.evidence)
            if not evidence:
                continue                    # abstention: no gold node to retrieve
            asked += 1
            result = await search(pool, SPACE, PRINCIPAL, SCOPE, question.text,
                                  "bench", limit=k)
            if any(evidence & set(provenance.get(r["node"]["id"], ()))
                   for r in result["results"]):
                hits += 1
    return {"evidence_bearing_questions": asked, "hits": hits,
            "recall_at_k": round(hits / asked, 4) if asked else None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, type=pathlib.Path)
    ap.add_argument("--haystacks", default="", help="comma-separated ids; default all")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--profile", help="informational; the profile comes from the environment")
    ap.add_argument("--collapse-at", type=float,
                    help="override the read-time near-duplicate collapse threshold. "
                         "Diagnostic, for deciding where that threshold belongs.")
    ap.add_argument("--no-collapse", action="store_true",
                    help="disable read-time near-duplicate collapse, so the arms differ "
                         "only in their vectors. Diagnostic: it is NOT what an operator "
                         "runs, and the shipped-path number is the one to quote.")
    ap.add_argument("--skip-load", action="store_true",
                    help="measure the store as it stands, without re-embedding it. Only "
                         "correct when the last load ran under THIS profile.")
    args = ap.parse_args()

    active = embedding.profile()
    if args.profile and args.profile != active:
        sys.exit(f"--profile says {args.profile} but ENGRAPHY_EMBEDDING_PROFILE resolves to "
                 f"{active}. The profile is process-level configuration; set the env var.")

    corpus = LoCoMoLoader().load(args.dataset)
    wanted = {h for h in args.haystacks.split(",") if h}
    haystacks = [h for h in corpus.haystacks if not wanted or h.haystack_id in wanted]
    if not haystacks:
        sys.exit(f"no haystacks matched {sorted(wanted)}")
    ids = {h.haystack_id for h in haystacks}
    questions = [q for q in corpus.questions if q.haystack_id in ids]

    print(f"profile={active}  stamp={embedding.MODEL_STAMP}", file=sys.stderr)
    print(f"haystacks={sorted(ids)}  questions={len(questions)}", file=sys.stderr)

    url = _database_url()
    with psycopg.connect(url) as conn:
        if args.skip_load:
            provenance = existing_store(conn, turn_nodes(haystacks))
        else:
            bootstrap(conn)
            provenance = load_store(conn, turn_nodes(haystacks))
    if args.collapse_at is not None:
        search_module._read_dedup_sim = lambda: args.collapse_at
    if args.no_collapse:
        # `search` collapses results it judges near-identical, at the ACTIVE
        # profile's merge band (core/search._near_dup_pairs). That is correct
        # behaviour and part of what an operator gets, but it means two profiles
        # differ in their read logic as well as their vectors, and a profile
        # whose similarities sit higher collapses more aggressively. Raising the
        # threshold to 1.0 turns the collapse off without touching the
        # calibration, which is what isolates the embedder's own contribution.
        search_module._read_dedup_sim = lambda: 1.0

    report = asyncio.run(measure(url, questions, provenance, args.k))
    report |= {"profile": active, "model_stamp": embedding.MODEL_STAMP, "k": args.k,
               "turn_nodes": len(provenance), "haystacks": sorted(ids),
               "questions_total": len(questions),
               "near_duplicate_collapse": (
                   False if args.no_collapse
                   else args.collapse_at if args.collapse_at is not None
                   else search_module._read_dedup_sim())}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
