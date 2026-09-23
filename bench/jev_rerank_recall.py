"""Does reranking search's candidates with Jev raise recall@k?

    # offline control arm, no key needed
    python -m bench.jev_rerank_recall --dataset datasets/locomo10.json \
        --haystacks conv-26,conv-30,conv-49 --scorer lexical

    # cost preview, then the real thing (needs JEV_API_KEY and JEV_API_URL)
    python -m bench.jev_rerank_recall ... --scorer jev --estimate-only
    python -m bench.jev_rerank_recall ... --scorer jev --skip-load

Same store and same shipped `search()` as `bench.retrieval_recall` (its
loaders are reused, not copied), so the baseline column here is the number that
script reports. The only thing added is a reorder of search's top `--depth`
before the top k is scored.

## Reading the result

`ceiling_at_depth` is recall@depth: the best any reorder could do, because
every arm is a permutation of the same `depth` candidates. A reranker can only
close the gap between `baseline` and that ceiling. If the gap is small, no
reranker is worth an API call, whatever it costs.

`gained` / `lost` count the questions an arm moves into / out of the top k
relative to baseline. They are the paired view: two arms with the same recall
can still disagree on many questions, and a small net gain made of large
gained and lost counts is noise, not signal.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

import psycopg
from psycopg_pool import AsyncConnectionPool

from bench.adapters.locomo import LoCoMoLoader
from bench.jev.client import (
    USD_PER_M_INPUT_TOKENS,
    JevClient,
    JevConfigError,
    LexicalScorer,
    estimate_tokens,
)
from bench.jev.rerank import ARMS
from bench.retrieval_recall import (
    PRINCIPAL,
    SCOPE,
    SPACE,
    _database_url,
    bootstrap,
    existing_store,
    load_store,
    turn_nodes,
)
from engraphy.core import embedding
from engraphy.core.search import _MAX_LIMIT, search


def candidate_text(node: dict) -> str:
    return f"{node.get('title', '')}\n{node.get('body', '')}".strip()


def hit(ids: list[str], k: int, evidence: set[str], provenance: dict) -> bool:
    return any(evidence & set(provenance.get(nid, ())) for nid in ids[:k])


def summarize(rows: list[dict], ks: list[int], arms: list[str]) -> dict:
    n = len(rows)
    out: dict = {"evidence_bearing_questions": n}
    if not n:
        return out
    for k in ks:
        base = [r["hits"]["baseline"][str(k)] for r in rows]
        block = {"baseline": round(sum(base) / n, 4)}
        for arm in arms:
            got = [r["hits"][arm][str(k)] for r in rows]
            block[arm] = {
                "recall": round(sum(got) / n, 4),
                "gained": sum(g and not b for g, b in zip(got, base)),
                "lost": sum(b and not g for g, b in zip(got, base)),
            }
        out[f"recall_at_{k}"] = block
    out["ceiling_at_depth"] = round(sum(r["ceiling"] for r in rows) / n, 4)
    return out


async def measure(url, questions, provenance, ks, depth, scorer, estimate_only, out_path):
    rows: list[dict] = []
    est_tokens = 0
    out = out_path.open("w") if out_path else None
    try:
        async with AsyncConnectionPool(url, min_size=1, max_size=4, open=False) as pool:
            await pool.open()
            for i, question in enumerate(questions, 1):
                evidence = set(question.evidence)
                if not evidence:
                    continue                # abstention: no gold node to retrieve
                result = await search(pool, SPACE, PRINCIPAL, SCOPE, question.text,
                                      "bench", limit=depth)
                nodes = [r["node"] for r in result["results"]]
                base_ids = [n["id"] for n in nodes]
                candidates = [(n["id"], candidate_text(n)) for n in nodes]
                est_tokens += sum(estimate_tokens(question.text) + estimate_tokens(t)
                                  for _, t in candidates)
                if estimate_only:
                    continue

                scores = await asyncio.to_thread(scorer.score, question.text, candidates)
                orders = {"baseline": base_ids} | {
                    name: fn(base_ids, scores) for name, fn in ARMS.items()}
                row = {
                    "question": question.text,
                    "ceiling": hit(base_ids, depth, evidence, provenance),
                    "hits": {arm: {str(k): hit(ids, k, evidence, provenance) for k in ks}
                             for arm, ids in orders.items()},
                    "gold_rank": {arm: next((j for j, nid in enumerate(ids, 1)
                                             if evidence & set(provenance.get(nid, ()))), None)
                                  for arm, ids in orders.items()},
                    "scores": [round(scores.get(nid, 0.0), 4) for nid in base_ids],
                }
                rows.append(row)
                if out:
                    out.write(json.dumps(row) + "\n")
                if i % 50 == 0:
                    print(f"  scored {i}/{len(questions)} questions", file=sys.stderr)
    finally:
        if out:
            out.close()
    return rows, est_tokens


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, type=pathlib.Path)
    ap.add_argument("--haystacks", default="", help="comma-separated ids; default all")
    ap.add_argument("--k", default="5,10", help="comma-separated cutoffs, each < --depth")
    ap.add_argument("--depth", type=int, default=_MAX_LIMIT,
                    help=f"candidates pulled from search and reordered (max {_MAX_LIMIT})")
    ap.add_argument("--scorer", choices=("lexical", "jev"), default="lexical")
    ap.add_argument("--max-questions", type=int,
                    help="stop after this many questions, to cap spend on a first run")
    ap.add_argument("--estimate-only", action="store_true",
                    help="run search, print the Jev input-token and cost estimate, "
                         "make no scorer calls")
    ap.add_argument("--skip-load", action="store_true",
                    help="reuse the store from the last run under this profile")
    ap.add_argument("--cache", type=pathlib.Path, default=pathlib.Path("runs/jev-cache.jsonl"),
                    help="Jev score cache; a re-run with the same prompt is free")
    ap.add_argument("--out", type=pathlib.Path, help="per-question JSONL for analysis")
    args = ap.parse_args()

    ks = sorted({int(k) for k in args.k.split(",") if k})
    if not 1 <= args.depth <= _MAX_LIMIT:
        sys.exit(f"--depth must be in 1..{_MAX_LIMIT} (search's own cap)")
    if any(k >= args.depth for k in ks):
        sys.exit(f"every --k must be below --depth {args.depth}: at k == depth every "
                 f"arm is the same set, so there is nothing to measure")

    scorer = LexicalScorer()
    if args.scorer == "jev" and not args.estimate_only:
        args.cache.parent.mkdir(parents=True, exist_ok=True)
        try:
            scorer = JevClient.from_env(cache_path=args.cache)
        except JevConfigError as exc:
            sys.exit(str(exc))

    corpus = LoCoMoLoader().load(args.dataset)
    wanted = {h for h in args.haystacks.split(",") if h}
    haystacks = [h for h in corpus.haystacks if not wanted or h.haystack_id in wanted]
    if not haystacks:
        sys.exit(f"no haystacks matched {sorted(wanted)}")
    ids = {h.haystack_id for h in haystacks}
    questions = [q for q in corpus.questions if q.haystack_id in ids]
    if args.max_questions:
        questions = questions[:args.max_questions]
    print(f"profile={embedding.profile()}  scorer={scorer.name}  depth={args.depth}  "
          f"questions={len(questions)}", file=sys.stderr)

    url = _database_url()
    with psycopg.connect(url) as conn:
        if args.skip_load:
            provenance = existing_store(conn, turn_nodes(haystacks))
        else:
            bootstrap(conn)
            provenance = load_store(conn, turn_nodes(haystacks))

    rows, est_tokens = asyncio.run(measure(url, questions, provenance, ks, args.depth,
                                           scorer, args.estimate_only, args.out))
    estimate = {"input_tokens": est_tokens,
                "usd": round(est_tokens / 1e6 * USD_PER_M_INPUT_TOKENS, 4)}
    if args.estimate_only:
        print(json.dumps({"jev_estimate_uncached": estimate}, indent=2))
        return

    report = summarize(rows, ks, list(ARMS)) | {
        "scorer": scorer.name, "depth": args.depth, "k": ks,
        "profile": embedding.profile(), "model_stamp": embedding.MODEL_STAMP,
        "haystacks": sorted(ids), "questions_total": len(questions),
    }
    if isinstance(scorer, JevClient):
        report["jev_usage"] = {"calls": scorer.calls, "cache_hits": scorer.cache_hits,
                               "input_tokens_est": scorer.input_tokens,
                               "usd_est": round(scorer.input_tokens / 1e6
                                                * USD_PER_M_INPUT_TOKENS, 4)}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
