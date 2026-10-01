"""Does the store hold the evidence at all? Measured with no LLM in the loop.

    python -m bench.extraction_coverage --space <space-id> [--space <space-id>] \
        --haystacks conv-26,conv-30,conv-49 --gap-labels <labels.json>

Retrieval can only return what was written. This compares one or more ingested
spaces on the one question retrieval cannot answer: for each question, is the
evidence the benchmark cites present in ANY stored memory of that scope?

A question's evidence turn counts as held when its text (first 60 normalised
characters, the same key `bench.k_sweep.recall` uses against retrieved bodies)
appears in the body of some memory in the scope. That is the ceiling on what any
read path could ever surface, so it isolates extraction from retrieval.

`--gap-labels` optionally takes the hand-labelled extraction-gap file and reports
coverage of that subset by category, which is the number the extraction work is
aimed at.

Reports store size alongside, because coverage bought by storing everything is
not a win: the point is to see coverage and size together.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import defaultdict

import psycopg

from bench.core import run as harness
from bench.k_sweep import PREFIX, _dia_ids, _norm, evidence_turns
from bench.console import use_utf8_streams

use_utf8_streams()


def scope_of(haystack_id: str, extractor: str) -> str:
    from bench.core.space import scope_id_for
    return scope_id_for(f"{haystack_id}:{extractor}")


def store_bodies(dsn: str, space_id: str) -> dict[str, list[str]]:
    """scope -> normalised bodies of every active memory."""
    out: dict[str, list[str]] = defaultdict(list)
    with psycopg.connect(dsn) as conn:
        cur = conn.cursor()
        cur.execute("SELECT scope_id, title, body FROM nodes "
                    "WHERE space_id = %s AND status = 'active'", (space_id,))
        for scope, title, body in cur.fetchall():
            out[scope].append(_norm((title or "") + " " + (body or "")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(prog="bench.extraction_coverage")
    ap.add_argument("--dsn", default=harness.DB)
    ap.add_argument("--space", action="append", required=True,
                    help="space id; repeat for several, compared side by side")
    ap.add_argument("--extractor", action="append", default=None,
                    help="extractor name per space, in the same order (default: llm)")
    ap.add_argument("--haystacks", default="conv-26,conv-30,conv-49")
    ap.add_argument("--dataset", default="locomo10.json")
    ap.add_argument("--gap-labels", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path)
    args = ap.parse_args()

    wanted = [h for h in args.haystacks.split(",") if h]
    extractors = args.extractor or ["llm"] * len(args.space)
    if len(extractors) != len(args.space):
        raise SystemExit("--extractor must be given once per --space, in the same order")

    dataset = harness.REPO / "datasets" / args.dataset
    corpus = harness.LOADERS["locomo"]().load(dataset)
    turns = evidence_turns(dataset)
    questions = [q for q in corpus.questions
                 if q.haystack_id in wanted and not q.abstain_expected]

    gap = {}
    if args.gap_labels:
        raw = json.loads(args.gap_labels.read_text(encoding="utf-8"))
        for section, items in raw.items():
            if section.startswith("_"):
                continue
            gap.update(items)

    report: dict = {"spaces": {}, "questions": len(questions), "haystacks": wanted}
    for space, extractor in zip(args.space, extractors):
        bodies = store_bodies(args.dsn, space)
        # Only this extractor's scopes. Two arms share a space, so counting the
        # whole space reports both arms' memories against each of them and hides
        # what the extraction change actually costs to store.
        mine = {scope_of(h, extractor) for h in wanted}
        size = {sc: len(b) for sc, b in sorted(bodies.items()) if sc in mine}
        held_all, held_any, per_cat = 0, 0, defaultdict(lambda: [0, 0])
        scorable = 0
        # Per question, so two spaces can be compared as paired outcomes rather
        # than as two rates. Extraction is a model call, so the comparison that
        # decides anything is paired: which questions one store holds and the
        # other does not.
        held_by_question: dict[str, int] = {}
        for q in questions:
            keys = [turns[(q.haystack_id, d)][:PREFIX] for d in _dia_ids(q.evidence)
                    if (q.haystack_id, d) in turns]
            if not keys:
                continue
            scorable += 1
            scope_bodies = bodies.get(scope_of(q.haystack_id, extractor), [])
            hits = sum(1 for k in keys if any(k in b for b in scope_bodies))
            held_all += 1 if hits == len(keys) else 0
            held_any += 1 if hits else 0
            held_by_question[q.question_id] = 1 if hits == len(keys) else 0
            if q.question_id in gap:
                cat = gap[q.question_id]
                per_cat[cat][1] += 1
                per_cat[cat][0] += 1 if hits == len(keys) else 0
        # Keyed by space AND extractor. Two arms of one run share a space and are
        # kept apart by scope, so keying on the space alone made the second arm
        # overwrite the first and left a report that could not be compared.
        report["spaces"][f"{space}#{extractor}"] = {
            "space": space,
            "extractor": extractor,
            "memories": sum(size.values()),
            "memories_by_scope": size,
            "scorable_questions": scorable,
            "all_evidence_held": held_all,
            "all_evidence_held_pct": round(100 * held_all / scorable, 1) if scorable else None,
            "some_evidence_held_pct": round(100 * held_any / scorable, 1) if scorable else None,
            "gap_set": {c: {"recovered": v[0], "n": v[1]} for c, v in sorted(per_cat.items())},
            "gap_set_total": {"recovered": sum(v[0] for v in per_cat.values()),
                              "n": sum(v[1] for v in per_cat.values())},
            "all_evidence_held_by_question": held_by_question,
        }

    text = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8", newline="\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
