"""Retrieval completeness, measured with no LLM anywhere in the loop.

    python -m bench.completeness_recall --run-dir runs/<run-id> --out runs/<run-id>/completeness

Re-runs retrieval for every question of a completed run against the store that
run kept, under several arms, and reports how much of each question's LoCoMo
evidence reaches the reader's context and at what size. Writes one envelope file
per arm so `bench.replay --envelopes` can read them under the run's own reader.

Arms (the width controls exist so a gain can be attributed to the mechanism
rather than to retrieving more):

  k20            hybrid search at the run's width; must reproduce the saved envelopes
  k25            hybrid search at the staged width (the control for the arms below)
  k25+roster     k25 plus the entity roster (bench/core/completeness.py)
  k25+turns      k25 plus the source-turn backstop
  k25+both       k25 plus both
  flat50         width control: unfiltered hybrid top-50, full bodies
  entity50       entity-filtered top-50 by cosine, full bodies

Nothing here writes to the store: fusion is called inside a read transaction and
recall statistics are not bumped.

Coverage. An evidence turn counts as covered when its text (first 60 normalised
characters, the `bench.k_sweep` key) appears in a full-body result, OR the store
node whose body quotes it is present by title in the roster, OR the turn itself
is among the source turns. The first is exact text in context; the second puts
the memory's title, not its source quote, in context; the third is exact.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from collections import defaultdict
from statistics import mean

from psycopg_pool import AsyncConnectionPool

from bench.core import completeness as C
from bench.core import run as harness
from bench.core.answer import render_envelope
from bench.k_sweep import PREFIX, _dia_ids, _norm, evidence_turns
from engraphy.core import embedding
from engraphy.core import search as S
from engraphy.core.scope_set import resolve_scope_set
from engraphy.server import db

ARMS = ("k20", "k25", "k25+roster", "k25+turns", "k25+both", "flat50", "entity50")


def _results(top, similarity, node_map):
    out = []
    for nid, score in top:
        item = {"node": S._node_envelope(node_map[nid], "full"), "score": score}
        if nid in similarity:
            item["similarity"] = round(similarity[nid], 2)
        out.append(item)
    return out


async def _entity_full(cur, space_id, scope_set, names, qvec, limit):
    if not names:
        return []
    import re
    pattern = "|".join(r"\m" + re.escape(n) + r"\M" for n in names)
    q = S._vector_literal(qvec)
    await cur.execute(
        "SELECT id FROM nodes WHERE space_id = %s AND scope_id = ANY(%s) AND status = 'active' "
        "AND (title ~* %s OR body ~* %s) ORDER BY embedding <=> %s::vector, id LIMIT %s",
        (space_id, list(scope_set), pattern, pattern, q, limit))
    ids = [str(i) for (i,) in await cur.fetchall()]
    node_map, _ = await S._hydrate_union(cur, ids)
    return [{"node": S._node_envelope(node_map[i], "full")} for i in ids]


async def build(args) -> dict:
    run_dir = args.run_dir if args.run_dir.is_absolute() else harness.REPO / args.run_dir
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    dataset = harness.REPO / "datasets" / manifest["dataset"]["path"]
    corpus = harness.LOADERS["locomo"]().load(dataset)
    wanted = set(manifest["haystacks"])
    arm = harness.parse_arm(args.arm)
    space_id = manifest["packs"][arm.pack]["space_id"]
    arm_space = harness.ArmSpace(space_id, "bench", arm.extractor)
    turns_by_hs = C.turn_indexes(corpus)
    keys_of = evidence_turns(dataset)
    questions = [q for q in corpus.questions if q.haystack_id in wanted]

    saved = {}
    for line in (run_dir / "envelopes.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        saved[row["question_id"]] = [r["node"]["id"] for r in row["envelope"].get("results") or []]

    args.out.mkdir(parents=True, exist_ok=True)
    sinks = {a: (args.out / f"envelopes-{a.replace('+', '_')}.jsonl").open("w", encoding="utf-8", newline="\n")
             for a in ARMS}
    per_q = (args.out / "per_question.jsonl").open("w", encoding="utf-8", newline="\n")

    embedding.load_model()
    pool = AsyncConnectionPool(harness.APP_DB, open=False, min_size=1, max_size=2)
    await pool.open()
    k20_match = 0
    try:
        for i, q in enumerate(questions, 1):
            scope = arm_space.scope_for(q.haystack_id)
            qvec = embedding.embed_query(q.text)
            keys = [(d, keys_of[(q.haystack_id, d)][:PREFIX]) for d in _dia_ids(q.evidence)
                    if (q.haystack_id, d) in keys_of]
            envs = {}
            async with db.transaction(pool, space_id, "bench") as conn:
                cur = conn.cursor()
                scope_set = sorted(await resolve_scope_set(cur, scope, False, "search"))
                fused = {}
                for width in (20, 25, 50):
                    top, sim, nmap, trunc = await S.hybrid_fuse(
                        cur, space_id, qvec, q.text, scope_set, None, False, width)
                    fused[width] = (_results(top, sim, nmap), trunc)
                names = C.names_in(q.text, await C.registry_names(cur, space_id, scope_set))
                k25_ids = {r["node"]["id"] for r in fused[25][0]}
                roster = await C.entity_roster(cur, space_id, scope_set, names, qvec, k25_ids)
                bodies = [r["node"].get("body") or "" for r in fused[25][0]]
                src = await turns_by_hs[q.haystack_id].search(cur, q.text, qvec, bodies)
                ent50 = await _entity_full(cur, space_id, scope_set, names, qvec, 50)
                # store bodies, to map roster ids to the evidence they quote
                await cur.execute("SELECT id, body FROM nodes WHERE space_id = %s AND scope_id = ANY(%s) "
                                  "AND status = 'active'", (space_id, scope_set))
                store_body = {str(n): _norm(b or "") for n, b in await cur.fetchall()}


            base = {"v": 1, "detail": "full", "scopes_searched": scope_set}
            envs["k20"] = {**base, "results": fused[20][0], "truncated": fused[20][1]}
            envs["k25"] = {**base, "results": fused[25][0], "truncated": fused[25][1]}
            envs["k25+roster"] = {**envs["k25"], "entities": names, "entity_roster": roster}
            envs["k25+turns"] = {**envs["k25"], "source_turns": C.turn_dicts(src)}
            envs["k25+both"] = {**envs["k25+roster"], "source_turns": C.turn_dicts(src)}
            envs["flat50"] = {**base, "results": fused[50][0], "truncated": fused[50][1]}
            envs["entity50"] = {**base, "results": ent50 or fused[50][0], "truncated": False}
            if [r["node"]["id"] for r in fused[20][0]] == saved.get(q.question_id):
                k20_match += 1

            row = {"question_id": q.question_id, "category": q.category,
                   "abstain_expected": q.abstain_expected, "entities": names,
                   "n_evidence": len(keys), "roster_n": len(roster), "turns_n": len(src)}
            for a, env in envs.items():
                full = [_norm((r.get("node") or r).get("body") or "") for r in env.get("results") or []]
                rost = [store_body.get(n["id"], "") for n in env.get("entity_roster") or []]
                tids = {t["turn_id"] for t in env.get("source_turns") or []}
                cov = {"full": 0, "roster": 0, "turn": 0}
                hit = 0
                for d, k in keys:
                    if any(k in b for b in full):
                        cov["full"] += 1; hit += 1
                    elif any(k in b for b in rost):
                        cov["roster"] += 1; hit += 1
                    elif d in tids:
                        cov["turn"] += 1; hit += 1
                row[a] = {"recall": (hit / len(keys)) if keys else None, **cov,
                          "chars": len(render_envelope(env))}
                sinks[a].write(json.dumps({"arm": f"{arm.arm_id}/{a}", "question_id": q.question_id,
                                           "envelope": env}) + "\n")
            per_q.write(json.dumps(row) + "\n")
            if i % 50 == 0:
                print(f"  {i}/{len(questions)}", file=sys.stderr, flush=True)
    finally:
        await pool.close()
        for f in sinks.values():
            f.close()
        per_q.close()
    return {"questions": len(questions), "k20_reproduces_saved_envelopes": k20_match}


def summarise(out: pathlib.Path) -> dict:
    rows = [json.loads(l) for l in (out / "per_question.jsonl").read_text(encoding="utf-8").splitlines()]
    agg = {}
    for a in ARMS:
        by = defaultdict(list)
        for r in rows:
            v = r[a]
            for b in ("all-non-adversarial" if not r["abstain_expected"] else "adversarial", r["category"]):
                by[b].append(v)
        agg[a] = {b: {"n": len(vs),
                      "evidence_recall": round(mean(v["recall"] for v in vs if v["recall"] is not None), 4)
                      if any(v["recall"] is not None for v in vs) else None,
                      "all_evidence": round(mean(1.0 if v["recall"] == 1.0 else 0.0
                                                 for v in vs if v["recall"] is not None), 4)
                      if any(v["recall"] is not None for v in vs) else None,
                      "chars_mean": round(mean(v["chars"] for v in vs))}
                  for b, vs in by.items()}
    return agg


def main() -> int:
    ap = argparse.ArgumentParser(prog="bench.completeness_recall")
    ap.add_argument("--run-dir", type=pathlib.Path, required=True)
    ap.add_argument("--arm", default="llm-conversational:search_only")
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--summarise-only", action="store_true")
    args = ap.parse_args()
    meta = {} if args.summarise_only else asyncio.run(build(args))
    summary = {"meta": meta, "by_arm": summarise(args.out)}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(meta))
    for a, b in summary["by_arm"].items():
        x = b["all-non-adversarial"]
        print(f"{a:11s} recall {x['evidence_recall']}  all-evidence {x['all_evidence']}  chars {x['chars_mean']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
